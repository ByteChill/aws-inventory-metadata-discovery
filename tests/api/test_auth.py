"""Authenticator and roles (spec 10). JWT checks need no database."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from inventory.api.auth import ANONYMOUS, ROLE_PERMISSIONS, Authenticator
from inventory.config import ApiSettings
from inventory.errors import UnauthenticatedError
from tests.api.auth_support import StaticJwks, auth_settings, token
from tests.helpers import load_fixture


def authenticator(**overrides: Any) -> Authenticator:
    return Authenticator(auth_settings(**overrides), db=None, jwks=StaticJwks())  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(load_fixture("api/auth.tokens.cases.json")))
def test_bearer_tokens(name: str) -> None:
    expected = load_fixture("api/auth.principals.expected.json")[name]
    if expected == "UNAUTHENTICATED":
        with pytest.raises(UnauthenticatedError):
            authenticator().authenticate(token(name), None)
        return

    principal = authenticator().authenticate(token(name), None)

    actual = {
        "subject": principal.subject,
        "kind": principal.kind,
        "roles": sorted(principal.roles),
    }
    assert actual == expected


def test_disabled_mode_is_an_anonymous_admin() -> None:
    principal = authenticator(auth_mode="disabled").authenticate(None, None)

    assert principal is ANONYMOUS
    assert principal.permissions == ROLE_PERMISSIONS["admin"]


@pytest.mark.parametrize(
    ("bearer", "key"),
    [(None, None), ("t", "k"), (None, "not-a-key"), (None, "inv_short_secret")],
)
def test_missing_ambiguous_or_malformed_credentials(bearer: str | None, key: str | None) -> None:
    with pytest.raises(UnauthenticatedError):
        authenticator().authenticate(bearer, key)


def test_roles_are_cumulative() -> None:
    assert ROLE_PERMISSIONS["viewer"] < ROLE_PERMISSIONS["operator"] < ROLE_PERMISSIONS["admin"]
    assert ROLE_PERMISSIONS["viewer"] == {"resources:read"}
    assert "api_keys:manage" in ROLE_PERMISSIONS["admin"]


def test_enforced_mode_requires_the_sso_settings() -> None:
    with pytest.raises(ValidationError, match="jwt_jwks_url"):
        ApiSettings(auth_mode="enforced")
