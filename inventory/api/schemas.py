"""Request/response models; they mirror docs/api.md one to one."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from inventory.models import ACCOUNT_ID_PATTERN, JobStatus, RefreshStatus, TaskStatus, Trigger

ARN_PATTERN = r"^arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{0,12}:.+$"
SortField = Literal["last_changed_at", "resource_id"]
SortOrder = Literal["asc", "desc"]
View = Literal["summary", "full"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class Resource(BaseModel):
    resource_type: str
    account_id: str
    region: str
    resource_id: str
    arn: str
    name: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    relations: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] | None = None
    first_seen_at: datetime
    last_changed_at: datetime
    observed_at: datetime
    deleted_at: datetime | None = None
    verified_at: datetime | None = None


class SearchRequest(_Request):
    account_ids: list[str] | None = Field(None, max_length=1000)
    regions: list[str] | None = Field(None, max_length=50)
    resource_types: list[str] | None = Field(None, max_length=100)
    tags: dict[str, str] | None = Field(None, max_length=20)
    tag_keys: list[str] | None = Field(None, max_length=20)
    name_prefix: str | None = Field(None, max_length=256)
    changed_since: datetime | None = None
    data_jsonpath: str | None = Field(None, max_length=1000)
    include_deleted: bool = False
    view: View = "summary"
    sort: SortField = "last_changed_at"
    order: SortOrder = "desc"
    limit: int = Field(100, ge=1, le=500)
    cursor: str | None = None
    with_total: bool = False


class StatsResponse(BaseModel):
    groups: list[dict[str, Any]]
    total: int


class ScanScopeIn(_Request):
    account_id: str | None = Field(None, pattern=ACCOUNT_ID_PATTERN)
    region: str | None = None
    resource_type: str | None = None
    arn: str | None = Field(None, pattern=ARN_PATTERN)


class ScanRequest(_Request):
    scope: ScanScopeIn
    wait_seconds: float = Field(0, ge=0, le=25)


class ScanTaskOut(BaseModel):
    task_id: str
    account_id: str
    region: str
    resource_types: list[str] | None = None
    status: TaskStatus
    attempts: int
    result: dict[str, Any]
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScanJobOut(BaseModel):
    job_id: str
    trigger: Trigger
    scope: dict[str, Any]
    status: JobStatus
    request_id: str | None = None
    deduplicated: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: dict[str, Any]
    error: str | None = None
    tasks: list[ScanTaskOut] | None = None


class RefreshRequest(_Request):
    arns: list[str] = Field(min_length=1, max_length=100)  # each validated below
    wait_seconds: float = Field(20, ge=0, le=25)

    @field_validator("arns")
    @classmethod
    def _arns(cls, value: list[str]) -> list[str]:
        bad = [i for i, a in enumerate(value) if len(a) > 2048 or not re.match(ARN_PATTERN, a)]
        if bad:
            raise ValueError(f"arns[{bad[0]}] is not an ARN")
        return list(dict.fromkeys(value))  # de-duplicate, keep order


class RefreshError(BaseModel):
    code: str
    message: str


class RefreshResult(BaseModel):
    arn: str
    status: RefreshStatus | Literal["pending"]
    resource: Resource | None = None
    error: RefreshError | None = None


class RefreshResponse(BaseModel):
    job: ScanJobOut
    results: list[RefreshResult] | None = None


class CoverageScope(BaseModel):
    region: str
    resource_type: str
    status: str
    resource_count: int | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None


class CoverageResponse(BaseModel):
    account_id: str
    enabled_regions: list[str]
    regions_checked_at: datetime | None = None
    scopes: list[CoverageScope]


class ResourceTypeOut(BaseModel):
    resource_type: str
    service: str
    is_global: bool
    depends_on: list[str]
    enabled_regions: list[str]


class ResourceTypesResponse(BaseModel):
    items: list[ResourceTypeOut]
