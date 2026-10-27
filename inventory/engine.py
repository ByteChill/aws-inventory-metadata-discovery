"""Scan execution: collect -> reconcile -> write. Shared by the worker and the API."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from pydantic import ValidationError
from sqlalchemy import Engine

from inventory.aws import ClientFactory, EndpointResolver
from inventory.collectors.base import Collector, CollectorContext, Registry
from inventory.db import IndexEntry, JobRepository
from inventory.errors import (
    ACCESS_DENIED_CODES,
    REGION_NOT_ENABLED_CODES,
    THROTTLE_CODES,
    AccessDeniedError,
    InventoryError,
)
from inventory.models import (
    ChangeEvent,
    ChangeSet,
    ErrorInfo,
    KeepExisting,
    RefreshStatus,
    ResourceKey,
    ResourceRecord,
    ScanTaskMessage,
    TypeResult,
    TypeScope,
    TypeStatus,
    utcnow,
)
from inventory.observability import context_scope, metric, submit_with_context
from inventory.transport import CredentialProvider, Dispatcher, ResultSink

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- reconcile


def reconcile(
    scope: TypeScope,
    index: Mapping[str, IndexEntry],
    current: Sequence[ResourceRecord],
    kept: Collection[str],
    scan_started_at: datetime,
    job_id: str,
) -> ChangeSet:
    """Diff a complete listing against the stored index. Pure function."""
    seen: set[str] = set()
    upserts: list[ResourceRecord] = []
    events: list[ChangeEvent] = []
    created = updated = unchanged = 0
    for rid in kept:
        entry = index.get(rid)
        if entry is None or entry.deleted:
            raise ValueError(f"KeepExisting for unknown or deleted {scope.resource_type} {rid}")
    for record in current:
        rid = record.key.resource_id
        if rid in seen or rid in kept:
            raise ValueError(f"duplicate resource id {rid} for {record.key.resource_type}")
        seen.add(rid)
        entry = index.get(rid)
        if entry is None or entry.deleted:
            created += 1
            change_type = "created"
        elif entry.content_hash != record.content_hash:
            updated += 1
            change_type = "updated"
        else:
            unchanged += 1
            continue
        upserts.append(record)
        events.append(
            ChangeEvent(
                key=record.key,
                arn=record.arn,
                change_type=change_type,  # type: ignore[arg-type]
                changed_at=record.observed_at,
                job_id=job_id,
                old_hash=entry.content_hash if entry else None,
                new_hash=record.content_hash,
                data=record.data,
            )
        )
    unchanged += len(kept)

    deleted_ids = sorted(
        rid
        for rid, entry in index.items()
        if not entry.deleted and rid not in seen and rid not in kept
    )
    events.extend(
        ChangeEvent(
            key=ResourceKey(scope.resource_type, scope.account_id, scope.region, rid),
            arn=index[rid].arn,
            change_type="deleted",
            changed_at=scan_started_at,
            job_id=job_id,
            old_hash=index[rid].content_hash,
            new_hash=None,
            data=None,
        )
        for rid in deleted_ids
    )
    return ChangeSet(
        upserts=upserts,
        deleted_ids=deleted_ids,
        created=created,
        updated=updated,
        unchanged=unchanged,
        events=events,
        scan_started_at=scan_started_at,
    )


# --------------------------------------------------------------------------- errors


def classify(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, InventoryError):
        return ErrorInfo(exc.code, exc.message, exc.retryable)
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        message = str(exc.response.get("Error", {}).get("Message", code))[:500]
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in THROTTLE_CODES:
            return ErrorInfo("THROTTLED", message, True)
        if code in ACCESS_DENIED_CODES:
            return ErrorInfo("API_ACCESS_DENIED", message, False)
        if code in REGION_NOT_ENABLED_CODES:
            return ErrorInfo("REGION_NOT_ENABLED", message, False)
        if status >= 500:
            return ErrorInfo("AWS_SERVER_ERROR", message, True)
        return ErrorInfo(f"AWS_{code}" if code else "AWS_CLIENT_ERROR", message, False)
    if isinstance(exc, EndpointConnectionError | ConnectTimeoutError | ReadTimeoutError):
        return ErrorInfo("NETWORK_ERROR", type(exc).__name__, True)
    if isinstance(exc, BotoCoreError):
        return ErrorInfo("AWS_SDK_ERROR", type(exc).__name__, True)
    return ErrorInfo("INTERNAL_ERROR", type(exc).__name__, True)


# --------------------------------------------------------------------------- engine


@dataclass
class RefreshOutcome:
    status: RefreshStatus
    record: ResourceRecord | None = None
    error: ErrorInfo | None = None


@dataclass
class _TaskState:
    upstream: dict[str, list[ResourceRecord]] = field(default_factory=dict)
    failed: dict[str, ErrorInfo] = field(default_factory=dict)
    memo: dict[str, Any] = field(default_factory=dict)
    memo_lock: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ScanEngine:
    def __init__(
        self,
        registry: Registry,
        clients: ClientFactory,
        resolver: EndpointResolver,
        credentials: CredentialProvider,
        sink: ResultSink,
        *,
        type_concurrency: int = 4,
        collector_concurrency: int = 4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.registry = registry
        self.clients = clients
        self.resolver = resolver
        self.credentials = credentials
        self.sink = sink
        self.type_concurrency = type_concurrency
        self.collector_concurrency = collector_concurrency
        self.clock = clock

    # ------------------------------------------------------------------ full scans
    def scan(self, task: ScanTaskMessage) -> dict[str, TypeResult]:
        with context_scope(
            job_id=task.job_id,
            task_id=task.task_id,
            account_id=task.account_id,
            region=task.region,
        ):
            return self._scan(task)

    def _scan(self, task: ScanTaskMessage) -> dict[str, TypeResult]:
        results: dict[str, TypeResult] = {}
        # Expand dependencies first, then check endpoints, so an auto-added dependency
        # without an endpoint is skipped instead of being called anyway.
        groups = self.registry.groups([c.resource_type for c in self._requested_types(task)])
        every = [cls for group in groups for cls in group]
        available, skipped = self._split_by_endpoint(every, task.region)
        for cls in skipped:
            results[cls.resource_type] = TypeResult(TypeStatus.SKIPPED_NO_ENDPOINT)
            self._record_skip(task, cls.resource_type, TypeStatus.SKIPPED_NO_ENDPOINT, None)
        if not available:
            return results

        try:
            session = self.credentials.session(task.account_id)
        except Exception as exc:
            denied = isinstance(exc, AccessDeniedError)
            error = classify(exc)
            if denied:
                error = ErrorInfo(error.code, error.message, False)
            status = TypeStatus.AUTH_DENIED if denied else TypeStatus.FAILED
            log.warning("could not obtain credentials", extra={"error_code": error.code})
            for cls in available:
                results[cls.resource_type] = TypeResult.failure(error, status)
                self._record_skip(task, cls.resource_type, status, error)
            return results

        if task.resource_ids is not None:
            target = self.registry.get(task.resource_types[0])  # type: ignore[index]
            if target in available:
                results[target.resource_type] = self._refresh_ids(
                    task, target, session, task.resource_ids
                )
            return results

        state = _TaskState()
        skipped_types = {c.resource_type for c in skipped}
        with ThreadPoolExecutor(max_workers=max(1, self.type_concurrency)) as pool:
            futures = [
                submit_with_context(
                    pool, self._run_group, task, group, session, state, skipped_types
                )
                for group in groups
            ]
            for future in futures:
                results.update(future.result())
        return results

    def _requested_types(self, task: ScanTaskMessage) -> list[type[Collector]]:
        if task.resource_types is not None:
            return [self.registry.get(t) for t in task.resource_types]
        return self.registry.applicable(task.region)

    def _split_by_endpoint(
        self, types: Sequence[type[Collector]], region: str
    ) -> tuple[list[type[Collector]], list[type[Collector]]]:
        available: list[type[Collector]] = []
        skipped: list[type[Collector]] = []
        for cls in types:
            ok = all(self.resolver.has_endpoint(s, region) for s in cls.endpoints())
            (available if ok else skipped).append(cls)
        return available, skipped

    def _has_dependents(self, resource_type: str) -> bool:
        return any(resource_type in c.depends_on for c in self.registry.all())

    def _run_group(
        self,
        task: ScanTaskMessage,
        group: Sequence[type[Collector]],
        session: Any,
        state: _TaskState,
        skipped_types: set[str],
    ) -> dict[str, TypeResult]:
        results: dict[str, TypeResult] = {}
        for cls in group:
            if cls.resource_type in skipped_types:
                continue  # already reported as skipped_no_endpoint
            blocker = next(
                (d for d in cls.depends_on if d in state.failed or d in skipped_types), None
            )
            if blocker is not None:
                upstream_error = state.failed.get(blocker)
                error = ErrorInfo(
                    "UPSTREAM_FAILED",
                    f"dependency {blocker} did not complete",
                    upstream_error.retryable if upstream_error else False,
                )
                with state.lock:
                    state.failed[cls.resource_type] = error
                results[cls.resource_type] = TypeResult.failure(error)
                self._record_skip(task, cls.resource_type, TypeStatus.FAILED, error)
                continue
            results[cls.resource_type] = self._scan_type(task, cls, session, state)
        return results

    def _scan_type(
        self, task: ScanTaskMessage, cls: type[Collector], session: Any, state: _TaskState
    ) -> TypeResult:
        scope = TypeScope(cls.resource_type, task.account_id, task.region)
        started_at = self.clock()
        t0 = time.perf_counter()
        with context_scope(resource_type=cls.resource_type):
            try:
                index = self.sink.load_index(cls.resource_type, task.account_id, task.region)
                ctx = self._context(task.account_id, task.region, session, task.full_refresh, state)
                ctx.known_ids = frozenset(rid for rid, e in index.items() if not e.deleted)
                collector = cls(ctx)
                current: list[ResourceRecord] = []
                kept: set[str] = set()
                with state.lock:
                    upstream = {k: list(v) for k, v in state.upstream.items()}
                for item in collector.list_resources(upstream):
                    if isinstance(item, KeepExisting):
                        if self._has_dependents(cls.resource_type):
                            # downstream collectors only see freshly listed records
                            raise ValueError(
                                f"{cls.resource_type} has dependents and cannot use KeepExisting"
                            )
                        kept.add(item.resource_id)
                    else:
                        # Stamp with the scan start: the data was read no earlier than this,
                        # so a refresh that happened meanwhile always wins the upsert guard.
                        current.append(collector.to_record(item, started_at))
                changes = reconcile(scope, index, current, kept, started_at, task.job_id)
                result = TypeResult(
                    TypeStatus.SUCCEEDED,
                    created=changes.created,
                    updated=changes.updated,
                    deleted=len(changes.deleted_ids),
                    unchanged=changes.unchanged,
                )
                result.duration_ms = int((time.perf_counter() - t0) * 1000)
                self.sink.apply(scope, changes, result, task.job_id, len(current) + len(kept))
                with state.lock:
                    state.upstream[cls.resource_type] = current
            except Exception as exc:
                error = classify(exc)
                with state.lock:
                    state.failed[cls.resource_type] = error
                log.warning(
                    "resource type scan failed",
                    exc_info=error.code == "INTERNAL_ERROR",
                    extra={"error_code": error.code, "retryable": error.retryable},
                )
                result = TypeResult.failure(error)
                result.duration_ms = int((time.perf_counter() - t0) * 1000)
                self._record_skip(task, cls.resource_type, TypeStatus.FAILED, error)
            self._log_type_result(cls.resource_type, result, task.region)
            return result

    def _refresh_ids(
        self,
        task: ScanTaskMessage,
        cls: type[Collector],
        session: Any,
        resource_ids: Sequence[str],
    ) -> TypeResult:
        """Batch refresh by id: no listing, so nothing is inferred as deleted."""
        result = TypeResult(TypeStatus.SUCCEEDED)
        t0 = time.perf_counter()
        errors: list[ErrorInfo] = []
        with context_scope(resource_type=cls.resource_type):
            ctx = self._context(task.account_id, task.region, session, False, _TaskState())
            collector = cls(ctx)
            for rid in resource_ids:
                key = ResourceKey(cls.resource_type, task.account_id, task.region, rid)
                outcome = self._refresh_with(collector, key, task.job_id)
                if outcome.status == "error" and outcome.error:
                    errors.append(outcome.error)
                elif outcome.status == "created":
                    result.created += 1
                elif outcome.status == "updated":
                    result.updated += 1
                elif outcome.status == "deleted":
                    result.deleted += 1
                else:
                    result.unchanged += 1
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        if errors:
            first = errors[0]
            result.status = TypeStatus.FAILED
            result.error_code, result.error_message = first.code, first.message
            result.retryable = all(e.retryable for e in errors)
        self._log_type_result(cls.resource_type, result, task.region)
        return result

    # ------------------------------------------------------------------ single refresh
    def refresh(self, key: ResourceKey, job_id: str) -> RefreshOutcome:
        with context_scope(
            job_id=job_id,
            account_id=key.account_id,
            region=key.region,
            resource_type=key.resource_type,
        ):
            if not self.registry.has(key.resource_type):
                return RefreshOutcome("unsupported_type")
            try:
                cls = self.registry.get(key.resource_type)
                session = self.credentials.session(key.account_id)
                collector = cls(
                    self._context(key.account_id, key.region, session, False, _TaskState())
                )
            except Exception as exc:
                return RefreshOutcome("error", error=classify(exc))
            return self._refresh_with(collector, key, job_id)

    def _refresh_with(self, collector: Collector, key: ResourceKey, job_id: str) -> RefreshOutcome:
        try:
            observed_at = self.clock()  # taken before the read, see _scan_type
            raw = collector.get_resource(key.resource_id)
            record = collector.to_record(raw, observed_at) if raw is not None else None
            status = self.sink.apply_refresh(key, record, job_id)
        except Exception as exc:
            error = classify(exc)
            log.warning(
                "refresh failed", extra={"error_code": error.code, "resource_id": key.resource_id}
            )
            return RefreshOutcome("error", error=error)
        log.info("resource refreshed", extra={"status": status, "resource_id": key.resource_id})
        return RefreshOutcome(status, record=record)

    # ------------------------------------------------------------------ helpers
    def _context(
        self, account_id: str, region: str, session: Any, full_refresh: bool, state: _TaskState
    ) -> CollectorContext:
        return CollectorContext(
            account_id=account_id,
            region=region,
            api_region=self.resolver.api_region(region),
            session=session,
            clients=self.clients,
            full_refresh=full_refresh,
            executor_workers=self.collector_concurrency,
            memo_store=state.memo,
            memo_lock=state.memo_lock,
        )

    def _record_skip(
        self, task: ScanTaskMessage, resource_type: str, status: TypeStatus, error: ErrorInfo | None
    ) -> None:
        try:
            self.sink.record_skip(
                TypeScope(resource_type, task.account_id, task.region), status, task.job_id, error
            )
        except Exception:
            log.exception("failed to record scope state", extra={"resource_type": resource_type})

    @staticmethod
    def _log_type_result(resource_type: str, result: TypeResult, region: str) -> None:
        log.info(
            "resource type scanned",
            extra={
                "resource_type": resource_type,
                "duration_ms": result.duration_ms,
                "type_status": result.status.value,
                "resources_created": result.created,
                "resources_updated": result.updated,
                "resources_deleted": result.deleted,
                "resources_unchanged": result.unchanged,
                "error_code": result.error_code,
            },
        )
        metric(
            "TypeDuration",
            result.duration_ms,
            unit="Milliseconds",
            dims={"ResourceType": resource_type, "Region": region},
        )
        if result.status is TypeStatus.SUCCEEDED:
            for name, value in (
                ("ResourcesCreated", result.created),
                ("ResourcesUpdated", result.updated),
                ("ResourcesDeleted", result.deleted),
            ):
                if value:
                    metric(name, value, dims={"ResourceType": resource_type})


# --------------------------------------------------------------------------- task executor

TASK_KEY = "_task"  # result key for failures that happened before any type ran


class HandleOutcome(StrEnum):
    DELETE = "delete"  # processed (or duplicate): remove the message
    KEEP = "keep"  # leave it for SQS to redeliver (and eventually DLQ)


class _Heartbeat:
    """Calls ``beat`` every ``interval`` seconds on a background thread."""

    def __init__(self, interval: float, beat: Callable[[], None]) -> None:
        self._interval = interval
        self._beat = beat
        self._stop = threading.Event()
        context = contextvars.copy_context()  # keep the log context in the heartbeat thread
        self._thread = threading.Thread(target=context.run, args=(self._run,), daemon=True)

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._beat()
            except Exception:
                log.warning("heartbeat failed", exc_info=True)


class TaskExecutor:
    """Processes one scan-task message, independent of the runtime (ECS or Lambda)."""

    def __init__(
        self,
        engine: ScanEngine,
        db: Engine,
        dispatcher: Dispatcher,
        *,
        worker_id: str,
        max_attempts: int = 3,
        retry_base_delay_seconds: int = 60,
        heartbeat_interval_seconds: float = 120,
        task_stale_minutes: int = 10,
        region: str = "",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.region = region
        self.engine = engine
        self.db = db
        self.dispatcher = dispatcher
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.retry_base_delay = retry_base_delay_seconds
        self.heartbeat_interval = heartbeat_interval_seconds
        self.task_stale = timedelta(minutes=task_stale_minutes)
        self.clock = clock
        self.jobs = JobRepository()

    def handle(self, body: str, extend_visibility: Callable[[], None]) -> HandleOutcome:
        try:
            message = ScanTaskMessage.model_validate_json(body)
        except ValidationError as exc:
            log.error("invalid scan task message", extra={"errors": exc.error_count()})
            metric("BadMessage", dims={"Region": self.region})
            return HandleOutcome.KEEP
        with context_scope(
            request_id=message.request_id or message.job_id,
            job_id=message.job_id,
            task_id=message.task_id,
            account_id=message.account_id,
            region=message.region,
        ):
            return self._handle(message, extend_visibility)

    def _handle(
        self, message: ScanTaskMessage, extend_visibility: Callable[[], None]
    ) -> HandleOutcome:
        started = time.perf_counter()
        with self.db.begin() as conn:
            claim = self.jobs.claim(
                conn,
                message.task_id,
                self.worker_id,
                attempt=message.attempt,
                stale_before=self.clock() - self.task_stale,
            )
        if claim.state == "busy":
            # another worker is running this attempt; let SQS redeliver after the timeout
            log.info("task is being processed elsewhere; message kept")
            return HandleOutcome.KEEP
        if claim.task is None:
            log.info("duplicate or superseded delivery dropped")
            metric("DuplicateDelivery", dims={"Region": self.region})
            return HandleOutcome.DELETE
        task = claim.task
        log.info("task started", extra={"attempt": message.attempt})

        def beat() -> None:
            # independent: a failing visibility call must not stop the DB heartbeat
            try:
                extend_visibility()
            except Exception:
                log.warning("could not extend message visibility", exc_info=True)
            with self.db.begin() as conn:
                self.jobs.heartbeat(conn, message.task_id)

        with _Heartbeat(self.heartbeat_interval, beat):
            try:
                results = self.engine.scan(message)
            except Exception as exc:
                error = classify(exc)
                log.exception("scan task crashed")
                results = {TASK_KEY: TypeResult.failure(error)}

        merged = {k: TypeResult.from_json(v) for k, v in task.result.items()} | results
        if TASK_KEY not in results:
            merged.pop(TASK_KEY, None)  # an earlier crash was superseded by this attempt
        status = self._finish(message, task.queue, merged, results)
        duration_ms = int((time.perf_counter() - started) * 1000)
        # prefixed: "created" etc. are reserved LogRecord attributes
        counts = {
            f"resources_{f}": sum(getattr(r, f) for r in merged.values())
            for f in ("created", "updated", "deleted", "unchanged")
        }
        log.info(
            "task finished",
            extra={
                "task_status": status,
                "attempt": message.attempt,
                "duration_ms": duration_ms,
                **counts,
            },
        )
        metric("TaskDuration", duration_ms, unit="Milliseconds", dims={"Region": message.region})
        metric(
            {"retrying": "TaskRetried", "succeeded": "TaskSucceeded"}.get(status, "TaskFailed"),
            dims={"Region": message.region},
        )
        return HandleOutcome.DELETE

    def _finish(
        self,
        message: ScanTaskMessage,
        queue: str,
        merged: dict[str, TypeResult],
        latest: dict[str, TypeResult],
    ) -> str:
        retry_types = [
            t for t, r in latest.items() if r.status is TypeStatus.FAILED and r.retryable
        ]
        payload = {t: r.to_json() for t, r in merged.items()}
        if retry_types and message.attempt < self.max_attempts:
            # a task-level crash retries the whole message; otherwise only the failed types
            retry_scope = message.resource_types if TASK_KEY in retry_types else sorted(retry_types)
            retry = message.model_copy(
                update={"attempt": message.attempt + 1, "resource_types": retry_scope}
            )
            delay = min(900, self.retry_base_delay * 2 ** (message.attempt - 1))
            sent = self.dispatcher.dispatch(
                [(retry, "ondemand" if queue == "ondemand" else "scheduled")], delay_seconds=delay
            )
            if not sent.failed:
                with self.db.begin() as conn:
                    owned = self.jobs.mark_retrying(
                        conn, message.task_id, payload, worker_id=self.worker_id, now=self.clock()
                    )
                if owned:
                    return "retrying"
            else:
                log.warning(
                    "could not enqueue retry; finishing task",
                    extra={"error_code": sent.failed[0][1]},
                )

        status = task_status(merged)
        first_error = next((r for r in merged.values() if r.error_code), None)
        with self.db.begin() as conn:
            self.jobs.finish_task(
                conn,
                message.task_id,
                status,  # type: ignore[arg-type]
                result=payload,
                error_code=first_error.error_code if first_error else None,
                error_message=first_error.error_message if first_error else None,
                worker_id=self.worker_id,
                now=self.clock(),
            )
        return status


def task_status(results: Mapping[str, TypeResult]) -> str:
    statuses = {r.status for r in results.values()}
    ok = {TypeStatus.SUCCEEDED, TypeStatus.SKIPPED_NO_ENDPOINT}
    if not statuses or statuses <= ok:
        return "succeeded"
    if statuses == {TypeStatus.AUTH_DENIED}:
        return "auth_denied"
    if TypeStatus.FAILED in statuses:
        return "partial" if statuses & ok else "failed"
    return "succeeded"  # succeeded/skipped mixed with auth_denied
