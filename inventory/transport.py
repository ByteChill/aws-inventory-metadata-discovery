"""Interfaces that differ between network scenarios, and their scenario-A implementations.

Scenario B (non-VPC Lambda, docs/specs/09-reserved-extensions.md) is reserved: only the
interfaces exist; there are deliberately no placeholder implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Connection, Engine
from sqlalchemy.engine import RowMapping

from inventory.aws import ClientFactory
from inventory.config import CommonSettings
from inventory.db import IndexEntry, ResourceRepository, ScopeStateRepository
from inventory.models import (
    ChangeEvent,
    ChangeSet,
    ErrorInfo,
    QueueKind,
    RefreshStatus,
    ResourceKey,
    ResourceRecord,
    ScanTaskMessage,
    TypeResult,
    TypeScope,
    TypeStatus,
    utcnow,
)

log = logging.getLogger(__name__)


class CredentialProvider(Protocol):
    def session(self, account_id: str) -> boto3.Session: ...

    def accessible_accounts(self) -> set[str]: ...


class ChangeSink(Protocol):
    def emit(self, conn: Connection, events: Sequence[ChangeEvent]) -> None: ...


class NullChangeSink:
    """v1 default: change history is reserved (spec 09)."""

    def emit(self, conn: Connection, events: Sequence[ChangeEvent]) -> None:
        return None


class ResultSink(Protocol):
    def load_index(
        self, resource_type: str, account_id: str, region: str
    ) -> dict[str, IndexEntry]: ...

    def apply(
        self,
        scope: TypeScope,
        changes: ChangeSet,
        result: TypeResult,
        job_id: str,
        resource_count: int,
    ) -> None: ...

    def apply_refresh(
        self, key: ResourceKey, record: ResourceRecord | None, job_id: str
    ) -> RefreshStatus: ...

    def record_skip(
        self, scope: TypeScope, status: TypeStatus, job_id: str, error: ErrorInfo | None
    ) -> None: ...


class PostgresResultSink:
    """Scenario A: workers and the API write straight to PostgreSQL."""

    def __init__(
        self,
        engine: Engine,
        change_sink: ChangeSink | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._engine = engine
        self._changes = change_sink or NullChangeSink()
        self._resources = ResourceRepository()
        self._scopes = ScopeStateRepository()
        self._clock = clock

    def load_index(self, resource_type: str, account_id: str, region: str) -> dict[str, IndexEntry]:
        with self._engine.connect() as conn:
            return self._resources.load_index(conn, resource_type, account_id, region)

    def apply(
        self,
        scope: TypeScope,
        changes: ChangeSet,
        result: TypeResult,
        job_id: str,
        resource_count: int,
    ) -> None:
        with self._engine.begin() as conn:
            self._resources.upsert(conn, changes.upserts, job_id)
            deleted = self._resources.soft_delete(
                conn,
                scope.resource_type,
                scope.account_id,
                scope.region,
                changes.deleted_ids,
                before=changes.scan_started_at,
                job_id=job_id,
                now=self._clock(),
            )
            if deleted != len(changes.deleted_ids):
                # rows refreshed after the scan started survive; keep the reported count honest
                result.deleted = deleted
            self._changes.emit(conn, changes.events)
            self._scopes.upsert(
                conn,
                account_id=scope.account_id,
                region=scope.region,
                resource_type=scope.resource_type,
                status=TypeStatus.SUCCEEDED,
                attempt_at=changes.scan_started_at,
                success_at=changes.scan_started_at,
                resource_count=resource_count,
                job_id=job_id,
            )

    def apply_refresh(
        self, key: ResourceKey, record: ResourceRecord | None, job_id: str
    ) -> RefreshStatus:
        now = self._clock()
        with self._engine.begin() as conn:
            existing = self._resources.get(conn, key, for_update=True)
            live = existing is not None and existing["deleted_at"] is None
            if record is None:
                if not live:
                    return "not_found"
                deleted = self._resources.soft_delete(
                    conn,
                    key.resource_type,
                    key.account_id,
                    key.region,
                    [key.resource_id],
                    before=now,
                    job_id=job_id,
                    now=now,
                )
                if not deleted:
                    return "unchanged"  # a newer observation exists; keep it
                self._changes.emit(conn, [_event(key, existing, None, "deleted", now, job_id)])
                return "deleted"

            if not self._resources.upsert(conn, [record], job_id):
                return "unchanged"  # a newer observation is already stored
            if not live:
                status: RefreshStatus = "created"
            elif bytes(existing["content_hash"]) == record.content_hash:  # type: ignore[index]
                return "unchanged"
            else:
                status = "updated"
            self._changes.emit(
                conn, [_event(key, existing, record, status, record.observed_at, job_id)]
            )
            return status

    def record_skip(
        self, scope: TypeScope, status: TypeStatus, job_id: str, error: ErrorInfo | None
    ) -> None:
        with self._engine.begin() as conn:
            self._scopes.upsert(
                conn,
                account_id=scope.account_id,
                region=scope.region,
                resource_type=scope.resource_type,
                status=status,
                attempt_at=self._clock(),
                job_id=job_id,
                error_code=error.code if error else None,
                error_message=error.message if error else None,
            )


def _event(
    key: ResourceKey,
    existing: RowMapping | None,
    record: ResourceRecord | None,
    change_type: str,
    when: object,
    job_id: str,
) -> ChangeEvent:
    old_hash = existing["content_hash"] if existing is not None else None
    return ChangeEvent(
        key=key,
        arn=record.arn if record else str(existing["arn"] if existing else ""),
        change_type=change_type,  # type: ignore[arg-type]
        changed_at=when,  # type: ignore[arg-type]
        job_id=job_id,
        old_hash=bytes(old_hash) if isinstance(old_hash, bytes | memoryview) else None,
        new_hash=record.content_hash if record else None,
        data=record.data if record else None,
    )


# --------------------------------------------------------------------------- dispatch

SQS_BATCH = 10


@dataclass
class DispatchResult:
    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (task_id, error)


class Dispatcher(Protocol):
    def dispatch(
        self, messages: Sequence[tuple[ScanTaskMessage, QueueKind]], delay_seconds: int = 0
    ) -> DispatchResult: ...


class SqsDirectDispatcher:
    """Scenario A: send straight to each region's queue (cross-region via peering endpoints)."""

    def __init__(self, settings: CommonSettings, clients: ClientFactory) -> None:
        self._settings = settings
        self._clients = clients

    def dispatch(
        self, messages: Sequence[tuple[ScanTaskMessage, QueueKind]], delay_seconds: int = 0
    ) -> DispatchResult:
        result = DispatchResult()
        by_queue: dict[tuple[str, str], list[ScanTaskMessage]] = {}
        for message, kind in messages:
            cell = self._settings.cell(message.region)
            url = cell.ondemand_queue_url if kind == "ondemand" else cell.scheduled_queue_url
            if not url:
                result.failed.append((message.task_id, f"no {kind} queue for {cell.region}"))
                continue
            by_queue.setdefault((cell.region, url), []).append(message)

        for (region, url), queued in by_queue.items():
            client = self._clients.client("sqs", region)
            for start in range(0, len(queued), SQS_BATCH):
                batch = queued[start : start + SQS_BATCH]
                self._send(client, url, batch, delay_seconds, result)
        return result

    @staticmethod
    def _send(
        client: Any,
        url: str,
        batch: Sequence[ScanTaskMessage],
        delay_seconds: int,
        result: DispatchResult,
    ) -> None:
        entries = [
            {
                "Id": str(i),
                "MessageBody": m.model_dump_json(),
                "DelaySeconds": delay_seconds,
                "MessageAttributes": {
                    name: {"DataType": "String", "StringValue": value}
                    for name, value in (
                        ("request_id", m.request_id or m.job_id),
                        ("job_id", m.job_id),
                        ("task_id", m.task_id),
                    )
                },
            }
            for i, m in enumerate(batch)
        ]
        try:
            response = client.send_message_batch(QueueUrl=url, Entries=entries)
        except (ClientError, BotoCoreError) as exc:
            log.warning(
                "send_message_batch failed",
                extra={"queue_url": url, "error_code": type(exc).__name__},
            )
            result.failed.extend((m.task_id, type(exc).__name__) for m in batch)
            return
        result.succeeded.extend(
            batch[int(ok["Id"])].task_id for ok in response.get("Successful", [])
        )
        result.failed.extend(
            (batch[int(bad["Id"])].task_id, str(bad.get("Code", "SendFailed")))
            for bad in response.get("Failed", [])
        )
