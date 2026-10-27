"""AWS client creation and endpoint routing.

Every boto3 client in the project is created through :class:`ClientFactory`, which applies
the cross-region endpoint routing (``INVENTORY_ENDPOINTS``), the retry policy and call logging.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ValidationError

from inventory import __version__
from inventory.config import GLOBAL_REGION, US_EAST_1, CommonSettings
from inventory.errors import AccessDeniedError, InventoryError, ThrottledError, UpstreamError
from inventory.models import (
    RETRYABLE_BROKER_CODES,
    AwsCredentials,
    BrokerErrorResponse,
    GetCredentialsRequest,
    GetCredentialsResponse,
    ListAccessibleAccountsRequest,
    ListAccessibleAccountsResponse,
)
from inventory.observability import get_context, register_call_logging

# boto3 clients are dynamically typed; the project treats them as Any.
AwsClient = Any


class EndpointResolver:
    def __init__(self, settings: CommonSettings) -> None:
        self._settings = settings

    def api_region(self, region: str) -> str:
        """The region API calls go to; 'global' resources are served from us-east-1."""
        return US_EAST_1 if region == GLOBAL_REGION else region

    def endpoint_url(self, service: str, region: str) -> str | None:
        """Endpoint-specific DNS for a cross-region call, else None (default DNS)."""
        if self._settings.aws_endpoint_url:
            return self._settings.aws_endpoint_url
        return self._settings.endpoints.get(self.api_region(region), {}).get(service) or None


class ClientFactory:
    def __init__(self, resolver: EndpointResolver, max_pool_connections: int = 32) -> None:
        self.resolver = resolver
        self._config = Config(
            retries={"mode": "adaptive", "max_attempts": 10},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=max_pool_connections,
            user_agent_extra=f"inventory/{__version__}",
        )
        self._default_session = boto3.Session()
        # boto3 sessions are not thread-safe when creating clients.
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], AwsClient] = {}

    def client(
        self,
        service: str,
        region: str,
        session: boto3.Session | None = None,
        config: Config | None = None,
    ) -> AwsClient:
        """Create a client; ``config`` overrides parts of the default retry/timeout config.

        Clients on the default session are cached (boto3 clients are thread-safe); clients
        on per-account sessions are not, as those sessions carry their own credentials.
        """
        api_region = self.resolver.api_region(region)
        cache_key = (service, region) if session is None and config is None else None
        if cache_key is not None and cache_key in self._cache:
            return self._cache[cache_key]
        with self._lock:
            client = (session or self._default_session).client(
                service,  # type: ignore[call-overload]
                region_name=api_region,
                endpoint_url=self.resolver.endpoint_url(service, region),
                config=self._config.merge(config) if config else self._config,
            )
        register_call_logging(client)
        if cache_key is not None:
            self._cache[cache_key] = client
        return client


# --------------------------------------------------------------------------- broker client


class BrokerTransport(Protocol):
    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class LambdaBrokerTransport:
    """Invokes the broker Lambda synchronously (cross-region via the peering endpoint)."""

    def __init__(
        self,
        clients: ClientFactory,
        function_name: str,
        region: str,
        read_timeout_seconds: int = 70,
    ) -> None:
        """``read_timeout_seconds`` must exceed the broker function timeout (60s)."""
        self._clients = clients
        self._function_name = function_name
        self._region = region
        # A broker call can run ADFS + STS; never let botocore re-invoke it many times.
        self._config = Config(
            read_timeout=read_timeout_seconds, retries={"mode": "standard", "max_attempts": 2}
        )
        self._client: AwsClient | None = None

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = self._clients.client("lambda", self._region, config=self._config)
        try:
            response = self._client.invoke(
                FunctionName=self._function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode(),
            )
        except (ClientError, BotoCoreError) as exc:
            raise UpstreamError(
                f"broker invoke failed: {type(exc).__name__}", retryable=True
            ) from exc
        try:
            body: dict[str, Any] = json.loads(response["Payload"].read() or b"{}")
        except ValueError:
            raise UpstreamError("broker returned invalid JSON", retryable=True) from None
        if response.get("FunctionError"):
            raise UpstreamError(
                f"broker function error: {body.get('errorType', 'unknown')}",
                code="BROKER_FUNCTION_ERROR",
                retryable=True,
            )
        return body


_BROKER_ERRORS: dict[str, type[InventoryError]] = {
    "ACCESS_DENIED": AccessDeniedError,
    "THROTTLED": ThrottledError,
}


@dataclass(frozen=True, slots=True)
class _CachedCredentials:
    credentials: AwsCredentials
    issued_at: datetime

    def is_fresh(self, now: datetime, min_ttl: int) -> bool:
        lifetime = (self.credentials.expiration - self.issued_at).total_seconds()
        # Short sessions (below min_ttl) would otherwise be re-fetched on every call.
        return self.credentials.remaining_seconds(now) >= min(min_ttl, lifetime / 2)


class BrokerLambdaProvider:
    """CredentialProvider backed by the broker, with an in-process cache per account."""

    def __init__(
        self,
        transport: BrokerTransport,
        min_ttl_seconds: int = 1200,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transport = transport
        self._min_ttl = min_ttl_seconds
        self._clock = clock
        self._cache: dict[str, _CachedCredentials] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def credentials(self, account_id: str, min_ttl_seconds: int | None = None) -> AwsCredentials:
        min_ttl = self._min_ttl if min_ttl_seconds is None else min_ttl_seconds
        cached = self._fresh(account_id, min_ttl)
        if cached is not None:
            return cached
        with self._lock_for(account_id):
            cached = self._fresh(account_id, min_ttl)
            if cached is not None:
                return cached
            request = GetCredentialsRequest(
                account_id=account_id, min_ttl_seconds=min_ttl, correlation_id=_correlation_id()
            )
            body = self._call(request.model_dump(mode="json"))
            credentials = _parse(GetCredentialsResponse, body).credentials
            self._cache[account_id] = _CachedCredentials(credentials, self._clock())
            return credentials

    def session(self, account_id: str) -> boto3.Session:
        def metadata() -> dict[str, str]:
            creds = self.credentials(account_id)
            return {
                "access_key": creds.access_key_id,
                "secret_key": creds.secret_access_key.get_secret_value(),
                "token": creds.session_token.get_secret_value(),
                "expiry_time": creds.expiration.isoformat(),
            }

        refreshable = RefreshableCredentials.create_from_metadata(
            metadata=metadata(), refresh_using=metadata, method="inventory-broker"
        )
        botocore_session = botocore.session.get_session()
        botocore_session._credentials = refreshable  # type: ignore[attr-defined]
        return boto3.Session(botocore_session=botocore_session)

    def accessible_accounts(self) -> set[str]:
        request = ListAccessibleAccountsRequest(correlation_id=_correlation_id())
        body = self._call(request.model_dump(mode="json"))
        return set(_parse(ListAccessibleAccountsResponse, body).account_ids)

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._transport.invoke(payload)
        if body.get("status") == "error":
            error = _parse(BrokerErrorResponse, body)
            # retryability comes from the error code table, not from the wire
            error_type = _BROKER_ERRORS.get(error.code, UpstreamError)
            raise error_type(error.message, retryable=error.code in RETRYABLE_BROKER_CODES)
        return body

    def _fresh(self, account_id: str, min_ttl: int) -> AwsCredentials | None:
        cached = self._cache.get(account_id)
        if cached and cached.is_fresh(self._clock(), min_ttl):
            return cached.credentials
        return None

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, threading.Lock())


def _parse[M: BaseModel](model: type[M], body: dict[str, Any]) -> M:
    try:
        return model.model_validate(body)
    except ValidationError:
        # never chain: the validation error text can contain credential values
        raise UpstreamError("broker returned an unexpected response", retryable=True) from None


def _correlation_id() -> str | None:
    context = get_context()
    return context.get("request_id") or context.get("job_id")
