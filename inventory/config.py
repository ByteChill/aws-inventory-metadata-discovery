"""All settings classes. This is the only module that reads environment variables."""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from inventory.errors import NotImplementedFeatureError

GLOBAL_REGION = "global"
# Fixed by AWS, not configuration: IAM / Route 53 APIs are served from us-east-1, and it is
# enabled for every account (unlike the opt-in primary region ap-east-1), so STS
# AssumeRoleWithSAML and ec2:DescribeRegions are called there too.
US_EAST_1 = "us-east-1"


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
    # control plane: broker, planner, API, database, Redis
    primary_region: str = "ap-east-1"
    # regions that have a worker; only the planner and the API use this
    scan_regions: list[str] = Field(
        default_factory=lambda: ["ap-east-1", "eu-west-1", "eu-west-2", US_EAST_1]
    )
    # Endpoint-specific DNS for cross-region calls only: {region: {service: url}}. Private
    # DNS is not shared over VPC peering; same-region calls use the default DNS.
    endpoints: dict[str, dict[str, str]] = Field(default_factory=dict)
    transport: Literal["peering", "lambda"] = "peering"
    history_sink: Literal["null", "outbox"] = "null"
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
        if US_EAST_1 not in self.scan_regions:
            raise ValueError(f"scan_regions must include {US_EAST_1} (global resources)")
        return self

    @property
    def name_prefix(self) -> str:
        """Prefix of every resource name, as created by Terraform."""
        return f"inventory-{self.environment}"

    @property
    def broker_function_name(self) -> str:
        return f"{self.name_prefix}-adfs-broker"

    def queue_name(self, kind: str) -> str:
        return f"{self.name_prefix}-{kind}"


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
    # soft-deleted resources are kept this long, then purged by maintenance
    deleted_retention_days: int = Field(180, ge=1)


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
    refresh_max_arns: int = 100
    scan_job_deadline_minutes: int = 60
    refresh_job_deadline_minutes: int = 5
    tracing_enabled: bool = False
    otlp_endpoint: str = "http://localhost:4317"
    ui_enabled: bool = True
    ui_dir: str | None = None  # built web UI; defaults to inventory/api/ui (filled in the image)
    # authentication (spec 10): "disabled" = anonymous with every role (local, migration period)
    auth_mode: Literal["disabled", "enforced"] = "disabled"
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwt_username_claim: str = "preferred_username"
    jwt_roles_claim: str = "roles"
    jwt_default_role: Literal["viewer", "operator", "admin"] = "viewer"

    @model_validator(mode="after")
    def _validate(self) -> ApiSettings:
        if self.auth_mode == "enforced":
            missing = [
                name
                for name in ("jwt_jwks_url", "jwt_issuer", "jwt_audience")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"auth_mode=enforced requires {', '.join(missing)}")
        return self


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
