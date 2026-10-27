"""Shared data types and cross-component contracts.

Cross-process data (SQS messages, Lambda events, API, broker contract) uses pydantic models;
hot in-process data uses frozen dataclasses.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_serializer,
    model_validator,
)

ACCOUNT_ID_PATTERN = r"^\d{12}$"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- broker contract

BrokerErrorCode = Literal[
    "ACCESS_DENIED",
    "ADFS_AUTH_FAILED",
    "ADFS_UNAVAILABLE",
    "THROTTLED",
    "INVALID_REQUEST",
    "INTERNAL",
]
RETRYABLE_BROKER_CODES: frozenset[str] = frozenset({"ADFS_UNAVAILABLE", "THROTTLED", "INTERNAL"})


class GetCredentialsRequest(_Contract):
    action: Literal["get_credentials"] = "get_credentials"
    account_id: str = Field(pattern=ACCOUNT_ID_PATTERN)
    role: str | None = None
    min_ttl_seconds: int = Field(1200, ge=0, le=3000)
    correlation_id: str | None = None


class ListAccessibleAccountsRequest(_Contract):
    action: Literal["list_accessible_accounts"] = "list_accessible_accounts"
    role: str | None = None
    correlation_id: str | None = None


class GetSamlRequest(_Contract):
    action: Literal["get_saml"] = "get_saml"
    correlation_id: str | None = None


BrokerRequest = Annotated[
    GetCredentialsRequest | ListAccessibleAccountsRequest | GetSamlRequest,
    Field(discriminator="action"),
]
BROKER_REQUEST_ADAPTER: TypeAdapter[BrokerRequest] = TypeAdapter(BrokerRequest)


class AwsCredentials(BaseModel):
    """Temporary credentials. Secrets are masked in repr/logs and revealed only in JSON."""

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr
    expiration: AwareDatetime

    @field_serializer("secret_access_key", "session_token", when_used="json")
    def _reveal(self, value: SecretStr) -> str:
        return value.get_secret_value()

    def remaining_seconds(self, now: datetime) -> float:
        return (self.expiration - now).total_seconds()


class GetCredentialsResponse(BaseModel):
    status: Literal["ok"] = "ok"
    account_id: str
    role_arn: str
    credentials: AwsCredentials
    source: Literal["cache", "sts"]


class ListAccessibleAccountsResponse(BaseModel):
    status: Literal["ok"] = "ok"
    role: str
    account_ids: list[str]
    expires_at: datetime


class GetSamlResponse(BaseModel):
    status: Literal["ok"] = "ok"
    saml_response: SecretStr
    expires_at: datetime

    @field_serializer("saml_response", when_used="json")
    def _reveal(self, value: SecretStr) -> str:
        return value.get_secret_value()


class BrokerErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    code: BrokerErrorCode
    message: str
    retryable: bool


BrokerResponse = (
    GetCredentialsResponse | ListAccessibleAccountsResponse | GetSamlResponse | BrokerErrorResponse
)


# --------------------------------------------------------------------------- JSON & hashing


def normalize(value: Any) -> Any:
    """Convert AWS SDK values to plain JSON types (datetimes -> UTC ISO, bytes -> base64)."""
    if isinstance(value, Mapping):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported value type for inventory data: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- resources


@dataclass(frozen=True, slots=True)
class ResourceKey:
    resource_type: str
    account_id: str
    region: str
    resource_id: str


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    key: ResourceKey
    arn: str
    name: str | None
    tags: Mapping[str, str]
    relations: Mapping[str, Any]
    data: Mapping[str, Any]
    observed_at: datetime
    content_hash: bytes

    @classmethod
    def build(
        cls,
        key: ResourceKey,
        *,
        arn: str,
        name: str | None,
        tags: Mapping[str, str],
        relations: Mapping[str, Any],
        data: Mapping[str, Any],
        observed_at: datetime,
    ) -> ResourceRecord:
        normalized = {
            "arn": arn,
            "name": name,
            "tags": normalize(tags),
            "relations": normalize(relations),
            "data": normalize(data),
        }
        digest = hashlib.sha256(canonical_json(normalized).encode()).digest()
        return cls(
            key=key,
            arn=arn,
            name=name,
            tags=normalized["tags"],
            relations=normalized["relations"],
            data=normalized["data"],
            observed_at=observed_at,
            content_hash=digest,
        )


@dataclass(frozen=True, slots=True)
class KeepExisting:
    """Yielded by a collector when a known resource still exists and need not be re-read."""

    resource_id: str


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    key: ResourceKey
    arn: str
    change_type: Literal["created", "updated", "deleted"]
    changed_at: datetime
    job_id: str
    old_hash: bytes | None
    new_hash: bytes | None
    data: Mapping[str, Any] | None


class TypeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AUTH_DENIED = "auth_denied"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool


@dataclass
class TypeResult:
    status: TypeStatus
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status.value}
        if self.status is TypeStatus.SUCCEEDED:
            payload |= {
                "created": self.created,
                "updated": self.updated,
                "deleted": self.deleted,
                "unchanged": self.unchanged,
                "duration_ms": self.duration_ms,
            }
        if self.error_code:
            payload |= {
                "error_code": self.error_code,
                "error_message": self.error_message,
                "retryable": self.retryable,
            }
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> TypeResult:
        return cls(
            status=TypeStatus(payload["status"]),
            created=payload.get("created", 0),
            updated=payload.get("updated", 0),
            deleted=payload.get("deleted", 0),
            unchanged=payload.get("unchanged", 0),
            duration_ms=payload.get("duration_ms", 0),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
            retryable=payload.get("retryable", False),
        )

    @classmethod
    def failure(cls, error: ErrorInfo, status: TypeStatus = TypeStatus.FAILED) -> TypeResult:
        return cls(
            status=status,
            error_code=error.code,
            error_message=error.message,
            retryable=error.retryable,
        )


RefreshStatus = Literal[
    "created", "updated", "unchanged", "deleted", "not_found", "unsupported_type", "error"
]


# --------------------------------------------------------------------------- scans

JobStatus = Literal["queued", "running", "succeeded", "partial", "failed", "timed_out"]
TaskStatus = Literal[
    "queued", "running", "retrying", "succeeded", "partial", "failed", "auth_denied", "timed_out"
]
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "partial", "failed", "auth_denied", "timed_out"}
)
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"succeeded", "partial", "failed", "timed_out"})
QueueKind = Literal["scheduled", "ondemand"]
Trigger = Literal["schedule", "api"]


class ScanScope(_Contract):
    account_id: str | None = Field(None, pattern=ACCOUNT_ID_PATTERN)
    region: str | None = None
    resource_type: str | None = None
    arn: str | None = None
    arns: list[str] | None = None  # internal: multi-ARN refresh jobs
    all: bool = False

    @model_validator(mode="after")
    def _shape(self) -> ScanScope:
        if self.all:
            if any((self.account_id, self.region, self.resource_type, self.arn, self.arns)):
                raise ValueError("'all' cannot be combined with other fields")
            return self
        if self.arns:
            if any((self.account_id, self.region, self.resource_type, self.arn)):
                raise ValueError("arns cannot be combined with other fields")
            return self
        if self.arn:
            if any((self.account_id, self.region, self.resource_type)):
                raise ValueError("arn cannot be combined with other fields")
            return self
        if not self.account_id:
            raise ValueError("account_id is required")
        return self

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=True)

    def scope_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.as_json()).encode()).hexdigest()


class ScanTaskMessage(_Contract):
    version: Literal[1] = 1
    job_id: str
    task_id: str
    request_id: str | None = None
    trigger: Trigger
    account_id: str = Field(pattern=ACCOUNT_ID_PATTERN)
    region: str
    resource_types: list[str] | None = None
    resource_ids: list[str] | None = None
    full_refresh: bool = False
    attempt: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _ids_need_one_type(self) -> ScanTaskMessage:
        if self.resource_ids is not None and (
            self.resource_types is None or len(self.resource_types) != 1
        ):
            raise ValueError("resource_ids requires exactly one resource_type")
        return self


class ScanTask(BaseModel):
    task_id: str
    job_id: str
    account_id: str
    region: str
    resource_types: list[str] | None = None
    resource_ids: list[str] | None = None
    queue: QueueKind
    status: TaskStatus
    attempts: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    worker_id: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime


class ScanJob(BaseModel):
    job_id: str
    trigger: Trigger
    scope: dict[str, Any]
    scope_hash: str
    status: JobStatus
    request_id: str | None = None
    flags: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    deadline_at: datetime | None = None

    @property
    def full_refresh(self) -> bool:
        return bool(self.flags.get("full_refresh"))


@dataclass(frozen=True, slots=True)
class NewTask:
    account_id: str
    region: str
    queue: QueueKind
    resource_types: list[str] | None = None
    resource_ids: list[str] | None = None
    status: TaskStatus = "queued"
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class TypeScope:
    resource_type: str
    account_id: str
    region: str


@dataclass
class ChangeSet:
    upserts: list[ResourceRecord]
    deleted_ids: list[str]
    created: int
    updated: int
    unchanged: int
    events: list[ChangeEvent]
    scan_started_at: datetime
