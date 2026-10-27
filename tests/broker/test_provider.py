from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import pytest
import time_machine

from inventory.aws import BrokerLambdaProvider, LambdaBrokerTransport
from inventory.errors import AccessDeniedError, ThrottledError, UpstreamError
from inventory.observability import context_scope
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture


class FakeTransport:
    def __init__(self, *responses: str) -> None:
        self.responses = [load_fixture(f"broker/provider/{name}") for name in responses]
        self.payloads: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.responses.pop(0)


def provider(transport: FakeTransport) -> BrokerLambdaProvider:
    return BrokerLambdaProvider(transport, clock=lambda: FROZEN_NOW)


def test_credentials_are_cached_per_account() -> None:
    transport = FakeTransport("get_credentials.ok.response.json")
    broker = provider(transport)

    with context_scope(request_id="req-123"):
        first = broker.credentials("123456789012")
    second = broker.credentials("123456789012")

    assert first is second
    assert len(transport.payloads) == 1
    assert transport.payloads[0]["correlation_id"] == "req-123"
    assert transport.payloads[0]["min_ttl_seconds"] == 1200


def test_short_lived_cached_credentials_are_refetched() -> None:
    transport = FakeTransport(
        "get_credentials.short.response.json", "get_credentials.ok.response.json"
    )
    now = [FROZEN_NOW]
    broker = BrokerLambdaProvider(transport, clock=lambda: now[0])

    broker.credentials("123456789012")  # 600s session, issued 08:00
    assert broker.credentials("123456789012").access_key_id == "ASIAEXAMPLEEXAMPLE02"
    now[0] = FROZEN_NOW + timedelta(minutes=6)  # 240s left < half the lifetime
    refreshed = broker.credentials("123456789012")

    assert refreshed.access_key_id == "ASIAEXAMPLEEXAMPLE01"
    assert len(transport.payloads) == 2


@pytest.mark.parametrize(
    ("response", "error_type", "retryable"),
    [
        ("error.access_denied.response.json", AccessDeniedError, False),
        ("error.throttled.response.json", ThrottledError, True),
        ("error.adfs_unavailable.response.json", UpstreamError, True),
        ("error.adfs_auth_failed.response.json", UpstreamError, False),
        ("error.internal.response.json", UpstreamError, True),
        ("error.invalid_request.response.json", UpstreamError, False),
    ],
)
def test_error_mapping(response: str, error_type: type[Exception], retryable: bool) -> None:
    with pytest.raises(error_type) as info:
        provider(FakeTransport(response)).credentials("111122223333")
    assert info.value.retryable is retryable  # type: ignore[attr-defined]


@pytest.mark.usefixtures("frozen_time")  # the fixture credentials expire at 09:00Z
def test_session_uses_broker_credentials() -> None:
    session = provider(FakeTransport("get_credentials.ok.response.json")).session("123456789012")

    frozen = session.get_credentials().get_frozen_credentials()  # type: ignore[union-attr]
    assert frozen.access_key == "ASIAEXAMPLEEXAMPLE01"
    assert frozen.token == "fake-session-token"


def test_accessible_accounts() -> None:
    accounts = provider(FakeTransport("list_accessible_accounts.response.json"))
    assert accounts.accessible_accounts() == {"123456789012", "210987654321"}


class FakeLambdaClient:
    def __init__(self, body: dict[str, Any], function_error: str | None = None) -> None:
        self.body = body
        self.function_error = function_error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        response: dict[str, Any] = {"Payload": io.BytesIO(json.dumps(self.body).encode())}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


class FakeFactory:
    def __init__(self, client: FakeLambdaClient) -> None:
        self.lambda_client = client

    def client(self, service: str, region: str, config: object = None) -> FakeLambdaClient:
        assert (service, region) == ("lambda", "ap-east-1")
        return self.lambda_client


def test_lambda_transport_invokes_synchronously() -> None:
    body = load_fixture("broker/provider/list_accessible_accounts.response.json")
    fake = FakeLambdaClient(body)
    transport = LambdaBrokerTransport(FakeFactory(fake), "inventory-broker", "ap-east-1")  # type: ignore[arg-type]

    assert transport.invoke({"action": "list_accessible_accounts"}) == body
    assert fake.calls[0]["InvocationType"] == "RequestResponse"


def test_lambda_function_error_is_retryable_upstream_error() -> None:
    fake = FakeLambdaClient({"errorType": "Runtime.ExitError"}, function_error="Unhandled")
    transport = LambdaBrokerTransport(FakeFactory(fake), "inventory-broker", "ap-east-1")  # type: ignore[arg-type]

    with pytest.raises(UpstreamError) as info:
        transport.invoke({"action": "get_saml"})
    assert info.value.retryable
    assert info.value.code == "BROKER_FUNCTION_ERROR"


def test_unexpected_response_is_wrapped_without_leaking_values() -> None:
    with pytest.raises(UpstreamError) as info:
        provider(FakeTransport("malformed.response.json")).credentials("123456789012")

    assert info.value.retryable
    assert info.value.__cause__ is None
    assert "tail-of-a-secret-token" not in str(info.value)


def test_session_refreshes_credentials_before_expiry() -> None:
    transport = FakeTransport(
        "get_credentials.ok.response.json", "get_credentials.later.response.json"
    )
    with time_machine.travel(FROZEN_NOW, tick=False) as traveller:
        session = BrokerLambdaProvider(transport).session("123456789012")
        credentials = session.get_credentials()
        assert credentials is not None
        assert credentials.get_frozen_credentials().access_key == "ASIAEXAMPLEEXAMPLE01"

        traveller.move_to(FROZEN_NOW + timedelta(minutes=50))  # 10 min before expiry
        refreshed = credentials.get_frozen_credentials()

    assert refreshed.access_key == "ASIAEXAMPLEEXAMPLE03"
    assert len(transport.payloads) == 2
