"""JWT test support: tokens signed with the fixture keys, and a JWKS client that reads the
fixture JWKS instead of fetching it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from inventory.config import ApiSettings
from tests.helpers import load_fixture

KID = "test-sso-1"


class StaticJwks:
    def __init__(self) -> None:
        self._keys = jwt.PyJWKSet.from_dict(load_fixture("api/auth.jwks.json"))

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in self._keys.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"no key {kid}")


def auth_settings(**overrides: Any) -> ApiSettings:
    return ApiSettings(**(load_fixture("api/auth.settings.json") | overrides))


def token(name: str) -> str:
    """A token for a case in api/auth.tokens.cases.json."""
    case = load_fixture("api/auth.tokens.cases.json")[name]
    settings = auth_settings()
    now = datetime.now(UTC)
    claims = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=case.get("expires_in", 300)),
    } | case["claims"]
    for claim in case.get("drop", []):
        claims.pop(claim)
    if case.get("alg") == "HS256":  # algorithm confusion: must be rejected (RS256 only)
        return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": KID})
    key = load_fixture(f"api/auth.{case.get('key', 'sso')}_private_key.pem")
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def bearer(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(name)}"}
