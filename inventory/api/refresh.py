"""Refresh by ARN: resolve each ARN and queue it to the worker of its region."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
from anyio.to_thread import run_sync

from inventory.bootstrap import Runtime
from inventory.config import ApiSettings
from inventory.db import JobRepository, ResourceRepository
from inventory.models import (
    TERMINAL_TASK_STATUSES,
    NewTask,
    ResourceKey,
    ScanJob,
    ScanScope,
    ScanTask,
    utcnow,
)
from inventory.observability import get_context

log = logging.getLogger(__name__)


@dataclass
class Resolved:
    arn: str
    key: ResourceKey | None = None
    status: str | None = None  # set when the ARN cannot be refreshed
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ArnResult:
    arn: str
    status: str  # RefreshStatus or "pending"
    key: ResourceKey | None = None
    error_code: str | None = None
    error_message: str | None = None


class RefreshService:
    def __init__(self, runtime: Runtime, settings: ApiSettings) -> None:
        self.rt = runtime
        self.settings = settings
        self.jobs = JobRepository()
        self.resources = ResourceRepository()
        self._db_limiter = anyio.CapacityLimiter(16)

    # ------------------------------------------------------------------ resolve
    def resolve(self, arn: str) -> Resolved:
        routed = self.rt.registry.route_arn(arn)
        if routed is None:
            return Resolved(arn, status="unsupported_type")
        cls, parsed = routed
        region = "global" if cls.is_global else parsed.region
        account, resource_id = parsed.account_id, parsed.resource_id
        if account is None or region is None or parsed.id_is_suffix:
            with self.rt.db.connect() as conn:
                matches = self.resources.find_account(
                    conn, cls.resource_type, resource_id, region, id_suffix=parsed.id_is_suffix
                )
            distinct = sorted(set(matches))
            if not distinct:
                return Resolved(
                    arn,
                    status="error",
                    error_code="ACCOUNT_UNRESOLVED",
                    error_message="the ARN has no account and the resource was never scanned; "
                    "scan its account first",
                )
            if len(distinct) > 1:
                return Resolved(
                    arn,
                    status="error",
                    error_code="ACCOUNT_AMBIGUOUS",
                    error_message="the resource id matches several accounts",
                )
            account, region, resource_id = distinct[0]
        key = ResourceKey(cls.resource_type, account, region, resource_id)
        return Resolved(arn, key=key)

    # ------------------------------------------------------------------ run
    async def refresh(
        self, arns: list[str], wait_seconds: float, *, requested_by: str | None = None
    ) -> tuple[ScanJob, list[ArnResult], bool, bool]:
        """Returns (job, per-ARN results, all done, deduplicated)."""
        resolved = [await run_sync(self.resolve, arn, limiter=self._db_limiter) for arn in arns]
        refreshable = [r for r in resolved if r.key is not None]
        scope = ScanScope(arn=arns[0]) if len(arns) == 1 else ScanScope(arns=sorted(arns))
        job, created = await run_sync(
            self._create_job, scope, requested_by, limiter=self._db_limiter
        )
        if created:
            new_tasks = [
                NewTask(
                    r.key.account_id,
                    r.key.region,
                    "ondemand",
                    resource_types=[r.key.resource_type],
                    resource_ids=[r.key.resource_id],
                )
                for r in refreshable
                if r.key is not None
            ]
            await run_sync(self.rt.scheduler.submit, job, new_tasks, limiter=self._db_limiter)

        deadline = anyio.current_time() + wait_seconds
        final_tasks = await self._wait_tasks(job.job_id, deadline, expect_tasks=bool(refreshable))
        results = await run_sync(self._results, resolved, final_tasks, limiter=self._db_limiter)
        job_now = await run_sync(self._get_job, job.job_id, limiter=self._db_limiter)
        done = all(r.status != "pending" for r in results)
        return job_now or job, results, done, not created

    async def _wait_tasks(
        self, job_id: str, deadline: float, *, expect_tasks: bool
    ) -> list[ScanTask]:
        interval = self.settings.wait_poll_interval_ms / 1000
        while True:
            tasks = await run_sync(self._job_tasks, job_id, limiter=self._db_limiter)
            # an empty list may just mean a concurrent identical request has not inserted yet
            settled = bool(tasks) or not expect_tasks
            if settled and all(t.status in TERMINAL_TASK_STATUSES for t in tasks):
                return tasks
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                return tasks
            await anyio.sleep(min(interval, remaining))

    # ------------------------------------------------------------------ sync helpers
    def _create_job(self, scope: ScanScope, requested_by: str | None) -> tuple[ScanJob, bool]:
        now = utcnow()
        with self.rt.db.begin() as conn:
            return self.jobs.create_job(
                conn,
                trigger="api",
                scope=scope,
                request_id=get_context().get("request_id"),
                requested_by=requested_by,
                deadline_at=now + timedelta(minutes=self.settings.refresh_job_deadline_minutes),
                now=now,
            )

    def _job_tasks(self, job_id: str) -> list[ScanTask]:
        with self.rt.db.connect() as conn:
            return self.jobs.list_tasks(conn, job_id)

    def _get_job(self, job_id: str) -> ScanJob | None:
        with self.rt.db.connect() as conn:
            return self.jobs.get_job(conn, job_id)

    def _results(self, resolved: list[Resolved], tasks: list[ScanTask]) -> list[ArnResult]:
        by_key = {_task_key(t): t for t in tasks}
        results = []
        with self.rt.db.connect() as conn:
            for r in resolved:
                if r.key is None:
                    results.append(
                        ArnResult(r.arn, r.status or "error", None, r.error_code, r.error_message)
                    )
                    continue
                task = by_key.get(r.key)
                if task is None or task.status not in TERMINAL_TASK_STATUSES:
                    results.append(ArnResult(r.arn, "pending", r.key))
                    continue
                if task.status != "succeeded":
                    code, message = contract_error(task.error_code, task.error_message)
                    results.append(ArnResult(r.arn, "error", r.key, code, message))
                    continue
                status = _status_from_row(self.resources.get(conn, r.key), task)
                results.append(ArnResult(r.arn, status, r.key))
        return results


def _task_key(task: ScanTask) -> ResourceKey:
    return ResourceKey(
        (task.resource_types or [""])[0],
        task.account_id,
        task.region,
        (task.resource_ids or [""])[0],
    )


def _status_from_row(row: Any, task: ScanTask) -> str:
    """Derive a per-ARN status for refreshes executed by a worker."""
    started = task.started_at or task.queued_at
    if row is None:
        return "not_found"
    if row["deleted_at"] is not None:
        return "deleted" if row["deleted_at"] >= started else "not_found"
    if row["first_seen_at"] >= started:
        return "created"
    if row["last_changed_at"] >= started:
        return "updated"
    return "unchanged"


def contract_error(code: str | None, message: str | None) -> tuple[str, str]:
    """Map internal error codes to the API contract (docs/api.md §1.2)."""
    if code in ("ACCOUNT_ACCESS_DENIED", "API_ACCESS_DENIED", "ACCESS_DENIED"):
        return "ACCOUNT_ACCESS_DENIED", message or "access denied"
    if code in ("THROTTLED", "UPSTREAM_THROTTLED"):
        return "UPSTREAM_THROTTLED", message or "throttled by AWS"
    if code in (None, "INTERNAL_ERROR"):
        return "INTERNAL_ERROR", "internal error"
    return "UPSTREAM_ERROR", f"{code}: {message}" if message else code
