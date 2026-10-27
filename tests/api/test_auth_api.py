"""Permissions on every route, API keys and requested_by (spec 10)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from inventory.api.app import create_app
from inventory.api.auth import Authenticator
from inventory.bootstrap import Runtime
from inventory.observability import ContextFilter
from tests.api.auth_support import StaticJwks, auth_settings, bearer
from tests.api.conftest import new_async_engine
from tests.helpers import load_fixture

pytestmark = pytest.mark.integration
ROLES = ("viewer", "operator", "admin")


@pytest.fixture
def secured(runtime: Runtime) -> Iterator[TestClient]:
    settings = auth_settings()
    app = create_app(
        settings,
        runtime=runtime,
        async_engine=new_async_engine(),
        authenticator=Authenticator(settings, runtime.db, jwks=StaticJwks()),  # type: ignore[arg-type]
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    "case",
    load_fixture("api/auth.permissions.cases.json"),
    ids=lambda c: f"{c['method']} {c['path']}",
)
def test_permissions(secured: TestClient, case: dict[str, Any]) -> None:
    def call(headers: dict[str, str]) -> Any:
        return secured.request(case["method"], case["path"], json=case.get("body"), headers=headers)

    anonymous = call({})
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "UNAUTHENTICATED"
    assert anonymous.headers["www-authenticate"] == "Bearer"
    for role in ROLES:
        response = call(bearer(role))
        if role in case["allowed"]:
            assert response.status_code == case.get("allowed_status", 200), (role, response.text)
        else:
            assert response.status_code == 403, role
            assert response.json()["error"]["code"] == "FORBIDDEN"


def test_unauthenticated_routes_stay_open(secured: TestClient) -> None:
    assert secured.get("/healthz").status_code == 200
    assert secured.get("/openapi.json").status_code == 200


def test_me(secured: TestClient) -> None:
    me = secured.get("/v1/me", headers=bearer("operator")).json()

    assert me == {
        "subject": "10000002",
        "kind": "user",
        "roles": ["operator"],
        "permissions": ["resources:read", "scans:read", "scans:write"],
        "auth_mode": "enforced",
    }


def test_api_key_lifecycle(secured: TestClient) -> None:
    created = secured.post(
        "/v1/api-keys", json=load_fixture("api/auth.api_key.request.json"), headers=bearer("admin")
    )
    body = created.json()
    key = {"X-API-Key": body["secret"]}

    me = secured.get("/v1/me", headers=key).json()
    read = secured.get("/v1/resources", headers=key)
    write = secured.post("/v1/scans", json={"scope": {"account_id": "123456789012"}}, headers=key)
    listed = secured.get("/v1/api-keys", headers=bearer("admin")).json()["items"]
    revoked = secured.delete(f"/v1/api-keys/{body['key']['key_id']}", headers=bearer("admin"))
    after = secured.get("/v1/resources", headers=key)
    again = secured.delete(f"/v1/api-keys/{body['key']['key_id']}", headers=bearer("admin"))

    assert created.status_code == 201 and body["secret"].startswith("inv_")
    assert body["key"]["created_by"] == "10000003" and body["key"]["roles"] == ["viewer"]
    assert (me["subject"], me["kind"]) == (f"apikey:{body['key']['key_id']}", "service")
    assert read.status_code == 200 and write.status_code == 403
    assert [k["key_id"] for k in listed] == [body["key"]["key_id"]]
    assert "secret" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["last_used_at"] is not None
    assert revoked.status_code == 204 and after.status_code == 401 and again.status_code == 404


def test_expired_and_forged_api_keys(secured: TestClient) -> None:
    expired = secured.post(
        "/v1/api-keys",
        json=load_fixture("api/auth.api_key_expired.request.json"),
        headers=bearer("admin"),
    ).json()
    key_id = expired["key"]["key_id"]
    forged = f"inv_{key_id}_{'A' * 43}"

    assert secured.get("/v1/resources", headers={"X-API-Key": expired["secret"]}).status_code == 401
    assert secured.get("/v1/resources", headers={"X-API-Key": forged}).status_code == 401
    assert (
        secured.post(
            "/v1/api-keys",
            json=load_fixture("api/auth.api_key.request.json"),
            headers=bearer("operator"),
        ).status_code
        == 403
    )


def test_scan_records_who_requested_it(secured: TestClient) -> None:
    response = secured.post(
        "/v1/scans",
        json=load_fixture("api/scan.account_region.request.json"),
        headers=bearer("operator"),
    )
    job = secured.get(f"/v1/scans/{response.json()['job_id']}", headers=bearer("viewer")).json()

    assert response.json()["requested_by"] == "10000002"
    assert job["requested_by"] == "10000002"


def test_disabled_mode_is_anonymous_but_cannot_manage_keys(client: TestClient) -> None:
    me = client.get("/v1/me").json()
    created = client.post("/v1/api-keys", json=load_fixture("api/auth.api_key.request.json"))

    assert (me["subject"], me["auth_mode"]) == ("anonymous", "disabled")
    # keys created while everyone is an admin would stay valid after enforcement
    assert created.status_code == 403 and created.json()["error"]["code"] == "FORBIDDEN"
    assert client.get("/v1/api-keys").status_code == 403


def test_principal_is_in_the_log_context(
    secured: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="inventory.api.access")
    caplog.handler.addFilter(ContextFilter())

    secured.get("/v1/resources", headers=bearer("viewer"))

    (record,) = [r for r in caplog.records if getattr(r, "path", None) == "/v1/resources"]
    assert getattr(record, "principal", None) == "10000001"


def test_every_v1_route_declares_a_permission(secured: TestClient) -> None:
    def permissions(dependant: Any) -> set[str]:
        found = {getattr(dependant.call, "permission", None)}
        for sub in dependant.dependencies:
            found |= permissions(sub)
        return found - {None}

    unguarded = [
        route.path
        for route in secured.app.routes  # type: ignore[attr-defined]
        if isinstance(route, APIRoute)
        and route.path.startswith("/v1/")
        and route.path != "/v1/me"
        and not permissions(route.dependant)
    ]
    assert unguarded == []
