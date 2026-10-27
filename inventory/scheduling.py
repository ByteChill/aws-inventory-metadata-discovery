"""Scope expansion, task creation and dispatch — shared by the planner and the API."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Engine

from inventory.aws import AwsClient, ClientFactory
from inventory.collectors.base import Registry
from inventory.config import GLOBAL_REGION, US_EAST_1, CommonSettings
from inventory.db import AccountRegionRepository, JobRepository
from inventory.errors import BadRequestError, UpstreamError
from inventory.models import (
    NewTask,
    QueueKind,
    ScanJob,
    ScanScope,
    ScanTask,
    ScanTaskMessage,
    utcnow,
)
from inventory.transport import CredentialProvider, Dispatcher, DispatchResult

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        settings: CommonSettings,
        db: Engine,
        credentials: CredentialProvider,
        clients: ClientFactory,
        registry: Registry,
        dispatcher: Dispatcher,
        *,
        account_region_max_age: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.db = db
        self.credentials = credentials
        self.clients = clients
        self.registry = registry
        self.dispatcher = dispatcher
        self.max_age = account_region_max_age
        self.clock = clock
        self.jobs = JobRepository()
        self.account_regions = AccountRegionRepository()

    # ------------------------------------------------------------------ regions
    def enabled_regions(self, account_id: str) -> list[str]:
        """Enabled ∩ scan regions for an account (cached in account_region)."""
        scan_regions = self.settings.scan_regions
        with self.db.connect() as conn:
            cached = self.account_regions.get_enabled(
                conn, account_id, max_age=self.max_age, regions=scan_regions, now=self.clock()
            )
        if cached is not None:
            return cached
        enabled = self._describe_regions(account_id)
        # jitter so that 1000+ accounts do not all expire (and hit the broker) together
        checked_at = self.clock() - timedelta(minutes=random.randint(0, 120))
        with self.db.begin() as conn:
            self.account_regions.replace(
                conn, account_id, enabled=enabled, supported=scan_regions, checked_at=checked_at
            )
        return sorted(r for r in scan_regions if r in enabled)

    def _describe_regions(self, account_id: str) -> set[str]:
        # us-east-1 is enabled for every account; the primary region is opt-in
        session = self.credentials.session(account_id)
        client: AwsClient = self.clients.client("ec2", US_EAST_1, session)
        response = client.describe_regions(AllRegions=False)
        return {r["RegionName"] for r in response.get("Regions", [])}

    # ------------------------------------------------------------------ expansion
    def expand(self, scope: ScanScope, queue: QueueKind) -> list[NewTask]:
        """Tasks for an account-level scope; raises BadRequestError for what cannot be scanned."""
        if scope.all or scope.arn or not scope.account_id:
            raise BadRequestError("only account-level scopes can be expanded into tasks")
        self.validate(scope)
        account = scope.account_id
        if scope.region is None and scope.resource_type is None:
            tasks = [NewTask(account, region, queue) for region in self.enabled_regions(account)]
            return [*tasks, NewTask(account, GLOBAL_REGION, queue)]
        rtype = scope.resource_type
        if scope.region is not None:
            regions = [scope.region]
            if scope.region != GLOBAL_REGION and scope.region not in self.enabled_regions(account):
                raise BadRequestError(f"region {scope.region} is not enabled for account {account}")
        elif rtype is not None and self.registry.get(rtype).is_global:
            regions = [GLOBAL_REGION]
        else:
            regions = self.enabled_regions(account)
            if not regions:
                raise BadRequestError(f"account {account} has none of the scan regions enabled")
        types = [rtype] if rtype else None
        return [NewTask(account, region, queue, resource_types=types) for region in regions]

    def validate(self, scope: ScanScope) -> None:
        """Checks that need no account data: scan region, known type, global/regional match."""
        region, rtype = scope.region, scope.resource_type
        if region not in (None, GLOBAL_REGION, *self.settings.scan_regions):
            raise BadRequestError(f"region {region} is not supported")
        if rtype is None:
            return
        if not self.registry.has(rtype):
            raise BadRequestError(f"unknown resource_type {rtype}")
        if region is not None and self.registry.get(rtype).is_global != (region == GLOBAL_REGION):
            raise BadRequestError(f"{rtype} cannot be scanned in region {region}")

    # ------------------------------------------------------------------ submit
    def submit(self, job: ScanJob, tasks: Sequence[NewTask]) -> SubmitResult:
        """Write tasks first, then dispatch; failed sends are marked DISPATCH_FAILED."""
        with self.db.begin() as conn:
            created = self.jobs.create_tasks(conn, job.job_id, tasks, now=self.clock())
        queued = [t for t in created if t.status == "queued"]
        messages = [(message_for(job, t), t.queue) for t in queued]
        try:
            result = self.dispatcher.dispatch(messages) if messages else DispatchResult()
        except Exception as exc:
            # nothing was confirmed sent: never leave queued tasks without a message
            log.exception("dispatch raised")
            result = DispatchResult(failed=[(t.task_id, type(exc).__name__) for t in queued])
        if result.failed or len(created) != len(queued):
            with self.db.begin() as conn:
                for task_id, error in result.failed:
                    log.warning("dispatch failed", extra={"task_id": task_id, "error_code": error})
                    self.jobs.finish_task(
                        conn,
                        task_id,
                        "failed",
                        error_code="DISPATCH_FAILED",
                        error_message=error,
                        now=self.clock(),
                    )
                self.jobs.refresh_job_status(conn, job.job_id, now=self.clock())
        elif not created:
            with self.db.begin() as conn:  # nothing to do: the job is complete
                self.jobs.refresh_job_status(conn, job.job_id, now=self.clock())
        return SubmitResult(created, result)


@dataclass
class SubmitResult:
    tasks: list[ScanTask]
    dispatch: DispatchResult

    @property
    def failed(self) -> list[tuple[str, str]]:
        return self.dispatch.failed


def message_for(job: ScanJob, task: ScanTask, attempt: int = 1) -> ScanTaskMessage:
    return ScanTaskMessage(
        job_id=job.job_id,
        task_id=task.task_id,
        request_id=job.request_id,
        trigger=job.trigger,
        account_id=task.account_id,
        region=task.region,
        resource_types=task.resource_types,
        resource_ids=task.resource_ids,
        full_refresh=job.full_refresh,
        attempt=attempt,
    )


class AccountDirectoryClient:
    """Adapter for aws-inventory-account-directory.

    The real request/response contract is still to be confirmed (spec 05 §6); only this class
    and its fixtures change when it is.
    """

    def __init__(self, clients: ClientFactory, function_name: str, region: str) -> None:
        self._clients = clients
        self._function_name = function_name
        self._region = region

    def list_accounts(self) -> list[str]:
        client = self._clients.client("lambda", self._region)
        try:
            response = client.invoke(
                FunctionName=self._function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps({}).encode(),
            )
            body: dict[str, Any] = json.loads(response["Payload"].read() or b"{}")
        except (ClientError, BotoCoreError, ValueError) as exc:
            raise UpstreamError(
                f"account directory call failed: {type(exc).__name__}", retryable=True
            ) from exc
        if response.get("FunctionError"):
            raise UpstreamError("account directory function error", retryable=True)
        return parse_account_directory(body)


def parse_account_directory(body: dict[str, Any]) -> list[str]:
    accounts = body.get("accounts")
    if not isinstance(accounts, list):
        raise UpstreamError("account directory response has no 'accounts' list", retryable=False)
    return sorted(
        {
            str(a["account_id"])
            for a in accounts
            if isinstance(a, dict) and a.get("account_id") and a.get("status", "ACTIVE") == "ACTIVE"
        }
    )
