"""Collector base classes, collector context and the registry.

A collector reads one resource type from AWS. It never writes to the database and never
swallows errors: a partial listing must fail loudly, otherwise resources would be deleted.
"""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import boto3
from botocore.exceptions import ClientError

from inventory.aws import AwsClient, ClientFactory, EndpointResolver
from inventory.config import GLOBAL_REGION
from inventory.models import KeepExisting, ResourceKey, ResourceRecord
from inventory.observability import submit_with_context

Raw = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedArn:
    account_id: str | None
    region: str | None
    resource_id: str
    # the ARN only carries the end of the resource id (e.g. old-format ECS service ARNs)
    id_is_suffix: bool = False


@dataclass
class CollectorContext:
    account_id: str
    region: str  # region label stored with the resource; 'global' for global types
    api_region: str  # region the API calls go to
    session: boto3.Session
    clients: ClientFactory
    full_refresh: bool = False
    known_ids: frozenset[str] = frozenset()
    executor_workers: int = 4
    memo_store: dict[str, Any] = field(default_factory=dict)
    memo_lock: threading.Lock = field(default_factory=threading.Lock)

    def client(self, service: str) -> AwsClient:
        return self.clients.client(service, self.region, self.session)

    def memo[T](self, key: str, loader: Callable[[], T]) -> T:
        """Share one expensive call between collectors of the same task (one lock per key)."""
        with self.memo_lock:
            key_lock = self.memo_store.setdefault(f"\0lock:{key}", threading.Lock())
        with key_lock:
            if key not in self.memo_store:
                self.memo_store[key] = loader()
            value: T = self.memo_store[key]
            return value

    def map[A, R](self, fn: Callable[[A], R], items: Iterable[A]) -> Iterator[R]:
        """Bounded concurrent map that keeps order, log context and propagates errors."""
        values = list(items)
        if not values:
            return iter(())
        workers = max(1, min(self.executor_workers, len(values)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [submit_with_context(pool, fn, v) for v in values]
            return iter([f.result() for f in futures])


class Collector(ABC):
    resource_type: ClassVar[str]
    service: ClassVar[str]
    is_global: ClassVar[bool] = False
    required_endpoints: ClassVar[tuple[str, ...]] = ()  # defaults to (service,)
    depends_on: ClassVar[tuple[str, ...]] = ()
    drop_fields: ClassVar[tuple[str, ...]] = ()
    arn_pattern: ClassVar[re.Pattern[str]]

    def __init__(self, ctx: CollectorContext) -> None:
        self.ctx = ctx
        self.client = ctx.client(self.service)

    @classmethod
    def endpoints(cls) -> tuple[str, ...]:
        return cls.required_endpoints or (cls.service,)

    # ---------------------------------------------------------------- to implement
    @abstractmethod
    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]: ...

    @abstractmethod
    def get_resource(self, resource_id: str) -> Raw | None: ...

    @abstractmethod
    def resource_id(self, raw: Raw) -> str: ...

    @abstractmethod
    def build_arn(self, raw: Raw) -> str: ...

    # ---------------------------------------------------------------- optional overrides
    def extract_tags(self, raw: Raw) -> dict[str, str]:
        return tags_to_dict(raw.get("Tags", raw.get("tags")))

    def extract_name(self, raw: Raw) -> str | None:
        return self.extract_tags(raw).get("Name")

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {}

    @classmethod
    def parse_arn(cls, arn: str) -> ParsedArn | None:
        match = cls.arn_pattern.match(arn)
        if match is None:
            return None
        groups = match.groupdict()
        return ParsedArn(groups.get("account") or None, groups.get("region") or None, groups["id"])

    # ---------------------------------------------------------------- template method
    def to_record(self, raw: Raw, observed_at: datetime) -> ResourceRecord:
        data = {k: v for k, v in raw.items() if k not in self.drop_fields}
        for tag_field in ("Tags", "tags"):
            # AWS does not guarantee tag order; an order change must not look like a change
            value = data.get(tag_field)
            if isinstance(value, list) and all(isinstance(t, Mapping) for t in value):
                data[tag_field] = sorted(value, key=lambda t: str(t.get("Key", t.get("key", ""))))
        key = ResourceKey(
            self.resource_type, self.ctx.account_id, self.ctx.region, self.resource_id(raw)
        )
        return ResourceRecord.build(
            key,
            arn=self.build_arn(raw),
            name=self.extract_name(raw),
            tags=self.extract_tags(raw),
            relations=self.extract_relations(raw),
            data=data,
            observed_at=observed_at,
        )


def tags_to_dict(tags: Any) -> dict[str, str]:
    """Normalise [{'Key','Value'}], [{'key','value'}] or a plain dict into {key: value}."""
    if not tags:
        return {}
    if isinstance(tags, Mapping):
        return {str(k): str(v) for k, v in tags.items()}
    result: dict[str, str] = {}
    for tag in tags:
        key = tag.get("Key", tag.get("key"))
        if key is not None:
            result[str(key)] = str(tag.get("Value", tag.get("value", "")))
    return result


def error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class Ec2DescribeCollector(Collector):
    """Declarative base for EC2 Describe* resources."""

    service = "ec2"
    operation: ClassVar[str]
    result_key: ClassVar[str]
    id_field: ClassVar[str]
    ids_param: ClassVar[str]
    arn_resource: ClassVar[str]
    not_found_codes: ClassVar[tuple[str, ...]] = ()
    extra_params: ClassVar[dict[str, Any]] = {}
    arn_has_account: ClassVar[bool] = True

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        paginator = self.client.get_paginator(self.operation)
        for page in paginator.paginate(**self.extra_params):
            yield from (item for item in self.items(page) if self.is_live(item))

    def get_resource(self, resource_id: str) -> Raw | None:
        try:
            page = getattr(self.client, self.operation)(
                **{self.ids_param: [resource_id]}, **self.extra_params
            )
        except ClientError as exc:
            if error_code(exc) in self.not_found_codes:
                return None
            raise
        items = [i for i in self.items(page) if self.is_live(i)]
        return items[0] if items else None

    def items(self, page: Mapping[str, Any]) -> list[Raw]:
        return list(page.get(self.result_key, []))

    def is_live(self, raw: Raw) -> bool:
        return True

    def resource_id(self, raw: Raw) -> str:
        return str(raw[self.id_field])

    def build_arn(self, raw: Raw) -> str:
        account = self.ctx.account_id if self.arn_has_account else ""
        return (
            f"arn:aws:ec2:{self.ctx.region}:{account}:{self.arn_resource}/{self.resource_id(raw)}"
        )


# --------------------------------------------------------------------------- registry


class Registry:
    def __init__(self) -> None:
        self._collectors: dict[str, type[Collector]] = {}

    def register(self, cls: type[Collector]) -> type[Collector]:
        if cls.resource_type in self._collectors:
            raise ValueError(f"duplicate collector for {cls.resource_type}")
        self._collectors[cls.resource_type] = cls
        return cls

    def get(self, resource_type: str) -> type[Collector]:
        try:
            return self._collectors[resource_type]
        except KeyError:
            raise KeyError(f"unknown resource type {resource_type}") from None

    def has(self, resource_type: str) -> bool:
        return resource_type in self._collectors

    def all(self) -> list[type[Collector]]:
        return [self._collectors[t] for t in sorted(self._collectors)]

    def applicable(self, region: str) -> list[type[Collector]]:
        """Types that belong to a region: global types only for 'global', regional otherwise."""
        is_global = region == GLOBAL_REGION
        return [c for c in self.all() if c.is_global == is_global]

    def for_region(
        self, region: str, resolver: EndpointResolver
    ) -> tuple[list[type[Collector]], list[type[Collector]]]:
        """Split applicable types into (available, skipped because an endpoint is missing)."""
        available: list[type[Collector]] = []
        skipped: list[type[Collector]] = []
        for cls in self.applicable(region):
            ok = all(resolver.has_endpoint(s, region) for s in cls.endpoints())
            (available if ok else skipped).append(cls)
        return available, skipped

    def route_arn(self, arn: str) -> tuple[type[Collector], ParsedArn] | None:
        for cls in self.all():
            parsed = cls.parse_arn(arn)
            if parsed is not None:
                return cls, parsed
        return None

    def groups(self, resource_types: Sequence[str]) -> list[list[type[Collector]]]:
        """Dependency groups: connected components, each in topological order.

        Missing dependencies are added; a dependency cycle raises ValueError.
        """
        included: set[str] = set()
        pending = list(resource_types)
        while pending:
            current = pending.pop()
            if current in included:
                continue
            included.add(current)
            pending.extend(self.get(current).depends_on)

        parent = {t: t for t in included}

        def find(t: str) -> str:
            while parent[t] != t:
                parent[t] = parent[parent[t]]
                t = parent[t]
            return t

        for t in included:
            for dep in self.get(t).depends_on:
                parent[find(t)] = find(dep)

        components: dict[str, list[str]] = {}
        for t in sorted(included):
            components.setdefault(find(t), []).append(t)
        return [
            [self.get(t) for t in self._topological(members)]
            for members in sorted(components.values(), key=lambda m: m[0])
        ]

    def _topological(self, members: list[str]) -> list[str]:
        remaining = {t: set(self.get(t).depends_on) for t in members}
        ordered: list[str] = []
        while remaining:
            ready = sorted(t for t, deps in remaining.items() if not deps - set(ordered))
            if not ready:
                raise ValueError(f"dependency cycle among {sorted(remaining)}")
            for t in ready:
                ordered.append(t)
                del remaining[t]
        return ordered


registry = Registry()
register = registry.register
