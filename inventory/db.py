"""Database schema (SQLAlchemy Core), engines and repositories.

Repositories are synchronous and take a ``Connection`` so callers control transactions.
API read queries live in :mod:`inventory.api.queries`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote_plus

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    Connection,
    Engine,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    and_,
    case,
    create_engine,
    delete,
    event,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from ulid import ULID

from inventory.config import CommonSettings
from inventory.models import (
    TERMINAL_TASK_STATUSES,
    DirectoryAccount,
    NewTask,
    ResourceKey,
    ResourceRecord,
    ScanJob,
    ScanScope,
    ScanTask,
    TaskStatus,
    Trigger,
    TypeStatus,
    utcnow,
)

UPSERT_BATCH = 500
TS = TIMESTAMP(timezone=True)

metadata = MetaData()

resource = Table(
    "resource",
    metadata,
    Column("resource_type", Text, primary_key=True),
    Column("account_id", CHAR(12), primary_key=True),
    Column("region", Text, primary_key=True),
    Column("resource_id", Text, primary_key=True),
    Column("arn", Text, nullable=False),
    Column("name", Text),
    Column("tags", JSONB, nullable=False),
    Column("relations", JSONB, nullable=False),
    Column("data", JSONB, nullable=False),
    Column("content_hash", LargeBinary, nullable=False),
    Column("observed_at", TS, nullable=False),
    Column("first_seen_at", TS, nullable=False),
    Column("last_changed_at", TS, nullable=False),
    Column("deleted_at", TS),
    Column("last_job_id", Text),
    postgresql_partition_by="LIST (resource_type)",
)

scope_state = Table(
    "scope_state",
    metadata,
    Column("account_id", CHAR(12), primary_key=True),
    Column("region", Text, primary_key=True),
    Column("resource_type", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("resource_count", Integer),
    Column("last_attempt_at", TS, nullable=False),
    Column("last_success_at", TS),
    Column("last_job_id", Text),
    Column("error_code", Text),
    Column("error_message", Text),
)

account_region = Table(
    "account_region",
    metadata,
    Column("account_id", CHAR(12), primary_key=True),
    Column("region", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("checked_at", TS, nullable=False),
)

scan_job = Table(
    "scan_job",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column("trigger", Text, nullable=False),
    Column("scope", JSONB, nullable=False),
    Column("scope_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("request_id", Text),
    Column("flags", JSONB, nullable=False),
    Column("stats", JSONB, nullable=False),
    Column("error", Text),
    Column("created_at", TS, nullable=False),
    Column("started_at", TS),
    Column("finished_at", TS),
    Column("deadline_at", TS),
    Column("requested_by", Text),  # principal.subject; NULL for scheduled jobs
)

# what account-directory last said about each account; rows stay when an account stops
# being listed (it is then just not scanned), so its resources keep their EIM
account = Table(
    "account",
    metadata,
    Column("account_id", CHAR(12), primary_key=True),
    Column("name", Text),
    Column("eim", Text),
    Column("account_type", Text),
    Column("status", Text, nullable=False),
    Column("listed_at", TS, nullable=False),
)

api_key = Table(
    "api_key",
    metadata,
    Column("key_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("key_hash", LargeBinary, nullable=False),
    Column("roles", ARRAY(Text), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("expires_at", TS),
    Column("revoked_at", TS),
    Column("last_used_at", TS),
)

scan_task = Table(
    "scan_task",
    metadata,
    Column("task_id", Text, primary_key=True),
    Column("job_id", Text, ForeignKey("scan_job.job_id"), nullable=False),
    Column("account_id", CHAR(12), nullable=False),
    Column("region", Text, nullable=False),
    Column("resource_types", ARRAY(Text)),
    Column("resource_ids", ARRAY(Text)),
    Column("queue", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("result", JSONB, nullable=False),
    Column("error_code", Text),
    Column("error_message", Text),
    Column("worker_id", Text),
    Column("queued_at", TS, nullable=False),
    Column("started_at", TS),
    Column("finished_at", TS),
    Column("updated_at", TS, nullable=False),
)

ACTIVE_JOB_STATUSES = ("queued", "running")


@dataclass(frozen=True, slots=True)
class Claim:
    state: Literal["claimed", "busy", "done"]
    task: ScanTask | None


def new_id() -> str:
    return str(ULID())


def partition_name(resource_type: str) -> str:
    """``AWS::EC2::Instance`` -> ``r_ec2_instance``."""
    parts = resource_type.removeprefix("AWS::").split("::")
    snake = [re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", p) for p in parts]
    return "r_" + "_".join(s.lower() for s in snake)


# --------------------------------------------------------------------------- engines


def resolve_database_url(settings: CommonSettings, secret: Mapping[str, Any] | None = None) -> str:
    """Local runs use ``database_url``; AWS uses the RDS-managed secret for the credentials and
    ``database_host``/``port``/``name`` from settings when the secret does not carry them."""
    if settings.database_url is not None:
        return settings.database_url.get_secret_value()
    if secret is None:
        raise ValueError("database_url or the database secret is required")
    host = secret.get("host") or settings.database_host
    if not host:
        raise ValueError("database host missing (set INVENTORY_DATABASE_HOST)")
    user = quote_plus(str(secret["username"]))
    password = quote_plus(str(secret["password"]))
    port = secret.get("port") or settings.database_port
    name = secret.get("dbname") or settings.database_name
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


Credentials = Callable[[], tuple[str, str]]


def connect_args(application_name: str) -> dict[str, Any]:
    # RDS Proxy rejects the "options" startup parameter, and psycopg's automatic server-side
    # prepared statements would pin proxy sessions; both are avoided here.
    return {"application_name": application_name, "prepare_threshold": None}


def install_session_settings(
    engine: Engine, statement_timeout_ms: int, credentials: Credentials | None = None
) -> None:
    """Per-connection setup: statement timeout, and fresh credentials for every new connection
    so an automatic password rotation does not require restarting the process."""

    @event.listens_for(engine, "connect")
    def _session(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        cursor.close()
        dbapi_connection.commit()

    if credentials is not None:

        @event.listens_for(engine, "do_connect")
        def _credentials(_dialect: Any, _record: Any, _cargs: Any, cparams: dict[str, Any]) -> None:
            cparams["user"], cparams["password"] = credentials()


def make_engine(
    url: str,
    *,
    application_name: str,
    statement_timeout_ms: int,
    pool_size: int = 5,
    max_overflow: int = 5,
    credentials: Credentials | None = None,
) -> Engine:
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args=connect_args(application_name),
    )
    install_session_settings(engine, statement_timeout_ms, credentials)
    return engine


# --------------------------------------------------------------------------- resources


@dataclass(frozen=True, slots=True)
class IndexEntry:
    content_hash: bytes
    observed_at: datetime
    deleted: bool
    arn: str = ""


class ResourceRepository:
    def load_index(
        self, conn: Connection, resource_type: str, account_id: str, region: str
    ) -> dict[str, IndexEntry]:
        rows = conn.execute(
            select(
                resource.c.resource_id,
                resource.c.content_hash,
                resource.c.observed_at,
                resource.c.deleted_at,
                resource.c.arn,
            ).where(
                resource.c.resource_type == resource_type,
                resource.c.account_id == account_id,
                resource.c.region == region,
            )
        )
        return {
            r.resource_id: IndexEntry(
                bytes(r.content_hash), r.observed_at, r.deleted_at is not None, r.arn
            )
            for r in rows
        }

    def upsert(
        self, conn: Connection, records: Sequence[ResourceRecord], job_id: str | None
    ) -> int:
        """Insert or update; returns rows written (a newer stored observation is kept)."""
        written = 0
        for start in range(0, len(records), UPSERT_BATCH):
            batch = records[start : start + UPSERT_BATCH]
            stmt = pg_insert(resource).values([_resource_row(r, job_id) for r in batch])
            excluded = stmt.excluded
            changed = or_(
                resource.c.content_hash != excluded.content_hash,
                resource.c.deleted_at.is_not(None),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    resource.c.resource_type,
                    resource.c.account_id,
                    resource.c.region,
                    resource.c.resource_id,
                ],
                set_={
                    "arn": excluded.arn,
                    "name": excluded.name,
                    "tags": excluded.tags,
                    "relations": excluded.relations,
                    "data": excluded.data,
                    "last_changed_at": case(
                        (changed, excluded.observed_at), else_=resource.c.last_changed_at
                    ),
                    "content_hash": excluded.content_hash,
                    "observed_at": excluded.observed_at,
                    "deleted_at": None,
                    "last_job_id": excluded.last_job_id,
                },
                where=resource.c.observed_at < excluded.observed_at,
            )
            written += int(conn.execute(stmt).rowcount)
        return written

    def soft_delete(
        self,
        conn: Connection,
        resource_type: str,
        account_id: str,
        region: str,
        resource_ids: Iterable[str],
        *,
        before: datetime,
        job_id: str | None,
        now: datetime | None = None,
    ) -> int:
        ids = list(resource_ids)
        if not ids:
            return 0
        when = now or utcnow()
        result = conn.execute(
            update(resource)
            .where(
                resource.c.resource_type == resource_type,
                resource.c.account_id == account_id,
                resource.c.region == region,
                resource.c.resource_id.in_(ids),
                resource.c.deleted_at.is_(None),
                resource.c.observed_at < before,
            )
            # observed_at moves too: an older scan must not resurrect a deletion seen later
            .values(deleted_at=when, last_changed_at=when, observed_at=when, last_job_id=job_id)
        )
        return int(result.rowcount)

    def get(
        self, conn: Connection, key: ResourceKey, *, for_update: bool = False
    ) -> RowMapping | None:
        stmt = select(resource).where(*_key_clause(key))
        if for_update:
            stmt = stmt.with_for_update()
        return conn.execute(stmt).mappings().first()

    def find_account(
        self,
        conn: Connection,
        resource_type: str,
        resource_id: str,
        region: str | None = None,
        *,
        id_suffix: bool = False,
    ) -> list[tuple[str, str, str]]:
        """Return (account_id, region, resource_id) for resources whose ARN lacks them."""
        id_clause = (
            resource.c.resource_id.endswith(f"/{resource_id}", autoescape=True)
            if id_suffix
            else resource.c.resource_id == resource_id
        )
        stmt = select(resource.c.account_id, resource.c.region, resource.c.resource_id).where(
            resource.c.resource_type == resource_type, id_clause
        )
        if region:
            stmt = stmt.where(resource.c.region == region)
        rows = conn.execute(
            stmt.order_by(resource.c.deleted_at.is_not(None), resource.c.account_id)
        )
        return [(r.account_id, r.region, r.resource_id) for r in rows]

    def purge_deleted(self, conn: Connection, older_than: datetime, batch_size: int = 5000) -> int:
        keys = (
            select(
                resource.c.resource_type,
                resource.c.account_id,
                resource.c.region,
                resource.c.resource_id,
            )
            .where(resource.c.deleted_at < older_than)
            .limit(batch_size)
        )
        result = conn.execute(
            delete(resource).where(
                tuple_(
                    resource.c.resource_type,
                    resource.c.account_id,
                    resource.c.region,
                    resource.c.resource_id,
                ).in_(keys)
            )
        )
        return int(result.rowcount)


def _key_clause(key: ResourceKey) -> tuple[Any, ...]:
    return (
        resource.c.resource_type == key.resource_type,
        resource.c.account_id == key.account_id,
        resource.c.region == key.region,
        resource.c.resource_id == key.resource_id,
    )


def _resource_row(record: ResourceRecord, job_id: str | None) -> dict[str, Any]:
    key = record.key
    return {
        "resource_type": key.resource_type,
        "account_id": key.account_id,
        "region": key.region,
        "resource_id": key.resource_id,
        "arn": record.arn,
        "name": record.name,
        "tags": dict(record.tags),
        "relations": dict(record.relations),
        "data": dict(record.data),
        "content_hash": record.content_hash,
        "observed_at": record.observed_at,
        "first_seen_at": record.observed_at,
        "last_changed_at": record.observed_at,
        "deleted_at": None,
        "last_job_id": job_id,
    }


# --------------------------------------------------------------------------- scope state


class ScopeStateRepository:
    def upsert(
        self,
        conn: Connection,
        *,
        account_id: str,
        region: str,
        resource_type: str,
        status: TypeStatus,
        attempt_at: datetime,
        job_id: str | None,
        resource_count: int | None = None,
        success_at: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        stmt = pg_insert(scope_state).values(
            account_id=account_id,
            region=region,
            resource_type=resource_type,
            status=status.value,
            resource_count=resource_count,
            last_attempt_at=attempt_at,
            last_success_at=success_at,
            last_job_id=job_id,
            error_code=error_code,
            error_message=error_message,
        )
        excluded = stmt.excluded
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    scope_state.c.account_id,
                    scope_state.c.region,
                    scope_state.c.resource_type,
                ],
                set_={
                    "status": excluded.status,
                    "resource_count": func.coalesce(
                        excluded.resource_count, scope_state.c.resource_count
                    ),
                    "last_attempt_at": excluded.last_attempt_at,
                    "last_success_at": func.coalesce(
                        excluded.last_success_at, scope_state.c.last_success_at
                    ),
                    "last_job_id": excluded.last_job_id,
                    "error_code": excluded.error_code,
                    "error_message": excluded.error_message,
                },
            )
        )

    def list_for_account(self, conn: Connection, account_id: str) -> list[RowMapping]:
        rows = conn.execute(
            select(scope_state)
            .where(scope_state.c.account_id == account_id)
            .order_by(scope_state.c.region, scope_state.c.resource_type)
        )
        return list(rows.mappings())


# --------------------------------------------------------------------------- account regions


class AccountRegionRepository:
    def get_enabled(
        self,
        conn: Connection,
        account_id: str,
        *,
        max_age: timedelta,
        regions: Iterable[str],
        now: datetime | None = None,
    ) -> list[str] | None:
        """Cached enabled regions, or None when missing, older than ``max_age`` or checked
        against a different set of scan regions (a region was added or removed)."""
        rows = list(
            conn.execute(
                select(
                    account_region.c.region, account_region.c.enabled, account_region.c.checked_at
                ).where(account_region.c.account_id == account_id)
            )
        )
        if not rows:
            return None
        cutoff = (now or utcnow()) - max_age
        if min(r.checked_at for r in rows) < cutoff or {r.region for r in rows} != set(regions):
            return None
        return sorted(r.region for r in rows if r.enabled)

    def replace(
        self,
        conn: Connection,
        account_id: str,
        *,
        enabled: Iterable[str],
        supported: Iterable[str],
        checked_at: datetime,
    ) -> None:
        enabled_set = set(enabled)
        conn.execute(delete(account_region).where(account_region.c.account_id == account_id))
        rows = [
            {
                "account_id": account_id,
                "region": r,
                "enabled": r in enabled_set,
                "checked_at": checked_at,
            }
            for r in supported
        ]
        if rows:
            conn.execute(account_region.insert(), rows)

    def list_for_account(self, conn: Connection, account_id: str) -> list[RowMapping]:
        return list(
            conn.execute(
                select(account_region).where(account_region.c.account_id == account_id)
            ).mappings()
        )


# --------------------------------------------------------------------------- jobs & tasks


class JobRepository:
    def create_job(
        self,
        conn: Connection,
        *,
        trigger: Trigger,
        scope: ScanScope,
        request_id: str | None = None,
        requested_by: str | None = None,
        flags: Mapping[str, Any] | None = None,
        deadline_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[ScanJob, bool]:
        """Create a job, or return the active job with the same scope (created=False)."""
        when = now or utcnow()
        scope_hash = scope.scope_hash()
        for _ in range(3):
            stmt = (
                pg_insert(scan_job)
                .values(
                    job_id=new_id(),
                    trigger=trigger,
                    scope=scope.as_json(),
                    scope_hash=scope_hash,
                    status="queued",
                    request_id=request_id,
                    requested_by=requested_by,
                    flags=dict(flags or {}),
                    stats={},
                    created_at=when,
                    deadline_at=deadline_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[scan_job.c.scope_hash],
                    # must be a literal to match the partial unique index predicate
                    index_where=text("status IN ('queued', 'running')"),
                )
                .returning(scan_job)
            )
            row = conn.execute(stmt).mappings().first()
            if row is not None:
                return ScanJob.model_validate(dict(row)), True
            active = (
                conn.execute(
                    select(scan_job).where(
                        scan_job.c.scope_hash == scope_hash,
                        scan_job.c.status.in_(ACTIVE_JOB_STATUSES),
                    )
                )
                .mappings()
                .first()
            )
            if active is not None:
                return ScanJob.model_validate(dict(active)), False
        raise RuntimeError("could not create or find an active job for the scope")

    def has_schedule_job_since(self, conn: Connection, since: datetime) -> bool:
        return (
            conn.execute(
                select(scan_job.c.job_id)
                .where(scan_job.c.trigger == "schedule", scan_job.c.created_at >= since)
                .limit(1)
            ).first()
            is not None
        )

    def get_job(self, conn: Connection, job_id: str) -> ScanJob | None:
        row = conn.execute(select(scan_job).where(scan_job.c.job_id == job_id)).mappings().first()
        return ScanJob.model_validate(dict(row)) if row else None

    def fail_job(
        self, conn: Connection, job_id: str, error: str, now: datetime | None = None
    ) -> None:
        """A job that could not even be started (e.g. expansion failed before any task)."""
        when = now or utcnow()
        conn.execute(
            update(scan_job)
            .where(scan_job.c.job_id == job_id, scan_job.c.status.in_(ACTIVE_JOB_STATUSES))
            .values(status="failed", error=error[:2000], finished_at=when)
        )

    def set_job_error(self, conn: Connection, job_id: str, error: str) -> None:
        conn.execute(update(scan_job).where(scan_job.c.job_id == job_id).values(error=error))

    def create_tasks(
        self, conn: Connection, job_id: str, tasks: Sequence[NewTask], now: datetime | None = None
    ) -> list[ScanTask]:
        if not tasks:
            return []
        when = now or utcnow()
        rows: list[dict[str, Any]] = [
            {
                "task_id": new_id(),
                "job_id": job_id,
                "account_id": t.account_id,
                "region": t.region,
                "resource_types": t.resource_types,
                "resource_ids": t.resource_ids,
                "queue": t.queue,
                "status": t.status,
                "attempts": 0,
                "result": {},
                "error_code": t.error_code,
                "error_message": t.error_message,
                "queued_at": when,
                "finished_at": when if t.status in TERMINAL_TASK_STATUSES else None,
                "updated_at": when,
            }
            for t in tasks
        ]
        stmt = (
            pg_insert(scan_task)
            .values(rows)
            # a retried planner batch must not create the same scheduled task twice
            .on_conflict_do_nothing(
                index_elements=[scan_task.c.job_id, scan_task.c.account_id, scan_task.c.region],
                index_where=text("queue = 'scheduled'"),
            )
            .returning(scan_task)
        )
        inserted = conn.execute(stmt).mappings()
        return [ScanTask.model_validate(dict(r)) for r in inserted]

    def accounts_with_tasks(
        self, conn: Connection, job_id: str, accounts: Sequence[str]
    ) -> set[str]:
        rows = conn.execute(
            select(scan_task.c.account_id)
            .where(scan_task.c.job_id == job_id, scan_task.c.account_id.in_(list(accounts)))
            .distinct()
        )
        return {r.account_id for r in rows}

    def get_task(self, conn: Connection, task_id: str) -> ScanTask | None:
        row = (
            conn.execute(select(scan_task).where(scan_task.c.task_id == task_id)).mappings().first()
        )
        return ScanTask.model_validate(dict(row)) if row else None

    def list_tasks(self, conn: Connection, job_id: str) -> list[ScanTask]:
        rows = conn.execute(
            select(scan_task).where(scan_task.c.job_id == job_id).order_by(scan_task.c.task_id)
        ).mappings()
        return [ScanTask.model_validate(dict(r)) for r in rows]

    def claim(
        self,
        conn: Connection,
        task_id: str,
        worker_id: str,
        *,
        attempt: int,
        stale_before: datetime,
        now: datetime | None = None,
    ) -> Claim:
        """Claim a task for message ``attempt``.

        CLAIMED: run it. BUSY: another worker is running this attempt and its heartbeat is
        fresh -- keep the message so SQS redelivers it later. DONE: finished, superseded by
        a later attempt, or unknown -- drop the message.
        """
        when = now or utcnow()
        c = scan_task.c
        row = (
            conn.execute(
                update(scan_task)
                .where(
                    c.task_id == task_id,
                    or_(
                        and_(c.status.in_(("queued", "retrying")), c.attempts == attempt - 1),
                        and_(
                            c.status == "running",
                            c.attempts == attempt,
                            c.updated_at < stale_before,
                        ),
                    ),
                )
                .values(
                    status="running",
                    # a stale takeover continues the same attempt
                    attempts=case((c.status == "running", c.attempts), else_=c.attempts + 1),
                    worker_id=worker_id,
                    started_at=func.coalesce(c.started_at, when),
                    updated_at=when,
                )
                .returning(scan_task)
            )
            .mappings()
            .first()
        )
        if row is not None:
            self._mark_job_running(conn, row["job_id"], when)
            return Claim("claimed", ScanTask.model_validate(dict(row)))
        current = conn.execute(select(c.status, c.attempts).where(c.task_id == task_id)).first()
        if current is not None and current.status == "running" and current.attempts == attempt:
            return Claim("busy", None)
        return Claim("done", None)

    def claim_task(
        self,
        conn: Connection,
        task_id: str,
        worker_id: str,
        *,
        stale_before: datetime,
        attempt: int = 1,
        now: datetime | None = None,
    ) -> ScanTask | None:
        claim = self.claim(
            conn, task_id, worker_id, attempt=attempt, stale_before=stale_before, now=now
        )
        return claim.task

    def heartbeat(self, conn: Connection, task_id: str, now: datetime | None = None) -> None:
        conn.execute(
            update(scan_task)
            .where(scan_task.c.task_id == task_id, scan_task.c.status == "running")
            .values(updated_at=now or utcnow())
        )

    def mark_retrying(
        self,
        conn: Connection,
        task_id: str,
        result: Mapping[str, Any],
        *,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Running -> retrying. False when the task is no longer ours (taken over/timed out)."""
        stmt = update(scan_task).where(
            scan_task.c.task_id == task_id, scan_task.c.status == "running"
        )
        if worker_id is not None:
            stmt = stmt.where(scan_task.c.worker_id == worker_id)
        updated = conn.execute(
            stmt.values(status="retrying", result=dict(result), updated_at=now or utcnow())
        )
        return bool(updated.rowcount)

    def finish_task(
        self,
        conn: Connection,
        task_id: str,
        status: TaskStatus,
        *,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        worker_id: str | None = None,
        now: datetime | None = None,
    ) -> ScanJob | None:
        """Move a task to a terminal status; never overwrites a task that already finished.

        With ``worker_id``, only the worker currently owning the task may finish it.
        Returns the (possibly finished) job, or None when nothing was updated.
        """
        when = now or utcnow()
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "finished_at": when,
            "updated_at": when,
        }
        if result is not None:
            values["result"] = dict(result)
        stmt = update(scan_task).where(
            scan_task.c.task_id == task_id,
            scan_task.c.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
        )
        if worker_id is not None:
            stmt = stmt.where(scan_task.c.worker_id == worker_id)
        job_id = conn.execute(
            stmt.values(**values).returning(scan_task.c.job_id)
        ).scalar_one_or_none()
        return self.refresh_job_status(conn, job_id, now=when) if job_id else None

    def refresh_job_status(
        self, conn: Connection, job_id: str, now: datetime | None = None
    ) -> ScanJob | None:
        """Finish the job once every task is terminal.

        The job row is locked *before* looking at the tasks: concurrent finishers of the
        last tasks are serialised, so the last one always sees all others committed.
        """
        when = now or utcnow()
        locked = (
            conn.execute(select(scan_job).where(scan_job.c.job_id == job_id).with_for_update())
            .mappings()
            .first()
        )
        if locked is None:
            return None
        job = ScanJob.model_validate(dict(locked))
        if job.status not in ACTIVE_JOB_STATUSES:
            return job
        # A scheduled job is dispatched in batches; it cannot finish before the last batch.
        if job.flags.get("dispatching") or self._has_unfinished_tasks(conn, job_id):
            return job
        counts = self.task_counts(conn, job_id)
        status = terminal_job_status(list(counts))
        if status == "succeeded" and job.error:
            status = "partial"  # e.g. some planner batches could not be dispatched
        row = (
            conn.execute(
                update(scan_job)
                .where(scan_job.c.job_id == job_id)
                .values(
                    status=status,
                    finished_at=when,
                    started_at=func.coalesce(scan_job.c.started_at, when),
                    stats=self.aggregate_stats(conn, job_id, counts),
                )
                .returning(scan_job)
            )
            .mappings()
            .one()
        )
        return ScanJob.model_validate(dict(row))

    def aggregate_stats(
        self, conn: Connection, job_id: str, counts: Mapping[str, int] | None = None
    ) -> dict[str, Any]:
        """Job statistics computed in SQL (no per-task rows are loaded)."""
        by_status = dict(counts if counts is not None else self.task_counts(conn, job_id))
        created, updated_, deleted, unchanged = conn.execute(
            text(
                """
                SELECT coalesce(sum((r.value->>'created')::int), 0),
                       coalesce(sum((r.value->>'updated')::int), 0),
                       coalesce(sum((r.value->>'deleted')::int), 0),
                       coalesce(sum((r.value->>'unchanged')::int), 0)
                FROM scan_task t CROSS JOIN LATERAL jsonb_each(t.result) AS r(key, value)
                WHERE t.job_id = :job_id AND jsonb_typeof(r.value) = 'object'
                """
            ),
            {"job_id": job_id},
        ).one()
        return {
            "tasks_total": sum(by_status.values()),
            "tasks_done": sum(n for s, n in by_status.items() if s in TERMINAL_TASK_STATUSES),
            "by_status": by_status,
            "created": int(created),
            "updated": int(updated_),
            "deleted": int(deleted),
            "unchanged": int(unchanged),
        }

    @staticmethod
    def _has_unfinished_tasks(conn: Connection, job_id: str) -> bool:
        return (
            conn.execute(
                select(scan_task.c.task_id)
                .where(
                    scan_task.c.job_id == job_id,
                    scan_task.c.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
                )
                .limit(1)
            ).first()
            is not None
        )

    def mark_dispatched(
        self, conn: Connection, job_id: str, now: datetime | None = None
    ) -> ScanJob | None:
        """Clear the ``dispatching`` flag so the job can finish once its tasks are done."""
        conn.execute(
            update(scan_job)
            .where(scan_job.c.job_id == job_id)
            .values(flags=scan_job.c.flags.op("-")(literal("dispatching", Text)))
        )
        return self.refresh_job_status(conn, job_id, now=now)

    def timeout_job(
        self, conn: Connection, job_id: str, now: datetime | None = None
    ) -> ScanJob | None:
        """Deadline passed: unfinished tasks and the job become timed_out.

        If every task had in fact finished, the job gets its real terminal status instead.
        """
        when = now or utcnow()
        conn.execute(select(scan_job.c.job_id).where(scan_job.c.job_id == job_id).with_for_update())
        timed_out = conn.execute(
            update(scan_task)
            .where(
                scan_task.c.job_id == job_id,
                scan_task.c.status.not_in(tuple(TERMINAL_TASK_STATUSES)),
            )
            .values(status="timed_out", finished_at=when, updated_at=when, error_code="TIMED_OUT")
        ).rowcount
        if not timed_out:
            conn.execute(
                update(scan_job)
                .where(scan_job.c.job_id == job_id)
                .values(flags=scan_job.c.flags.op("-")(literal("dispatching", Text)))
            )
            return self.refresh_job_status(conn, job_id, now=when)
        row = (
            conn.execute(
                update(scan_job)
                .where(scan_job.c.job_id == job_id, scan_job.c.status.in_(ACTIVE_JOB_STATUSES))
                .values(
                    status="timed_out",
                    finished_at=when,
                    stats=self.aggregate_stats(conn, job_id),
                )
                .returning(scan_job)
            )
            .mappings()
            .first()
        )
        return ScanJob.model_validate(dict(row)) if row else self.get_job(conn, job_id)

    def timeout_stale_tasks(
        self, conn: Connection, stale_before: datetime, now: datetime | None = None
    ) -> list[str]:
        when = now or utcnow()
        job_ids = conn.execute(
            update(scan_task)
            .where(
                # only running tasks heartbeat; queued/retrying ones wait in SQS (possibly
                # behind a long backlog) and are covered by the job deadline instead
                scan_task.c.status == "running",
                scan_task.c.updated_at < stale_before,
            )
            .values(status="timed_out", finished_at=when, updated_at=when, error_code="TIMED_OUT")
            .returning(scan_task.c.job_id)
        ).scalars()
        affected = sorted(set(job_ids))
        for job_id in affected:
            self.refresh_job_status(conn, job_id, now=when)
        return affected

    def overdue_jobs(self, conn: Connection, now: datetime | None = None) -> list[str]:
        when = now or utcnow()
        return list(
            conn.execute(
                select(scan_job.c.job_id).where(
                    scan_job.c.status.in_(ACTIVE_JOB_STATUSES), scan_job.c.deadline_at < when
                )
            ).scalars()
        )

    def task_counts(self, conn: Connection, job_id: str) -> dict[str, int]:
        rows = conn.execute(
            select(scan_task.c.status, func.count())
            .where(scan_task.c.job_id == job_id)
            .group_by(scan_task.c.status)
        )
        return {status: int(count) for status, count in rows}

    @staticmethod
    def _mark_job_running(conn: Connection, job_id: str, when: datetime) -> None:
        conn.execute(
            update(scan_job)
            .where(scan_job.c.job_id == job_id, scan_job.c.status == "queued")
            .values(status="running", started_at=when)
        )


def terminal_job_status(task_statuses: Sequence[str]) -> str:
    counted = [s for s in task_statuses if s != "auth_denied"]
    if all(s == "succeeded" for s in counted):
        return "succeeded"
    if all(s == "failed" for s in counted):
        return "failed"
    return "partial"


# --------------------------------------------------------------------------- accounts


class AccountRepository:
    def upsert(self, conn: Connection, accounts: Sequence[DirectoryAccount], now: datetime) -> None:
        rows = [
            {
                "account_id": a.account_id,
                "name": a.name,
                "eim": a.eim,
                "account_type": a.account_type,
                "status": a.status,
                "listed_at": now,
            }
            for a in accounts
        ]
        for start in range(0, len(rows), UPSERT_BATCH):
            stmt = pg_insert(account).values(rows[start : start + UPSERT_BATCH])
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[account.c.account_id],
                    set_={
                        k: stmt.excluded[k]
                        for k in ("name", "eim", "account_type", "status", "listed_at")
                    },
                )
            )


# --------------------------------------------------------------------------- API keys


class ApiKeyRepository:
    """API keys for service callers (spec 10); only the SHA-256 of the secret is stored."""

    def create(
        self,
        conn: Connection,
        *,
        key_id: str,
        name: str,
        key_hash: bytes,
        roles: Sequence[str],
        created_by: str,
        expires_at: datetime | None,
        now: datetime | None = None,
    ) -> RowMapping:
        row = conn.execute(
            api_key.insert()
            .values(
                key_id=key_id,
                name=name,
                key_hash=key_hash,
                roles=list(roles),
                created_by=created_by,
                created_at=now or utcnow(),
                expires_at=expires_at,
            )
            .returning(api_key)
        ).mappings()
        return row.one()

    def get(self, conn: Connection, key_id: str) -> RowMapping | None:
        return conn.execute(select(api_key).where(api_key.c.key_id == key_id)).mappings().first()

    def list(self, conn: Connection) -> list[RowMapping]:
        rows = conn.execute(select(api_key).order_by(api_key.c.created_at.desc())).mappings()
        return list(rows)

    def revoke(self, conn: Connection, key_id: str, now: datetime | None = None) -> bool:
        result = conn.execute(
            update(api_key)
            .where(api_key.c.key_id == key_id, api_key.c.revoked_at.is_(None))
            .values(revoked_at=now or utcnow())
        )
        return result.rowcount > 0

    def touch(self, conn: Connection, key_id: str, now: datetime, min_interval: timedelta) -> None:
        """Record use, at most once per ``min_interval`` (avoids a write per request)."""
        conn.execute(
            update(api_key)
            .where(
                api_key.c.key_id == key_id,
                or_(api_key.c.last_used_at.is_(None), api_key.c.last_used_at < now - min_interval),
            )
            .values(last_used_at=now)
        )
