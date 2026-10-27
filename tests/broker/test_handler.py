from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inventory.broker.service import BrokerService
from inventory.handlers import broker_lambda
from tests.helpers import load_fixture


def subset(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    return {k: actual.get(k) for k in expected}


@pytest.mark.parametrize(
    ("request_fixture", "expected_fixture"),
    [
        ("get_credentials.request.json", "get_credentials.response.json"),
        ("get_credentials.denied.request.json", "get_credentials.denied.response.json"),
        ("list_accessible_accounts.request.json", "list_accessible_accounts.response.json"),
        ("get_saml.request.json", "get_saml.response.json"),
        ("invalid.request.json", "invalid.response.json"),
    ],
)
def test_process_returns_contract_responses(
    service: BrokerService, request_fixture: str, expected_fixture: str
) -> None:
    expected = load_fixture(f"broker/{expected_fixture}")

    response = broker_lambda.process(load_fixture(f"broker/{request_fixture}"), service)

    assert subset(response, expected) == expected


def test_credentials_are_revealed_only_on_the_wire(service: BrokerService) -> None:
    response = broker_lambda.process(load_fixture("broker/get_credentials.request.json"), service)

    assert response["credentials"]["secret_access_key"] == "fake-secret-access-key"
    assert response["credentials"]["session_token"] == "fake-session-token"


def test_unexpected_errors_become_internal(service: BrokerService) -> None:
    class Boom(BrokerService):
        def handle(self, request: Any) -> Any:
            raise RuntimeError("unexpected")

    boom = Boom.__new__(Boom)
    response = broker_lambda.process(load_fixture("broker/get_saml.request.json"), boom)

    assert response == {
        "status": "error",
        "code": "INTERNAL",
        "message": "internal error",
        "retryable": True,
    }


def test_handler_entrypoint(
    service: BrokerService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(broker_lambda, "get_service", lambda: service)
    context = SimpleNamespace(**load_fixture("broker/lambda_context.json"))

    response = broker_lambda.handler(load_fixture("broker/get_credentials.request.json"), context)

    saml = broker_lambda.handler(load_fixture("broker/get_saml.request.json"), context)

    assert response["status"] == "ok" and saml["status"] == "ok"
    out = capsys.readouterr().out
    assert "fake-secret-access-key" not in out
    assert "fake-session-token" not in out
    assert saml["saml_response"] not in out
