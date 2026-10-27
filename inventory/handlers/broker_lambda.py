"""Lambda entry point of the credential broker."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Mapping
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.utilities.parameters import SecretsProvider
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import SecretStr, ValidationError
from redis import Redis

from inventory.aws import ClientFactory, EndpointResolver
from inventory.broker import BrokerError
from inventory.broker.adfs import AdfsClient
from inventory.broker.cache import RedisCache
from inventory.broker.crypto import EnvelopeCipher
from inventory.broker.service import BrokerService
from inventory.broker.sts import StsAssumer
from inventory.config import US_EAST_1, get_broker_settings, get_common_settings
from inventory.models import (
    BROKER_REQUEST_ADAPTER,
    BrokerErrorResponse,
    BrokerResponse,
    GetCredentialsRequest,
)
from inventory.observability import context_scope, install_filters, setup_logging

SERVICE_NAME = "inventory-broker"

logger = Logger(service=SERVICE_NAME)
tracer = Tracer(service=SERVICE_NAME)
metrics = Metrics(namespace="Inventory", service=SERVICE_NAME)
log = logging.getLogger(__name__)


@functools.cache
def get_service() -> BrokerService:
    common = get_common_settings()
    settings = get_broker_settings()
    setup_logging(SERVICE_NAME, common.log_level, common.log_sample_rate)
    install_filters(logging.getLogger(SERVICE_NAME))  # the Powertools logger

    clients = ClientFactory(EndpointResolver(common))
    # secrets and the KMS key live in the primary region; STS is called in us-east-1, which
    # every account has enabled (the primary region is opt-in)
    secrets = SecretsProvider(boto3_client=clients.client("secretsmanager", common.primary_region))

    def adfs_credentials() -> tuple[str, SecretStr]:
        # Read through the provider's TTL cache on every login so rotations are picked up.
        secret: dict[str, str] = secrets.get(  # type: ignore[assignment]
            settings.adfs_credentials_secret_arn, transform="json", max_age=300
        )
        return secret["username"], SecretStr(secret["password"])

    redis_password = (
        str(secrets.get(settings.redis_auth_secret_arn, max_age=300))
        if settings.redis_auth_secret_arn
        else None
    )
    cipher = EnvelopeCipher(
        clients.client("kms", common.primary_region),
        settings.kms_key_id,
        settings.data_key_ttl_seconds,
    )
    redis = Redis.from_url(
        settings.redis_url,
        password=redis_password,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
    )
    cache = RedisCache(
        redis,
        cipher,
        settings.identity,
        settings.saml_refresh_margin_seconds,
        settings.lock_ttl_ms,
    )
    adfs = AdfsClient(
        settings.adfs_idp_url,
        adfs_credentials,
        settings.adfs_timeout_seconds,
        settings.adfs_ca_bundle,
    )
    sts = StsAssumer(clients.client("sts", US_EAST_1), settings.session_duration_seconds)
    return BrokerService(settings, cache, adfs, sts)


def process(event: Mapping[str, Any], service: BrokerService) -> dict[str, Any]:
    """Validate, dispatch and always return a JSON-able response (errors included)."""
    started = time.perf_counter()
    action = str(event.get("action", "")) if isinstance(event, Mapping) else ""
    response: BrokerResponse
    try:
        request = BROKER_REQUEST_ADAPTER.validate_python(event)
    except ValidationError as exc:
        response = BrokerErrorResponse(
            code="INVALID_REQUEST",
            message=f"invalid request: {exc.error_count()} validation error(s)",
            retryable=False,
        )
        _log_summary(action, None, response, started)
        return response.model_dump(mode="json")

    account_id = request.account_id if isinstance(request, GetCredentialsRequest) else None
    with context_scope(request_id=request.correlation_id, account_id=account_id):
        try:
            response = service.handle(request)
        except BrokerError as exc:
            if exc.code == "INTERNAL":
                cause = exc.__cause__
                log.warning(
                    "broker internal error",
                    extra={
                        "error_message": exc.message,
                        "cause": type(cause).__name__ if cause else None,
                    },
                )
            response = BrokerErrorResponse(
                code=exc.code, message=exc.message, retryable=exc.retryable
            )
        except Exception:
            log.exception("broker request failed unexpectedly")
            response = BrokerErrorResponse(
                code="INTERNAL", message="internal error", retryable=True
            )
        _log_summary(action, account_id, response, started)
    return response.model_dump(mode="json")


def _log_summary(
    action: str, account_id: str | None, response: BrokerResponse, started: float
) -> None:
    log.info(
        "broker request",
        extra={
            "action": action,
            "account_id": account_id,
            "status": response.status,
            "source": getattr(response, "source", None),
            "code": getattr(response, "code", None),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
    )


@logger.inject_lambda_context(correlation_id_path="correlation_id")
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    service = get_service()
    with context_scope(aws_request_id=context.aws_request_id):
        return process(event, service)
