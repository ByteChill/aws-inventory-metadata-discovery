"""Planner Lambda: the steps of the scan-run state machine plus periodic maintenance."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from inventory.bootstrap import Runtime, build_runtime
from inventory.config import PlannerSettings, get_common_settings, get_planner_settings
from inventory.db import ACTIVE_JOB_STATUSES, JobRepository, ResourceRepository, scan_job
from inventory.engine import classify
from inventory.errors import NotFoundError
from inventory.models import NewTask, ScanJob, ScanScope, TaskStatus, utcnow
from inventory.observability import context_scope, install_filters, metric, setup_logging
from inventory.scheduling import AccountDirectoryClient

SERVICE_NAME = "inventory-planner"
SCHEDULE_SCOPE = ScanScope(all=True)
NO_REGION = "*"  # tasks recorded for a whole account (denied / failed before expansion)
AUTH_CODES = frozenset({"ACCOUNT_ACCESS_DENIED", "API_ACCESS_DENIED"})

logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
metrics = Metrics(namespace="Inventory", service=SERVICE_NAME)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- events


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateJobEvent(_Event):
    action: Literal["create_job"]
    scheduled_at: str | None = None


class JobEvent(_Event):
    action: Literal["list_targets", "check_progress"]
    job_id: str


class ErrorEvent(_Event):
    """``error`` comes from a Step Functions Catch ({"Error", "Cause"})."""

    action: Literal["mark_dispatched", "finalize"]
    job_id: str
    error: Any = None


class BatchInput(BaseModel):
    job_id: str


class DispatchBatchEvent(_Event):
    action: Literal["dispatch_batch"]
    batch_input: BatchInput = Field(alias="BatchInput")
    items: list[str] = Field(alias="Items")


class MaintenanceEvent(_Event):
    action: Literal["maintenance"]


PlannerEvent = Annotated[
    CreateJobEvent | JobEvent | ErrorEvent | DispatchBatchEvent | MaintenanceEvent,
    Field(discriminator="action"),
]
EVENT_ADAPTER: TypeAdapter[PlannerEvent] = TypeAdapter(PlannerEvent)


# --------------------------------------------------------------------------- planner


class Planner:
    def __init__(
        self,
        runtime: Runtime,
        settings: PlannerSettings,
        directory: AccountDirectoryClient,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.rt = runtime
        self.settings = settings
        self.directory = directory
        self.clock = clock
        self.jobs = JobRepository()

    def handle(self, event: dict[str, Any], remaining_ms: Callable[[], int]) -> dict[str, Any]:
        parsed = EVENT_ADAPTER.validate_python(event)
        job_id = getattr(parsed, "job_id", None) or getattr(
            getattr(parsed, "batch_input", None), "job_id", None
        )
        with context_scope(job_id=job_id):
            if isinstance(parsed, CreateJobEvent):
                return self.create_job()
            if isinstance(parsed, DispatchBatchEvent):
                return self.dispatch_batch(parsed.batch_input.job_id, parsed.items)
            if isinstance(parsed, MaintenanceEvent):
                return self.maintenance(remaining_ms)
            if isinstance(parsed, ErrorEvent):
                if parsed.action == "finalize":
                    return self.finalize(parsed.job_id, parsed.error)
                return self.mark_dispatched(parsed.job_id, parsed.error)
            if parsed.action == "list_targets":
                return self.list_targets(parsed.job_id)
            return self.check_progress(parsed.job_id)

    # ------------------------------------------------------------------ steps
    def create_job(self) -> dict[str, Any]:
        now = self.clock()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.rt.db.begin() as conn:
            full_refresh = not self.jobs.has_schedule_job_since(conn, start_of_day)
            job, created = self.jobs.create_job(
                conn,
                trigger="schedule",
                scope=SCHEDULE_SCOPE,
                flags={"full_refresh": full_refresh, "dispatching": True},
                deadline_at=now + timedelta(minutes=self.settings.job_deadline_minutes),
                now=now,
            )
        with context_scope(job_id=job.job_id):
            if not created:
                log.warning("previous scheduled run still active; skipping")
                metric("ScheduleOverlap")
                return {
                    "skipped": True,
                    "job_id": None,
                    "active_job_id": job.job_id,
                    "full_refresh": False,
                }
            log.info("scheduled job created", extra={"full_refresh": full_refresh})
        return {"skipped": False, "job_id": job.job_id, "full_refresh": full_refresh}

    def list_targets(self, job_id: str) -> dict[str, Any]:
        directory = set(self.directory.list_accounts())
        accessible = self.rt.credentials.accessible_accounts()
        denied = sorted(directory - accessible)
        if denied:
            with self.rt.db.begin() as conn:
                self.jobs.create_tasks(
                    conn,
                    job_id,
                    [
                        self._account_task(
                            a,
                            "auth_denied",
                            "ACCESS_DENIED",
                            "no ReadOnly role in the SAML assertion",
                        )
                        for a in denied
                    ],
                    now=self.clock(),
                )
        accounts = sorted(directory & accessible)
        log.info(
            "scan targets listed",
            extra={"accounts": len(accounts), "denied": len(denied), "directory": len(directory)},
        )
        return {"job_id": job_id, "accounts": accounts, "denied_count": len(denied)}

    def dispatch_batch(self, job_id: str, accounts: list[str]) -> dict[str, Any]:
        """Expand and dispatch a batch of accounts. Idempotent and never raises:

        accounts that already have tasks (a retried batch) are skipped, and per-account
        failures are recorded as account-level tasks so the run reports them.
        """
        with self.rt.db.connect() as conn:
            job = self.jobs.get_job(conn, job_id)
            done = self.jobs.accounts_with_tasks(conn, job_id, accounts) if job else set()
        if job is None:
            log.error("dispatch_batch for unknown job")
            return {"accounts": len(accounts), "tasks": 0, "failed_accounts": len(accounts)}

        pending = [a for a in accounts if a not in done]
        new_tasks: list[NewTask] = []
        failures: list[NewTask] = []
        for account in pending:
            with context_scope(account_id=account):
                try:
                    new_tasks.extend(
                        self.rt.scheduler.expand(ScanScope(account_id=account), "scheduled")
                    )
                except Exception as exc:
                    error = classify(exc)
                    status: TaskStatus = "auth_denied" if error.code in AUTH_CODES else "failed"
                    log.warning("account expansion failed", extra={"error_code": error.code})
                    failures.append(self._account_task(account, status, error.code, error.message))
        sent_failed = 0
        try:
            if failures:
                with self.rt.db.begin() as conn:
                    self.jobs.create_tasks(conn, job_id, failures, now=self.clock())
            if new_tasks:
                sent_failed = len(self.rt.scheduler.submit(job, new_tasks).failed)
        except Exception:
            log.exception("batch submit failed")
            return {
                "accounts": len(accounts),
                "skipped_accounts": len(done),
                "tasks": 0,
                "failed_accounts": len(pending),
            }
        return {
            "accounts": len(accounts),
            "skipped_accounts": len(done),
            "tasks": len(new_tasks),
            "failed_accounts": len(failures),
            "failed_tasks": sent_failed,
        }

    def mark_dispatched(self, job_id: str, error: Any = None) -> dict[str, Any]:
        with self.rt.db.begin() as conn:
            if error is not None:
                log.error("dispatch did not complete", extra={"sfn_error": str(error)[:500]})
                self.jobs.set_job_error(conn, job_id, f"dispatch: {str(error)[:1990]}")
            job = self.jobs.mark_dispatched(conn, job_id, now=self.clock())
        return {"job_id": job_id, "status": job.status if job else None}

    def check_progress(self, job_id: str) -> dict[str, Any]:
        with self.rt.db.begin() as conn:
            job = self.jobs.refresh_job_status(conn, job_id, now=self.clock())
            counts = self.jobs.task_counts(conn, job_id)
        if job is None:
            raise NotFoundError(f"job {job_id} not found")
        overdue = job.deadline_at is not None and self.clock() > job.deadline_at
        done = job.status not in ACTIVE_JOB_STATUSES or overdue
        return {"job_id": job_id, "done": done, "status": job.status, "counts": counts}

    def finalize(self, job_id: str, error: Any = None) -> dict[str, Any]:
        """Close the run. An error before the deadline is only recorded: the queued work
        keeps running and maintenance closes the job later; tasks are not thrown away."""
        now = self.clock()
        with self.rt.db.begin() as conn:
            job = self.jobs.get_job(conn, job_id)
            if job is None:
                raise NotFoundError(f"job {job_id} not found")
            if error is not None:
                self.jobs.set_job_error(conn, job_id, str(error)[:2000])
            overdue = job.deadline_at is None or now >= job.deadline_at
            if job.status in ACTIVE_JOB_STATUSES and (overdue or error is None):
                job = self.jobs.timeout_job(conn, job_id, now=now)
            else:
                job = self.jobs.get_job(conn, job_id)
        assert job is not None
        self._job_metrics(job)
        log.info("job finalized", extra={"job_status": job.status, "stats": job.stats})
        return {"job_id": job_id, "status": job.status, "stats": job.stats}

    def maintenance(self, remaining_ms: Callable[[], int]) -> dict[str, Any]:
        now = self.clock()
        stale_before = now - timedelta(minutes=self.settings.task_stale_minutes)
        with self.rt.db.begin() as conn:
            stale_jobs = self.jobs.timeout_stale_tasks(conn, stale_before, now=now)
            overdue = self.jobs.overdue_jobs(conn, now=now)
            for job_id in overdue:
                self.jobs.timeout_job(conn, job_id, now=now)
            active = list(
                conn.execute(
                    scan_job.select()
                    .with_only_columns(scan_job.c.job_id)
                    .where(scan_job.c.status.in_(ACTIVE_JOB_STATUSES))
                ).scalars()
            )
        for job_id in active:  # safety net: close jobs whose tasks all finished
            with self.rt.db.begin() as conn:
                self.jobs.refresh_job_status(conn, job_id, now=now)
        purged = 0
        cutoff = now - timedelta(days=self.settings.deleted_retention_days)
        resources = ResourceRepository()
        while remaining_ms() > 30_000:
            with self.rt.db.begin() as conn:
                batch = resources.purge_deleted(conn, cutoff)
            purged += batch
            if batch == 0:
                break
        log.info(
            "maintenance done",
            extra={"stale_jobs": len(stale_jobs), "overdue_jobs": len(overdue), "purged": purged},
        )
        return {"stale_jobs": stale_jobs, "overdue_jobs": overdue, "purged": purged}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _account_task(account: str, status: TaskStatus, code: str, message: str) -> NewTask:
        return NewTask(
            account, NO_REGION, "scheduled", status=status, error_code=code, error_message=message
        )

    @staticmethod
    def _job_metrics(job: ScanJob) -> None:
        by_status = job.stats.get("by_status", {})
        metric("JobTasksFailed", by_status.get("failed", 0) + by_status.get("partial", 0))
        metric("JobTasksTimedOut", by_status.get("timed_out", 0))
        if job.started_at and job.finished_at:
            metric(
                "JobDuration", (job.finished_at - job.started_at).total_seconds(), unit="Seconds"
            )


@functools.cache
def get_planner() -> Planner:
    common = get_common_settings()
    setup_logging(SERVICE_NAME, common.log_level, common.log_sample_rate)
    install_filters(logging.getLogger(SERVICE_NAME))
    runtime = build_runtime(SERVICE_NAME, statement_timeout_ms=60_000)
    settings = get_planner_settings()
    directory = AccountDirectoryClient(
        runtime.clients, settings.account_directory_function_name, runtime.common.primary_region
    )
    return Planner(runtime, settings, directory)


@logger.inject_lambda_context(correlation_id_path="job_id")
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    started = time.perf_counter()
    with context_scope(aws_request_id=context.aws_request_id):
        response = get_planner().handle(event, context.get_remaining_time_in_millis)
        log.info(
            "planner step done",
            extra={
                "action": event.get("action"),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return response
