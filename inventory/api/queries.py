"""Asynchronous read-only queries used by the API."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, cast, func, literal, select, text, tuple_
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from inventory.db import account, account_region, resource, scan_job, scan_task, scope_state
from inventory.errors import BadRequestError, InventoryError
from inventory.models import TERMINAL_TASK_STATUSES

GROUPABLE = ("account_id", "region", "resource_type", "eim", "account_type")
ACCOUNT_FIELDS = ("eim", "account_type")  # from the account table, joined on account_id


class QueryTimeoutError(InventoryError):
    default_code = "QUERY_TIMEOUT"
    http_status = 422


class InvalidQueryError(InventoryError):
    default_code = "VALIDATION_ERROR"
    http_status = 422


@dataclass(frozen=True)
class ResourceFilters:
    account_ids: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    eims: tuple[str, ...] = ()
    account_types: tuple[str, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)
    tag_keys: tuple[str, ...] = ()
    name_prefix: str | None = None
    changed_since: datetime | None = None
    include_deleted: bool = False
    data_jsonpath: str | None = None

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def account_clauses(self, account_id: Any) -> list[ColumnElement[bool]]:
        """EIM / account type filters, as a subquery on the (small) account table."""
        where: list[ColumnElement[bool]] = []
        for column, values in (("eim", self.eims), ("account_type", self.account_types)):
            if values:
                accounts = select(account.c.account_id).where(account.c[column].in_(values))
                where.append(account_id.in_(accounts))
        return where

    @property
    def is_scope_only(self) -> bool:
        """Only account/region/type/EIM filters: counts can come from scope_state."""
        return not (
            self.tags
            or self.tag_keys
            or self.name_prefix
            or self.changed_since
            or self.include_deleted
            or self.data_jsonpath
        )

    def clauses(self) -> list[ColumnElement[bool]]:
        c = resource.c
        where: list[ColumnElement[bool]] = []
        if self.account_ids:
            where.append(c.account_id.in_(self.account_ids))
        if self.regions:
            where.append(c.region.in_(self.regions))
        if self.resource_types:
            where.append(c.resource_type.in_(self.resource_types))
        where.extend(self.account_clauses(c.account_id))
        if self.tags:
            where.append(c.tags.contains(self.tags))
        for key in self.tag_keys:
            # "@?" can use the jsonb_path_ops GIN index; "?" (has_key) cannot
            where.append(c.tags.op("@?")(cast(literal(jsonpath_key(key)), JSONPATH)))
        if self.name_prefix:
            where.append(c.name.startswith(self.name_prefix, autoescape=True))
        if self.changed_since:
            where.append(c.last_changed_at >= self.changed_since)
        if not self.include_deleted:
            where.append(c.deleted_at.is_(None))
        if self.data_jsonpath:
            where.append(func.jsonb_path_match(c.data, cast(literal(self.data_jsonpath), JSONPATH)))
        return where


@dataclass(frozen=True)
class PageRequest:
    sort: str = "last_changed_at"
    order: str = "desc"
    limit: int = 100
    cursor: str | None = None
    with_total: bool = False
    full: bool = False


@dataclass
class ResourcePage:
    rows: list[RowMapping]
    next_cursor: str | None
    total: int | None


def _sort_columns(sort: str) -> list[Any]:
    c = resource.c
    tail = [c.resource_type, c.account_id, c.region, c.resource_id]
    return [c.last_changed_at, *tail] if sort == "last_changed_at" else [c.resource_id, *tail[:3]]


def encode_cursor(sort: str, order: str, fingerprint: str, values: Sequence[Any]) -> str:
    payload = {
        "v": 1,
        "s": sort,
        "o": order,
        "f": fingerprint,
        "k": [v.isoformat() if isinstance(v, datetime) else v for v in values],
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(
    cursor: str, sort: str, order: str, fingerprint: str, key_count: int
) -> list[Any]:
    """Decode and fully validate a cursor; anything unexpected is a 400, never a 500."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        values = list(payload["k"])
        if payload.get("v") != 1 or (payload["s"], payload["o"], payload["f"]) != (
            sort,
            order,
            fingerprint,
        ):
            raise ValueError("cursor does not match the request")
        if len(values) != key_count or not all(isinstance(v, str) for v in values):
            raise ValueError("malformed cursor keys")
        if sort in ("last_changed_at", "created_at"):
            values[0] = datetime.fromisoformat(values[0])
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
        raise BadRequestError("invalid cursor (it is bound to the same sort and filters)") from None
    return values


def jsonpath_key(key: str) -> str:
    """``$."key"`` with the key escaped for a jsonpath string literal."""
    return '$."' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resource_columns(full: bool) -> list[Any]:
    columns = [c for c in resource.c if c.name not in ("data", "content_hash", "last_job_id")]
    if full:
        columns.append(resource.c.data)
    return [
        *columns,
        scope_state.c.last_success_at.label("verified_at"),
        account.c.name.label("account_name"),
        account.c.eim,
        account.c.account_type,
    ]


def _with_scope_state(stmt: Any) -> Any:
    return stmt.select_from(
        resource.outerjoin(
            scope_state,
            and_(
                scope_state.c.account_id == resource.c.account_id,
                scope_state.c.region == resource.c.region,
                scope_state.c.resource_type == resource.c.resource_type,
            ),
        ).outerjoin(account, account.c.account_id == resource.c.account_id)
    )


class ResourceQueries:
    def __init__(self, engine: AsyncEngine, search_timeout_ms: int = 5000) -> None:
        self.engine = engine
        self.search_timeout_ms = search_timeout_ms

    async def list_resources(self, filters: ResourceFilters, page: PageRequest) -> ResourcePage:
        keys = _sort_columns(page.sort)
        fingerprint = filters.fingerprint()
        where = filters.clauses()
        if page.cursor:
            values = decode_cursor(page.cursor, page.sort, page.order, fingerprint, len(keys))
            boundary = tuple_(*keys)
            where.append(
                boundary > tuple_(*values) if page.order == "asc" else boundary < tuple_(*values)
            )
        ordering = [k.asc() if page.order == "asc" else k.desc() for k in keys]
        stmt = (
            _with_scope_state(select(*_resource_columns(page.full)))
            .where(*where)
            .order_by(*ordering)
            .limit(page.limit + 1)
        )
        rows = await self._fetch(stmt, filters)
        next_cursor = None
        if len(rows) > page.limit:
            rows = rows[: page.limit]
            last = rows[-1]
            next_cursor = encode_cursor(
                page.sort, page.order, fingerprint, [last[k.name] for k in keys]
            )
        total = None
        if page.with_total:
            count_stmt = select(func.count()).select_from(resource).where(*filters.clauses())
            total = int((await self._fetch(count_stmt, filters, scalar=True))[0])
        return ResourcePage(rows, next_cursor, total)

    async def get(self, key: tuple[str, str, str, str], include_deleted: bool) -> RowMapping | None:
        c = resource.c
        where = [
            c.resource_type == key[0],
            c.account_id == key[1],
            c.region == key[2],
            c.resource_id == key[3],
        ]
        if not include_deleted:
            where.append(c.deleted_at.is_(None))
        rows = await self._fetch(
            _with_scope_state(select(*_resource_columns(True))).where(*where), None
        )
        return rows[0] if rows else None

    async def get_by_arn(self, arn: str, include_deleted: bool) -> RowMapping | None:
        where = [resource.c.arn == arn]
        if not include_deleted:
            where.append(resource.c.deleted_at.is_(None))
        stmt = (
            _with_scope_state(select(*_resource_columns(True)))
            .where(*where)
            .order_by(resource.c.deleted_at.desc().nulls_first())
            .limit(1)
        )
        rows = await self._fetch(stmt, None)
        return rows[0] if rows else None

    async def stats(
        self, group_by: Sequence[str], filters: ResourceFilters
    ) -> tuple[list[dict[str, Any]], int]:
        if filters.is_scope_only:
            source = scope_state
            count: Any = func.coalesce(func.sum(scope_state.c.resource_count), 0)
            where: list[Any] = [
                scope_state.c.status == "succeeded",
                scope_state.c.resource_count.is_not(None),
            ]
            for column, values in (
                ("account_id", filters.account_ids),
                ("region", filters.regions),
                ("resource_type", filters.resource_types),
            ):
                if values:
                    where.append(scope_state.c[column].in_(values))
            where.extend(filters.account_clauses(scope_state.c.account_id))
        else:
            source = resource
            count = func.count()
            where = filters.clauses()
        columns = [account.c[g] if g in ACCOUNT_FIELDS else source.c[g] for g in group_by]
        stmt = select(*columns, count.label("count")).where(*where)
        if any(g in ACCOUNT_FIELDS for g in group_by):
            stmt = stmt.select_from(
                source.outerjoin(account, account.c.account_id == source.c.account_id)
            )
        if columns:
            stmt = stmt.group_by(*columns).order_by(*columns)
        rows = await self._fetch(stmt, filters)
        groups = [dict(r) | {"count": int(r["count"])} for r in rows if int(r["count"]) > 0]
        return groups, sum(g["count"] for g in groups)

    async def accounts(self) -> list[RowMapping]:
        return await self._fetch(select(account).order_by(account.c.account_id), None)

    async def account(self, account_id: str) -> RowMapping | None:
        rows = await self._fetch(select(account).where(account.c.account_id == account_id), None)
        return rows[0] if rows else None

    async def coverage(self, account_id: str) -> tuple[list[RowMapping], list[RowMapping]]:
        regions = await self._fetch(
            select(account_region).where(account_region.c.account_id == account_id), None
        )
        scopes = await self._fetch(
            select(scope_state)
            .where(scope_state.c.account_id == account_id)
            .order_by(scope_state.c.region, scope_state.c.resource_type),
            None,
        )
        return regions, scopes

    # ------------------------------------------------------------------ jobs
    async def job(self, job_id: str) -> RowMapping | None:
        rows = await self._fetch(select(scan_job).where(scan_job.c.job_id == job_id), None)
        return rows[0] if rows else None

    async def job_tasks(self, job_id: str) -> list[RowMapping]:
        return await self._fetch(
            select(scan_task).where(scan_task.c.job_id == job_id).order_by(scan_task.c.task_id),
            None,
        )

    async def live_job_stats(self, job_id: str) -> dict[str, Any]:
        counts = await self._fetch(
            select(scan_task.c.status, func.count().label("n"))
            .where(scan_task.c.job_id == job_id)
            .group_by(scan_task.c.status),
            None,
        )
        by_status = {r["status"]: int(r["n"]) for r in counts}
        sums = await self._fetch(
            text(
                """
                SELECT coalesce(sum((r.value->>'created')::int), 0) AS created,
                       coalesce(sum((r.value->>'updated')::int), 0) AS updated,
                       coalesce(sum((r.value->>'deleted')::int), 0) AS deleted,
                       coalesce(sum((r.value->>'unchanged')::int), 0) AS unchanged
                FROM scan_task t CROSS JOIN LATERAL jsonb_each(t.result) AS r(key, value)
                WHERE t.job_id = :job_id AND jsonb_typeof(r.value) = 'object'
                """
            ).bindparams(job_id=job_id),
            None,
        )
        return {
            "tasks_total": sum(by_status.values()),
            "tasks_done": sum(n for s, n in by_status.items() if s in TERMINAL_TASK_STATUSES),
            "by_status": by_status,
            **{k: int(v) for k, v in dict(sums[0]).items()},
        }

    async def list_jobs(
        self,
        statuses: Sequence[str],
        trigger: str | None,
        account_id: str | None,
        created_since: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[RowMapping], str | None]:
        c = scan_job.c
        where: list[Any] = []
        if statuses:
            where.append(c.status.in_(statuses))
        if trigger:
            where.append(c.trigger == trigger)
        if account_id:
            where.append(c.scope["account_id"].astext == account_id)
        if created_since:
            where.append(c.created_at >= created_since)
        fingerprint = hashlib.sha256(
            json.dumps([sorted(statuses), trigger, account_id, str(created_since)]).encode()
        ).hexdigest()[:16]
        if cursor:
            values = decode_cursor(cursor, "created_at", "desc", fingerprint, 2)
            where.append(tuple_(c.created_at, c.job_id) < tuple_(values[0], values[1]))
        rows = await self._fetch(
            select(scan_job)
            .where(*where)
            .order_by(c.created_at.desc(), c.job_id.desc())
            .limit(limit + 1),
            None,
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = encode_cursor(
                "created_at",
                "desc",
                fingerprint,
                [rows[-1]["created_at"].isoformat(), rows[-1]["job_id"]],
            )
        return rows, next_cursor

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    # ------------------------------------------------------------------ helpers
    async def _fetch(
        self, stmt: Any, filters: ResourceFilters | None, scalar: bool = False
    ) -> list[Any]:
        async with self.engine.connect() as conn:
            if filters is not None and filters.data_jsonpath:
                await conn.execute(
                    text(f"SET LOCAL statement_timeout = {int(self.search_timeout_ms)}")
                )
            try:
                result = await conn.execute(stmt)
            except DBAPIError as exc:
                translated = _translate(exc, filters)
                if translated is None:
                    raise
                raise translated from None
            return list(result.scalars()) if scalar else list(result.mappings())


def _translate(exc: DBAPIError, filters: ResourceFilters | None) -> InventoryError | None:
    """Client-caused query failures become 422; everything else stays a server error."""
    sqlstate = getattr(exc.orig, "sqlstate", None) or ""
    if sqlstate == "57014":  # query_canceled (statement_timeout)
        return QueryTimeoutError("query exceeded the time limit; narrow the filters")
    jsonpath_error = sqlstate == "42601" or sqlstate.startswith("2203")
    if filters is not None and filters.data_jsonpath and jsonpath_error:
        return InvalidQueryError("invalid data_jsonpath")
    return None
