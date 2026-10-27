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

from inventory.aws import AwsClient, ClientFactory, EndpointResolver
from inventory.collectors.base import Registry
from inventory.config import GLOBAL_REGION, CommonSettings
from inventory.db import AccountRegionRepository, JobRepository
from inventory.errors import REGION_NOT_ENABLED_CODES, BadRequestError, UpstreamError
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
        resolver: EndpointResolver,
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
        self.resolver = resolver
        self.dispatcher = dispatcher
        self.max_age = account_region_max_age
        self.clock = clock
        self.jobs = JobRepository()
        self.account_regions = AccountRegionRepository()

    # ------------------------------------------------------------------ regions
    def enabled_regions(self, account_id: str) -> list[str]:
        """Enabled ∩ supported regions for an account (cached in account_region)."""
        with self.db.connect() as conn:
            cached = self.account_regions.get_enabled(
                conn, account_id, max_age=self.max_age, now=self.clock()
            )
        if cached is not None:
            return cached
        enabled = self._describe_regions(account_id)
        supported = self.settings.supported_regions
        # jitter so that 1000+ accounts do not all expire (and hit the broker) together
        checked_at = self.clock() - timedelta(minutes=random.randint(0, 120))
        with self.db.begin() as conn:
            self.account_regions.replace(
                conn, account_id, enabled=enabled, supported=supported, checked_at=checked_at
            )
        return sorted(r for r in supported if r in enabled)

    def _describe_regions(self, account_id: str) -> set[str]:
        session = self.credentials.session(account_id)
        home = self.settings.home_region
        candidates = [home, *(r for r in self.settings.supported_regions if r != home)]
        last_error: Exception | None = None
        for api_region in candidates:
            client: AwsClient = self.clients.client("ec2", api_region, session)
            try:
                response = client.describe_regions(AllRegions=False)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in REGION_NOT_ENABLED_CODES:
                    # the account has not opted in to this region; ask another one
                    last_error = exc
                    continue
                raise
            return {r["RegionName"] for r in response.get("Regions", [])}
        raise UpstreamError(
            f"could not list regions for {account_id}: {type(last_error).__name__}",
            retryable=False,
            code="REGION_NOT_ENABLED",
        )

    # ------------------------------------------------------------------ expansion
    def expand(self, scope: ScanScope, queue: QueueKind) -> list[NewTask]:
        """Tasks for an account-level scope; raises BadRequestError for what cannot be scanned."""
        if scope.all or scope.arn or not scope.account_id:
            raise BadRequestError("only account-level scopes can be expanded into tasks")
        self.validate(scope)
        account = scope.account_id
        if scope.region is None and scope.resource_type is None:
            tasks = [NewTask(account, region, queue) for region in self.enabled_regions(account)]
            available, _ = self.registry.for_region(GLOBAL_REGION, self.resolver)
            if available:
                tasks.append(NewTask(account, GLOBAL_REGION, queue))
            return tasks
        rtype = scope.resource_type
        if scope.region is not None:
            regions = [scope.region]
            if scope.region != GLOBAL_REGION and scope.region not in self.enabled_regions(account):
                raise BadRequestError(f"region {scope.region} is not enabled for account {account}")
        elif rtype is not None and self.registry.get(rtype).is_global:
            regions = [GLOBAL_REGION]
        elif rtype is not None:
            regions = [r for r in self.enabled_regions(account) if self._available(rtype, r)]
            if not regions:
                raise BadRequestError(
                    f"{rtype} is not available in any region enabled for account {account}"
                )
        types = [rtype] if rtype else None
        return [NewTask(account, region, queue, resource_types=types) for region in regions]

    def validate(self, scope: ScanScope) -> None:
        """Checks that need no account data: supported region, known type, endpoints."""
        region, rtype = scope.region, scope.resource_type
        if region not in (None, GLOBAL_REGION, *self.settings.supported_regions):
            raise BadRequestError(f"region {region} is not supported")
        if rtype is None:
            return
        if not self.registry.has(rtype):
            raise BadRequestError(f"unknown resource_type {rtype}")
        cls = self.registry.get(rtype)
        if region is None:
            candidates = [GLOBAL_REGION] if cls.is_global else self.settings.supported_regions
            if not any(self._available(rtype, r) for r in candidates):
                raise BadRequestError(f"{rtype} is not available in any supported region")
            return
        if cls.is_global != (region == GLOBAL_REGION):
            raise BadRequestError(f"{rtype} cannot be scanned in region {region}")
        if not self._available(rtype, region):
            raise BadRequestError(f"{rtype} is not available in region {region} (no endpoint)")

    def _available(self, resource_type: str, region: str) -> bool:
        endpoints = self.registry.get(resource_type).endpoints()
        return all(self.resolver.has_endpoint(s, region) for s in endpoints)

    # ------------------------------------------------------------------ submit
    def submit(self, job: ScanJob, tasks: Sequence[NewTask]) -> SubmitResult:
        """Write tasks first, then dispatch; failed sends are marked DISPATCH_FAILED.

        Inline tasks (API refresh) are created but not dispatched.
        """
        with self.db.begin() as conn:
            created = self.jobs.create_tasks(conn, job.job_id, tasks, now=self.clock())
        queued = [t for t in created if t.status == "queued" and t.queue != "inline"]
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
