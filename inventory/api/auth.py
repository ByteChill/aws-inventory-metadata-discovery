"""Authentication and role-based access control for the API (spec 10).

Users present an SSO-issued JWT (``Authorization: Bearer``); services present an API key
(``X-API-Key``). With ``auth_mode=disabled`` every caller is an anonymous admin.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import jwt
from anyio import CapacityLimiter
from anyio.to_thread import run_sync
from fastapi import Depends
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from ulid import ULID

from inventory.api.state import ApiState, get_state
from inventory.config import ApiSettings
from inventory.db import ApiKeyRepository
from inventory.errors import ForbiddenError, UnauthenticatedError, UpstreamError
from inventory.models import utcnow
from inventory.observability import bind_context

log = logging.getLogger(__name__)

Permission = Literal["resources:read", "scans:read", "scans:write", "api_keys:manage"]
Role = Literal["viewer", "operator", "admin"]

_VIEWER: frozenset[Permission] = frozenset({"resources:read"})
_OPERATOR: frozenset[Permission] = _VIEWER | {"scans:read", "scans:write"}
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": _VIEWER,
    "operator": _OPERATOR,
    "admin": _OPERATOR | {"api_keys:manage"},
}
USERNAME = re.compile(r"[0-9]{8}")  # fullmatch: ASCII digits only, no trailing newline
API_KEY = re.compile(r"^inv_([0-9A-HJKMNP-TV-Z]{26})_([A-Za-z0-9_-]{43})$")
KEY_TOUCH_INTERVAL = timedelta(minutes=5)
JWKS_TIMEOUT_SECONDS = 3
JWT_LEEWAY_SECONDS = 30  # clock skew between the SSO and the API
# authentication has its own threads: a slow JWKS endpoint must not starve the routes
AUTH_LIMITER_THREADS = 16


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str  # 8-digit username, apikey:<key_id>, or anonymous
    kind: Literal["user", "service", "anonymous"]
    roles: frozenset[str]

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset(p for role in self.roles for p in ROLE_PERMISSIONS.get(role, ()))


ANONYMOUS = Principal("anonymous", "anonymous", frozenset(ROLE_PERMISSIONS))


def hash_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def new_api_key() -> tuple[str, str, bytes]:
    """Returns (key_id, the full key shown once, hash of the secret part)."""
    key_id = str(ULID())
    secret = secrets.token_urlsafe(32)
    return key_id, f"inv_{key_id}_{secret}", hash_secret(secret)


class Authenticator:
    def __init__(
        self,
        settings: ApiSettings,
        db: Engine,
        *,
        jwks: jwt.PyJWKClient | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.db = db
        self.keys = ApiKeyRepository()
        self.clock = clock
        if jwks is None and settings.jwt_jwks_url:
            # no per-kid cache: a key the SSO removes stops working within the set's lifespan
            jwks = jwt.PyJWKClient(
                settings.jwt_jwks_url,
                cache_keys=False,
                lifespan=300,
                timeout=JWKS_TIMEOUT_SECONDS,
            )
        self._jwks = jwks
        self.limiter = CapacityLimiter(AUTH_LIMITER_THREADS)

    @property
    def enabled(self) -> bool:
        return self.settings.auth_mode == "enforced"

    def authenticate(self, token: str | None, api_key: str | None) -> Principal:
        """Blocking (JWKS fetch, database): call from a worker thread."""
        if not self.enabled:
            return ANONYMOUS
        if token and api_key:
            raise UnauthenticatedError("send either a bearer token or an API key, not both")
        if api_key:
            return self._from_api_key(api_key)
        if token:
            return self._from_jwt(token)
        raise UnauthenticatedError("authentication required")

    def _from_jwt(self, token: str) -> Principal:
        settings = self.settings
        if self._jwks is None:
            raise UnauthenticatedError("bearer tokens are not configured")
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=settings.jwt_algorithms,
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
                options={"require": ["exp", "iss", "aud"]},
                leeway=JWT_LEEWAY_SECONDS,
            )
        except jwt.PyJWKClientConnectionError as exc:
            raise UpstreamError("could not fetch the SSO signing keys", retryable=True) from exc
        except jwt.PyJWTError as exc:
            log.info("invalid bearer token", extra={"error_code": type(exc).__name__})
            raise UnauthenticatedError("invalid token") from None
        username = claims.get(settings.jwt_username_claim)
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise UnauthenticatedError("the token's username must be 8 digits")
        claimed = claims.get(settings.jwt_roles_claim) or []
        roles = frozenset(
            r for r in (claimed if isinstance(claimed, list) else []) if r in ROLE_PERMISSIONS
        )
        return Principal(username, "user", roles or frozenset({settings.jwt_default_role}))

    def _from_api_key(self, value: str) -> Principal:
        match = API_KEY.match(value)
        if match is None:
            raise UnauthenticatedError("invalid API key")
        key_id, secret = match.groups()
        now = self.clock()
        with self.db.connect() as conn:
            row = self.keys.get(conn, key_id)
        if row is None or not hmac.compare_digest(bytes(row["key_hash"]), hash_secret(secret)):
            raise UnauthenticatedError("invalid API key")
        if row["revoked_at"] is not None:
            raise UnauthenticatedError("API key revoked")
        if row["expires_at"] is not None and row["expires_at"] <= now:
            raise UnauthenticatedError("API key expired")
        last_used = row["last_used_at"]
        if last_used is None or last_used < now - KEY_TOUCH_INTERVAL:
            with self.db.begin() as conn:
                self.keys.touch(conn, key_id, now, KEY_TOUCH_INTERVAL)
        roles = frozenset(r for r in row["roles"] if r in ROLE_PERMISSIONS)
        return Principal(f"apikey:{key_id}", "service", roles)


# --------------------------------------------------------------------------- FastAPI dependencies

_bearer = HTTPBearer(auto_error=False, description="SSO-issued JWT")
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")
State = Annotated[ApiState, Depends(get_state)]


async def current_principal(
    state: State,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, Depends(_api_key)],
) -> Principal:
    auth = state.auth
    if not auth.enabled:
        principal = ANONYMOUS
    else:
        token = bearer.credentials if bearer else None
        principal = await run_sync(auth.authenticate, token, api_key, limiter=auth.limiter)
    # reset with the request context by RequestContextMiddleware
    bind_context(principal=principal.subject)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    async def check(principal: CurrentPrincipal) -> Principal:
        if permission not in principal.permissions:
            raise ForbiddenError(f"{permission} is required")
        return principal

    check.permission = permission  # type: ignore[attr-defined]  # read by the route audit test
    return check
