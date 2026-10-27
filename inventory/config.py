"""All settings classes. This is the only module that reads environment variables."""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from inventory.errors import NotImplementedFeatureError

GLOBAL_REGION = "global"


class RegionCell(BaseModel):
    region: str
    # boto3 service name -> endpoint URL. An empty string means "default regional DNS",
    # which only works inside the region's own VPC. Services not listed are unavailable.
    endpoints: dict[str, str] = Field(default_factory=dict)
    scheduled_queue_url: str | None = None
    ondemand_queue_url: str | None = None


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix, env_file=".env", extra="ignore", env_nested_delimiter="__"
    )


class CommonSettings(BaseSettings):
    model_config = _config("INVENTORY_")

    environment: Literal["local", "dev", "prod"] = "local"
    service_name: str = "inventory"
    log_level: str = "INFO"
    log_sample_rate: float = Field(0.0, ge=0.0, le=1.0)
    # "emf": metrics as EMF log lines (Lambda extracts them); "api": buffered PutMetricData
    # (ECS awslogs does not extract EMF)
    metrics_mode: Literal["emf", "api"] = "emf"
    home_region: str = "ap-east-1"
    supported_regions: list[str] = Field(
        default_factory=lambda: ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]
    )
    global_home_region: str = "us-east-1"
    transport: Literal["peering", "lambda"] = "peering"
    history_sink: Literal["null", "outbox"] = "null"
    cells: list[RegionCell] = Field(default_factory=list)
    broker_function_name: str = "inventory-broker"
    account_region_max_age_hours: int = 24
    database_url: SecretStr | None = None
    database_secret_arn: str | None = None  # RDS-managed secret: username + password
    database_host: str | None = None  # RDS Proxy endpoint; used when the secret has no host
    database_port: int = 5432
    database_name: str = "inventory"
    aws_endpoint_url: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> CommonSettings:
        if self.transport == "lambda":
            raise NotImplementedFeatureError("transport=lambda (方案 B) 未实现")
        if self.history_sink == "outbox":
            raise NotImplementedFeatureError("history_sink=outbox (变更历史) 未实现")
        configured = {cell.region for cell in self.cells}
        missing = [r for r in self.supported_regions if r not in configured]
        if missing:
            raise ValueError(f"supported regions without a cell: {missing}")
        if self.global_home_region not in self.supported_regions:
            raise ValueError("global_home_region must be one of supported_regions")
        return self

    def find_cell(self, region: str) -> RegionCell | None:
        lookup = self.global_home_region if region == GLOBAL_REGION else region
        return next((c for c in self.cells if c.region == lookup), None)

    def cell(self, region: str) -> RegionCell:
        found = self.find_cell(region)
        if found is None:
            raise KeyError(f"no cell configured for region {region}")
        return found


class BrokerSettings(BaseSettings):
    model_config = _config("BROKER_")

    adfs_idp_url: str
    adfs_credentials_secret_arn: str
    adfs_timeout_seconds: float = 15.0
    adfs_ca_bundle: str | None = None
    identity: str = "default"
    role_name: str = "ReadOnly"
    redis_url: str
    redis_auth_secret_arn: str | None = None
    kms_key_id: str
    sts_region: str = "ap-east-1"
    session_duration_seconds: int = 3600
    saml_refresh_margin_seconds: int = 300
    default_min_ttl_seconds: int = 1200
    lock_ttl_ms: int = 45_000
    lock_wait_timeout_ms: int = 5_000
    lock_poll_interval_ms: int = 100
    data_key_ttl_seconds: int = 3600
    adfs_failure_cache_seconds: int = 300
    redis_socket_timeout_seconds: float = 2.0

    @model_validator(mode="after")
    def _validate(self) -> BrokerSettings:
        # The credential lock covers: waiting for the SAML lock + ADFS GET/POST + STS.
        critical_ms = self.lock_wait_timeout_ms + 2 * self.adfs_timeout_seconds * 1000 + 5_000
        if self.lock_ttl_ms < critical_ms:
            raise ValueError(f"lock_ttl_ms must be >= {int(critical_ms)} for these timeouts")
        if self.session_duration_seconds < 900:
            raise ValueError("session_duration_seconds must be >= 900 (STS minimum)")
        return self


class PlannerSettings(BaseSettings):
    model_config = _config("PLANNER_")

    account_directory_function_name: str
    job_deadline_minutes: int = 210
    task_stale_minutes: int = 10
    deleted_retention_days: int = 30


class WorkerSettings(BaseSettings):
    model_config = _config("WORKER_")

    region: str
    message_concurrency: int = Field(2, ge=1)
    type_concurrency: int = Field(4, ge=1)
    visibility_timeout_seconds: int = 900
    heartbeat_interval_seconds: int = 120
    max_attempts: int = 3
    retry_base_delay_seconds: int = 60
    ondemand_wait_seconds: int = 1
    scheduled_wait_seconds: int = 10
    shutdown_grace_seconds: int = 100
    task_stale_minutes: int = 10  # 5 missed heartbeats (120s) before another worker takes over
    ecs_metadata_uri: str | None = Field(None, validation_alias="ECS_CONTAINER_METADATA_URI_V4")


class ApiSettings(BaseSettings):
    model_config = _config("API_")

    port: int = 8080
    db_pool_size: int = 10
    db_max_overflow: int = 10
    statement_timeout_ms: int = 10_000
    search_timeout_ms: int = 5_000
    wait_max_seconds: int = 25
    wait_poll_interval_ms: int = 300
    refresh_inline_max: int = 20
    refresh_max_arns: int = 100
    refresh_parallelism: int = 8
    refresh_thread_limit: int = 32
    refresh_timeout_seconds: float = 15.0
    scan_job_deadline_minutes: int = 60
    refresh_job_deadline_minutes: int = 5
    tracing_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4317"
    ui_enabled: bool = True


@functools.cache
def get_common_settings() -> CommonSettings:
    return CommonSettings()


@functools.cache
def get_broker_settings() -> BrokerSettings:
    return BrokerSettings()


@functools.cache
def get_planner_settings() -> PlannerSettings:
    return PlannerSettings()


@functools.cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()


@functools.cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()


def clear_settings_cache() -> None:
    for factory in (
        get_common_settings,
        get_broker_settings,
        get_planner_settings,
        get_worker_settings,
        get_api_settings,
    ):
        factory.cache_clear()
