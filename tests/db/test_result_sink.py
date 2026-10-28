from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import Engine, select

from inventory.aws import ClientFactory, EndpointResolver
from inventory.collectors import registry
from inventory.config import get_common_settings
from inventory.db import ResourceRepository, resource, scope_state
from inventory.engine import ScanEngine, reconcile
from inventory.models import ResourceKey, ScanTaskMessage, TypeResult, TypeScope, TypeStatus
from inventory.transport import PostgresResultSink
from tests.collectors.conftest import seed_ec2
from tests.conftest import FROZEN_NOW
from tests.db.test_repository import rec
from tests.engine.fakes import FakeCredentials

pytestmark = pytest.mark.integration
SCOPE = TypeScope("AWS::EC2::VPC", "123456789012", "eu-west-1")


def apply(sink: PostgresResultSink, records: list, started_offset: int = 0) -> TypeResult:  # type: ignore[type-arg]
    index = sink.load_index(SCOPE.resource_type, SCOPE.account_id, SCOPE.region)
    started = FROZEN_NOW + timedelta(hours=started_offset)
    changes = reconcile(SCOPE, index, records, set(), started, "job")
    result = TypeResult(
        TypeStatus.SUCCEEDED,
        created=changes.created,
        updated=changes.updated,
        deleted=len(changes.deleted_ids),
        unchanged=changes.unchanged,
    )
    sink.apply(SCOPE, changes, result, "job", len(records))
    return result


def test_apply_writes_changes_and_scope_state(db: Engine) -> None:
    sink = PostgresResultSink(db, clock=lambda: FROZEN_NOW + timedelta(hours=4))

    first = apply(sink, [rec("vpc_a_v1"), rec("vpc_b")])
    second = apply(sink, [rec("vpc_a_v2", FROZEN_NOW + timedelta(hours=4))], started_offset=4)

    assert (first.created, second.updated, second.deleted) == (2, 1, 1)
    with db.connect() as conn:
        state = conn.execute(select(scope_state)).mappings().one()
        deleted = (
            conn.execute(select(resource.c.resource_id).where(resource.c.deleted_at.is_not(None)))
            .scalars()
            .all()
        )
    assert state["status"] == "succeeded" and state["resource_count"] == 1
    assert state["last_success_at"] == FROZEN_NOW + timedelta(hours=4)
    assert deleted == ["vpc-0b"]


def test_apply_refresh_statuses(db: Engine) -> None:
    sink = PostgresResultSink(db, clock=lambda: FROZEN_NOW + timedelta(hours=1))
    key = rec("vpc_a_v1").key

    statuses = [
        sink.apply_refresh(key, rec("vpc_a_v1", FROZEN_NOW), "j1"),
        sink.apply_refresh(key, rec("vpc_a_v1", FROZEN_NOW + timedelta(minutes=1)), "j2"),
        sink.apply_refresh(key, rec("vpc_a_v2", FROZEN_NOW + timedelta(minutes=2)), "j3"),
        sink.apply_refresh(key, None, "j4"),
        sink.apply_refresh(key, None, "j5"),
        sink.apply_refresh(
            ResourceKey("AWS::EC2::VPC", "123456789012", "eu-west-1", "vpc-404"), None, "j6"
        ),
    ]

    assert statuses == ["created", "unchanged", "updated", "deleted", "not_found", "not_found"]


def test_record_skip(db: Engine) -> None:
    PostgresResultSink(db).record_skip(SCOPE, TypeStatus.AUTH_DENIED, "job", None)
    with db.connect() as conn:
        assert conn.execute(select(scope_state.c.status)).scalar_one() == "auth_denied"


@pytest.fixture
def moto_ec2(settings_env: dict[str, str]) -> Iterator[dict[str, str]]:
    with mock_aws():
        yield seed_ec2(boto3.client("ec2", region_name="ap-east-1"))


def test_engine_scan_end_to_end(db: Engine, moto_ec2: dict[str, str]) -> None:
    resolver = EndpointResolver(get_common_settings())
    engine = ScanEngine(
        registry, ClientFactory(resolver), resolver, FakeCredentials(), PostgresResultSink(db)
    )
    task = ScanTaskMessage(
        job_id="job-e2e",
        task_id="t1",
        trigger="schedule",
        account_id="123456789012",
        region="ap-east-1",
    )

    first = engine.scan(task)
    boto3.client("ec2", region_name="ap-east-1").terminate_instances(
        InstanceIds=[moto_ec2["web-01"]]
    )
    second = engine.scan(task.model_copy(update={"job_id": "job-e2e-2"}))

    assert {r.status for r in first.values()} == {TypeStatus.SUCCEEDED}
    assert first["AWS::EC2::Instance"].created == 1
    assert second["AWS::EC2::Instance"].deleted == 1
    assert second["AWS::EC2::VPC"].unchanged == 2
    with db.connect() as conn:
        instance = ResourceRepository().get(
            conn, ResourceKey("AWS::EC2::Instance", "123456789012", "ap-east-1", moto_ec2["web-01"])
        )
    assert instance is not None and instance["deleted_at"] is not None


def test_refresh_does_not_delete_a_newer_observation(db: Engine) -> None:
    sink = PostgresResultSink(db, clock=lambda: FROZEN_NOW)
    key = rec("vpc_a_v1").key
    sink.apply_refresh(key, rec("vpc_a_v1", FROZEN_NOW + timedelta(minutes=5)), "j1")

    assert sink.apply_refresh(key, None, "late-refresh") == "unchanged"
