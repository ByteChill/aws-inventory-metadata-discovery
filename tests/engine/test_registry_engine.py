from __future__ import annotations

import logging
import re

import pytest
from botocore.exceptions import ClientError

from inventory.aws import ClientFactory, EndpointResolver
from inventory.collectors import registry as global_registry
from inventory.collectors.base import Collector, Registry
from inventory.config import get_common_settings
from inventory.db import IndexEntry
from inventory.engine import ScanEngine
from inventory.models import ResourceKey, ScanTaskMessage, TypeStatus
from inventory.observability import ContextFilter
from tests.conftest import FROZEN_NOW
from tests.engine.fakes import FakeCollector, FakeCredentials, MemorySink, make_registry

ACCOUNT = "123456789012"


def task(**overrides: object) -> ScanTaskMessage:
    payload = {
        "job_id": "job-1",
        "task_id": "task-1",
        "trigger": "api",
        "account_id": ACCOUNT,
        "region": "ap-east-1",
        **overrides,
    }
    return ScanTaskMessage.model_validate(payload)


@pytest.fixture
def engine_parts(settings_env: dict[str, str]) -> tuple[EndpointResolver, ClientFactory]:
    resolver = EndpointResolver(get_common_settings())
    return resolver, ClientFactory(resolver)


def make_engine(
    registry: Registry,
    parts: tuple[EndpointResolver, ClientFactory],
    sink: MemorySink,
    denied: bool = False,
) -> ScanEngine:
    resolver, clients = parts
    return ScanEngine(
        registry, clients, resolver, FakeCredentials(denied), sink, clock=lambda: FROZEN_NOW
    )


# --------------------------------------------------------------------------- registry


def test_groups_are_topological_and_add_missing_dependencies() -> None:
    registry, _ = make_registry()

    groups = registry.groups(["Test::Child", "Test::Solo"])

    assert [[c.resource_type for c in g] for g in groups] == [
        ["Test::Parent", "Test::Child"],
        ["Test::Solo"],
    ]


def test_dependency_cycle_is_rejected() -> None:
    class A(FakeCollector):
        resource_type = "Cycle::A"
        depends_on = ("Cycle::B",)
        arn_pattern = re.compile("^a$")

    class B(FakeCollector):
        resource_type = "Cycle::B"
        depends_on = ("Cycle::A",)
        arn_pattern = re.compile("^b$")

    registry = Registry()
    registry.register(A)
    registry.register(B)
    with pytest.raises(ValueError, match="cycle"):
        registry.groups(["Cycle::A"])


def test_duplicate_registration_is_rejected() -> None:
    registry, classes = make_registry()
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(classes["parent"])


def test_route_arn() -> None:
    registry, classes = make_registry()

    routed = registry.route_arn(f"arn:aws:test:eu-west-1:{ACCOUNT}:thing/c-9")

    assert routed is not None
    cls, parsed = routed
    assert cls is classes["child"]
    assert (parsed.account_id, parsed.region, parsed.resource_id) == (ACCOUNT, "eu-west-1", "c-9")
    assert registry.route_arn("arn:aws:other:::x") is None


def test_for_region_skips_types_without_endpoints(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, _ = make_registry()

    available, skipped = registry.for_region("ap-east-1", engine_parts[0])

    assert [c.resource_type for c in available] == ["Test::Child", "Test::Parent"]
    assert [c.resource_type for c in skipped] == ["Test::Solo"]
    assert registry.applicable("global") == []


def test_global_registry_has_v1_ec2_types() -> None:
    types = {c.resource_type for c in global_registry.all()}
    assert {"AWS::EC2::VPC", "AWS::EC2::Instance", "AWS::EC2::Image"} <= types
    assert all(issubclass(c, Collector) for c in global_registry.all())


# --------------------------------------------------------------------------- engine


def test_scan_passes_upstream_and_records_skips(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, classes = make_registry()
    sink = MemorySink()

    results = make_engine(registry, engine_parts, sink).scan(task())

    assert results["Test::Parent"].status is TypeStatus.SUCCEEDED
    assert results["Test::Parent"].created == 2
    assert results["Test::Child"].created == 2
    assert results["Test::Solo"].status is TypeStatus.SKIPPED_NO_ENDPOINT
    assert classes["child"].seen_upstream["Test::Child"] == ["Test::Parent"]
    child = sink.applied["Test::Child"].upserts[0]
    assert child.relations == {"parent": "p-1"}
    assert ("Test::Solo", TypeStatus.SKIPPED_NO_ENDPOINT, None) in sink.skips


def test_upstream_failure_fails_dependents_without_deleting(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, classes = make_registry()
    classes["parent"].fail_with = ClientError(
        {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "DescribeParents"
    )
    classes["parent"].fail_after = 1  # fails mid-listing
    index = {"Test::Parent": {"p-9": IndexEntry(b"\x00", FROZEN_NOW, False)}}
    sink = MemorySink(index)

    results = make_engine(registry, engine_parts, sink).scan(task(resource_types=["Test::Child"]))

    assert results["Test::Parent"].error_code == "THROTTLED"
    assert results["Test::Parent"].retryable
    assert results["Test::Child"].error_code == "UPSTREAM_FAILED"
    assert results["Test::Child"].retryable
    assert "Test::Parent" not in sink.applied  # partial listing never reconciled


def test_access_denied_marks_every_type(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, _ = make_registry()
    sink = MemorySink()

    results = make_engine(registry, engine_parts, sink, denied=True).scan(task())

    assert {r.status for t, r in results.items() if t != "Test::Solo"} == {TypeStatus.AUTH_DENIED}
    assert not sink.applied


def test_keep_existing_is_unchanged(engine_parts: tuple[EndpointResolver, ClientFactory]) -> None:
    registry, classes = make_registry()
    classes["child"].keep_known = True
    sink = MemorySink({"Test::Child": {"c-1": IndexEntry(b"\x00", FROZEN_NOW, False)}})

    results = make_engine(registry, engine_parts, sink).scan(task(resource_types=["Test::Child"]))

    assert (results["Test::Child"].created, results["Test::Child"].unchanged) == (1, 1)
    assert sink.applied["Test::Child"].deleted_ids == []


def test_keep_existing_is_rejected_for_types_with_dependents(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, classes = make_registry()
    classes["parent"].keep_known = True
    sink = MemorySink({"Test::Parent": {"p-1": IndexEntry(b"\x00", FROZEN_NOW, False)}})

    results = make_engine(registry, engine_parts, sink).scan(task(resource_types=["Test::Parent"]))

    assert results["Test::Parent"].status is TypeStatus.FAILED
    assert "Test::Parent" not in sink.applied


def test_auto_added_dependency_without_endpoint_is_skipped(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, classes = make_registry()
    classes["parent"].service = "solo"  # no endpoint configured
    try:
        results = make_engine(registry, engine_parts, MemorySink()).scan(
            task(resource_types=["Test::Child"])
        )
    finally:
        classes["parent"].service = "ec2"

    assert results["Test::Parent"].status is TypeStatus.SKIPPED_NO_ENDPOINT
    assert results["Test::Child"].error_code == "UPSTREAM_FAILED"


def test_listed_records_are_stamped_with_the_scan_start(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, _ = make_registry()
    sink = MemorySink()

    make_engine(registry, engine_parts, sink).scan(task(resource_types=["Test::Parent"]))

    assert {r.observed_at for r in sink.applied["Test::Parent"].upserts} == {FROZEN_NOW}


def test_batch_refresh_by_ids_never_deletes(
    engine_parts: tuple[EndpointResolver, ClientFactory],
) -> None:
    registry, _ = make_registry()
    sink = MemorySink()

    results = make_engine(registry, engine_parts, sink).scan(
        task(resource_types=["Test::Parent"], resource_ids=["p-1", "p-404"])
    )

    assert results["Test::Parent"].updated == 1
    assert results["Test::Parent"].unchanged == 1  # not_found counts as unchanged
    assert [k.resource_id for k, _ in sink.refreshed] == ["p-1", "p-404"]
    assert not sink.applied


def test_refresh_single_resource(engine_parts: tuple[EndpointResolver, ClientFactory]) -> None:
    registry, _ = make_registry()
    sink = MemorySink()
    engine = make_engine(registry, engine_parts, sink)

    found = engine.refresh(ResourceKey("Test::Parent", ACCOUNT, "ap-east-1", "p-2"), "job-2")
    missing = engine.refresh(ResourceKey("Test::Parent", ACCOUNT, "ap-east-1", "p-404"), "job-2")
    unknown = engine.refresh(ResourceKey("Test::Nope", ACCOUNT, "ap-east-1", "x"), "job-2")

    assert found.status == "updated" and found.record is not None
    assert found.record.name is None and found.record.tags == {}
    assert missing.status == "not_found"
    assert unknown.status == "unsupported_type"


def test_worker_threads_keep_log_context(
    engine_parts: tuple[EndpointResolver, ClientFactory], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="inventory.engine")
    caplog.handler.addFilter(ContextFilter())  # what setup_logging installs in production
    registry, _ = make_registry()

    make_engine(registry, engine_parts, MemorySink()).scan(task())

    summaries = [r for r in caplog.records if r.getMessage() == "resource type scanned"]
    assert {r.resource_type for r in summaries} >= {"Test::Parent", "Test::Child"}  # type: ignore[attr-defined]
    assert all(getattr(r, "task_id", None) == "task-1" for r in summaries)
