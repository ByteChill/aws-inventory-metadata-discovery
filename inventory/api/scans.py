"""Scan jobs and refresh endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any

import anyio
from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, Query, Response
from pydantic import ValidationError
from sqlalchemy.engine import RowMapping

from inventory.api.errors import ValidationFailedError
from inventory.api.refresh import ArnResult
from inventory.api.resources import to_resource
from inventory.api.schemas import (
    Page,
    RefreshError,
    RefreshRequest,
    RefreshResponse,
    RefreshResult,
    ScanJobOut,
    ScanRequest,
    ScanTaskOut,
)
from inventory.api.state import ApiState, get_state
from inventory.db import JobRepository
from inventory.engine import classify
from inventory.errors import BadRequestError, NotFoundError, NotImplementedFeatureError
from inventory.models import TERMINAL_JOB_STATUSES, JobStatus, ScanJob, ScanScope, Trigger, utcnow
from inventory.observability import get_context

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["scans"])
State = Annotated[ApiState, Depends(get_state)]
jobs = JobRepository()


async def job_out(
    state: ApiState,
    job: ScanJob | RowMapping,
    *,
    deduplicated: bool = False,
    include_tasks: bool = False,
    live_stats: bool = True,
) -> ScanJobOut:
    data = job.model_dump() if isinstance(job, ScanJob) else dict(job)
    if live_stats and data["status"] not in TERMINAL_JOB_STATUSES:
        data["stats"] = await state.queries.live_job_stats(data["job_id"])
    out = ScanJobOut.model_validate({**data, "deduplicated": deduplicated})
    if include_tasks:
        rows = await state.queries.job_tasks(data["job_id"])
        out.tasks = [ScanTaskOut.model_validate(dict(r)) for r in rows]
    return out


async def wait_for_job(state: ApiState, job_id: str, wait_seconds: float) -> RowMapping | None:
    deadline = anyio.current_time() + wait_seconds
    interval = state.settings.wait_poll_interval_ms / 1000
    while True:
        row = await state.queries.job(job_id)
        remaining = deadline - anyio.current_time()
        if row is None or row["status"] in TERMINAL_JOB_STATUSES or remaining <= 0:
            return row
        await anyio.sleep(min(interval, remaining))


@router.post("/scans", response_model=ScanJobOut, responses={202: {"model": ScanJobOut}})
async def create_scan(state: State, body: ScanRequest, response: Response) -> ScanJobOut:
    scope_in = body.scope
    if scope_in.arn:
        if any((scope_in.account_id, scope_in.region, scope_in.resource_type)):
            raise ValidationFailedError("arn cannot be combined with other scope fields")
        job, _, done, deduplicated = await state.refresh.refresh([scope_in.arn], body.wait_seconds)
        response.status_code = 200 if done else 202
        return await job_out(state, job, deduplicated=deduplicated)
    try:
        scope = ScanScope.model_validate(scope_in.model_dump(exclude_none=True))
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
        details = [
            {"loc": ["body", "scope", *e["loc"]], "msg": e["msg"], "type": e["type"]}
            for e in errors
        ]
        raise ValidationFailedError(
            errors[0]["msg"].removeprefix("Value error, "), details
        ) from None

    scheduler = state.runtime.scheduler
    try:
        if scope.region:
            scheduler.validate_region(scope.region)
            if scope.resource_type:
                scheduler.validate_type(scope.resource_type, scope.region)
    except BadRequestError as exc:
        raise ValidationFailedError(exc.message) from None

    def create() -> tuple[ScanJob, bool]:
        now = utcnow()
        with state.runtime.db.begin() as conn:
            return jobs.create_job(
                conn,
                trigger="api",
                scope=scope,
                request_id=get_context().get("request_id"),
                deadline_at=now + timedelta(minutes=state.settings.scan_job_deadline_minutes),
                now=now,
            )

    def start(job: ScanJob) -> None:
        # Expansion can call the broker/AWS (enabled regions); a failure fails this job
        # instead of leaving an empty "queued" job that blocks the scope until its deadline.
        try:
            scheduler.submit(job, scheduler.expand(scope, "ondemand"))
        except Exception as exc:
            error = classify(exc)
            log.warning("scan could not be started", extra={"error_code": error.code})
            with state.runtime.db.begin() as conn:
                jobs.fail_job(conn, job.job_id, f"{error.code}: {error.message}")

    job, created = await run_sync(create)
    if created:
        await run_sync(start, job)
    row = await wait_for_job(state, job.job_id, body.wait_seconds)
    final = row if row is not None else job
    status = final.status if isinstance(final, ScanJob) else final["status"]
    response.status_code = 200 if status in TERMINAL_JOB_STATUSES else 202
    return await job_out(state, final, deduplicated=not created)


@router.get("/scans/{job_id}", response_model=ScanJobOut)
async def get_scan(state: State, job_id: str, include_tasks: bool = False) -> ScanJobOut:
    row = await state.queries.job(job_id)
    if row is None:
        raise NotFoundError(f"job {job_id} not found")
    return await job_out(state, row, include_tasks=include_tasks)


@router.get("/scans", response_model=Page[ScanJobOut])
async def list_scans(
    state: State,
    status: Annotated[list[JobStatus] | None, Query()] = None,
    trigger: Trigger | None = None,
    account_id: str | None = None,
    created_since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> Page[ScanJobOut]:
    rows, next_cursor = await state.queries.list_jobs(
        status or [], trigger, account_id, created_since, limit, cursor
    )
    # stored stats only: live aggregation per running job would be N heavy queries per page
    items = [await job_out(state, r, live_stats=False) for r in rows]
    return Page[ScanJobOut](items=items, next_cursor=next_cursor)


@router.post(
    "/resources/refresh",
    response_model=RefreshResponse,
    responses={202: {"model": RefreshResponse}},
)
async def refresh_resources(
    state: State, body: RefreshRequest, response: Response
) -> RefreshResponse:
    if len(body.arns) > state.settings.refresh_max_arns:
        raise ValidationFailedError(f"at most {state.settings.refresh_max_arns} ARNs per request")
    job, results, done, deduplicated = await state.refresh.refresh(body.arns, body.wait_seconds)
    response.status_code = 200 if done else 202
    return RefreshResponse(
        job=await job_out(state, job, deduplicated=deduplicated),
        results=[await _result(state, r) for r in results],
    )


async def _result(state: ApiState, result: ArnResult) -> RefreshResult:
    resource = None
    if result.key is not None and result.status in ("created", "updated", "unchanged"):
        k = result.key
        row = await state.queries.get(
            (k.resource_type, k.account_id, k.region, k.resource_id), False
        )
        resource = to_resource(row) if row is not None else None
    error = (
        RefreshError(code=result.error_code, message=result.error_message or "")
        if result.error_code
        else None
    )
    return RefreshResult(arn=result.arn, status=result.status, resource=resource, error=error)


@router.api_route("/history/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def history(path: str) -> dict[str, Any]:
    raise NotImplementedFeatureError("change history is not implemented yet (spec 09)")
