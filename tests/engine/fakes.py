"""Test doubles for the engine: fake collectors, an in-memory sink and credentials."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar

import boto3

from inventory.collectors.base import Collector, Raw, Registry
from inventory.db import IndexEntry
from inventory.errors import AccessDeniedError
from inventory.models import (
    ChangeSet,
    ErrorInfo,
    KeepExisting,
    RefreshStatus,
    ResourceKey,
    ResourceRecord,
    TypeResult,
    TypeScope,
    TypeStatus,
)
from tests.helpers import load_fixture


class FakeCollector(Collector):
    """Lists items from tests/fixtures/engine/fake_resources.json."""

    service = "ec2"
    fail_with: ClassVar[BaseException | None] = None
    fail_after: ClassVar[int] = 0
    keep_known: ClassVar[bool] = False
    seen_upstream: ClassVar[dict[str, list[str]]] = {}

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        type(self).seen_upstream[self.resource_type] = sorted(upstream)
        for index, item in enumerate(
            load_fixture("engine/fake_resources.json")[self.resource_type]
        ):
            if self.fail_with is not None and index >= self.fail_after:
                raise self.fail_with
            if self.keep_known and item["Id"] in self.ctx.known_ids:
                yield KeepExisting(item["Id"])
            else:
                yield dict(item)

    def get_resource(self, resource_id: str) -> Raw | None:
        items = load_fixture("engine/fake_resources.json")[self.resource_type]
        return next((dict(i) for i in items if i["Id"] == resource_id), None)

    def resource_id(self, raw: Raw) -> str:
        return str(raw["Id"])

    def build_arn(self, raw: Raw) -> str:
        return f"arn:aws:test:{self.ctx.region}:{self.ctx.account_id}:thing/{raw['Id']}"


def make_registry() -> tuple[Registry, dict[str, type[FakeCollector]]]:
    """A fresh registry with Parent <- Child and an independent Solo type."""

    class Parent(FakeCollector):
        resource_type = "Test::Parent"
        arn_pattern = re.compile(
            r"^arn:aws:test:(?P<region>[^:]+):(?P<account>\d{12}):thing/(?P<id>p-\d+)$"
        )

    class Child(FakeCollector):
        resource_type = "Test::Child"
        depends_on = ("Test::Parent",)
        arn_pattern = re.compile(
            r"^arn:aws:test:(?P<region>[^:]+):(?P<account>\d{12}):thing/(?P<id>c-\d+)$"
        )

        def extract_relations(self, raw: Raw) -> dict[str, Any]:
            return {"parent": raw["Parent"]}

    class Solo(FakeCollector):
        resource_type = "Test::Solo"
        arn_pattern = re.compile(
            r"^arn:aws:test:(?P<region>[^:]+):(?P<account>\d{12}):thing/(?P<id>s-\d+)$"
        )

    registry = Registry()
    for cls in (Parent, Child, Solo):
        cls.fail_with = None
        cls.fail_after = 0
        cls.keep_known = False
        cls.seen_upstream = {}
        registry.register(cls)
    return registry, {"parent": Parent, "child": Child, "solo": Solo}


class MemorySink:
    """In-memory ResultSink."""

    def __init__(self, index: dict[str, dict[str, IndexEntry]] | None = None) -> None:
        self.index = index or {}
        self.applied: dict[str, ChangeSet] = {}
        self.skips: list[tuple[str, TypeStatus, ErrorInfo | None]] = []
        self.refreshed: list[tuple[ResourceKey, ResourceRecord | None]] = []
        self._lock = threading.Lock()

    def load_index(self, resource_type: str, account_id: str, region: str) -> dict[str, IndexEntry]:
        return dict(self.index.get(resource_type, {}))

    def apply(
        self,
        scope: TypeScope,
        changes: ChangeSet,
        result: TypeResult,
        job_id: str,
        resource_count: int,
    ) -> None:
        with self._lock:
            self.applied[scope.resource_type] = changes

    def apply_refresh(
        self, key: ResourceKey, record: ResourceRecord | None, job_id: str
    ) -> RefreshStatus:
        with self._lock:
            self.refreshed.append((key, record))
        return "updated" if record else "not_found"

    def record_skip(
        self, scope: TypeScope, status: TypeStatus, job_id: str, error: ErrorInfo | None
    ) -> None:
        with self._lock:
            self.skips.append((scope.resource_type, status, error))


class FakeCredentials:
    def __init__(self, denied: bool = False) -> None:
        self.denied = denied

    def session(self, account_id: str) -> boto3.Session:
        if self.denied:
            raise AccessDeniedError(f"no ReadOnly role for account {account_id}")
        return boto3.Session(region_name="ap-east-1")

    def accessible_accounts(self) -> set[str]:
        return set()
