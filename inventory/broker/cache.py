"""Redis cache for SAML assertions and STS credentials, plus single-flight locks."""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from redis import Redis
from redis.exceptions import RedisError

from inventory.broker import BrokerError
from inventory.broker.crypto import EnvelopeCipher
from inventory.broker.saml import RolePair, SamlAssertion
from inventory.models import AwsCredentials

log = logging.getLogger(__name__)

PAYLOAD_VERSION = 1
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


@dataclass(frozen=True, slots=True)
class CachedCredential:
    role_arn: str
    credentials: AwsCredentials
    issued_at: datetime

    def lifetime_seconds(self) -> float:
        return (self.credentials.expiration - self.issued_at).total_seconds()


class RedisCache:
    def __init__(
        self,
        redis: Redis,
        cipher: EnvelopeCipher,
        identity: str,
        saml_refresh_margin_seconds: int = 300,
        lock_ttl_ms: int = 45_000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis
        self._cipher = cipher
        self._identity = identity
        self._margin = saml_refresh_margin_seconds
        self._lock_ttl_ms = lock_ttl_ms
        self._clock = clock
        self._release = redis.register_script(_RELEASE_SCRIPT)

    # keys --------------------------------------------------------------------------
    @property
    def saml_key(self) -> str:
        return f"broker:saml:{self._identity}"

    @property
    def roles_key(self) -> str:
        return f"broker:roles:{self._identity}"

    @property
    def saml_lock_key(self) -> str:
        return f"broker:lock:saml:{self._identity}"

    @property
    def adfs_failure_key(self) -> str:
        return f"broker:adfs_failed:{self._identity}"

    @staticmethod
    def cred_key(account_id: str, role: str) -> str:
        return f"broker:cred:{account_id}:{role}"

    @staticmethod
    def cred_lock_key(account_id: str, role: str) -> str:
        return f"broker:lock:cred:{account_id}:{role}"

    # SAML --------------------------------------------------------------------------
    def get_saml(self) -> SamlAssertion | None:
        payload = self._read(self.saml_key, "saml")
        if payload is None:
            return None
        try:
            return SamlAssertion.parse(payload["saml_b64"])
        except (BrokerError, KeyError, TypeError):
            self._discard(self.saml_key, "unparseable cached SAML assertion")
            return None

    def set_saml(self, assertion: SamlAssertion) -> None:
        ttl = int((assertion.expires_at - self._clock()).total_seconds()) - self._margin
        if ttl <= 0:
            return
        envelope = self._cipher.encrypt(
            json.dumps({"v": PAYLOAD_VERSION, "saml_b64": assertion.raw_b64}).encode(), "saml"
        )
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(self.saml_key, envelope, ex=ttl)
        pipe.delete(self.roles_key)
        for account, pairs in assertion.roles.items():
            pipe.hset(
                self.roles_key, account, json.dumps([[p.role_arn, p.principal_arn] for p in pairs])
            )
        if assertion.roles:
            pipe.expire(self.roles_key, ttl)
        self._call(pipe.execute)

    def delete_saml(self) -> None:
        self._call(self._redis.delete, self.saml_key, self.roles_key)

    def get_roles(self) -> dict[str, list[RolePair]] | None:
        """Role map from the last login (plain text, no decryption needed)."""
        raw = self._call(self._redis.hgetall, self.roles_key)
        if not raw:
            return None
        try:
            return {
                _text(account): [RolePair(r, p) for r, p in json.loads(_text(value))]
                for account, value in raw.items()
            }
        except (ValueError, TypeError):
            self._discard(self.roles_key, "unparseable cached role map")
            return None

    def roles_ttl_seconds(self) -> int:
        ttl = self._call(self._redis.ttl, self.roles_key)
        return max(int(ttl), 0)

    # ADFS failures -------------------------------------------------------------------
    def get_adfs_failure(self) -> str | None:
        value = self._call(self._redis.get, self.adfs_failure_key)
        return _text(value) if value is not None else None

    def set_adfs_failure(self, message: str, ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            self._call(self._redis.set, self.adfs_failure_key, message, ex=ttl_seconds)

    # credentials ---------------------------------------------------------------------
    def get_cred(self, account_id: str, role: str) -> CachedCredential | None:
        key = self.cred_key(account_id, role)
        payload = self._read(key, "cred")
        if payload is None:
            return None
        try:
            return CachedCredential(
                role_arn=payload["role_arn"],
                credentials=AwsCredentials.model_validate(payload["credentials"]),
                issued_at=datetime.fromisoformat(payload["issued_at"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            self._discard(key, "unparseable cached credentials")
            return None

    def set_cred(self, account_id: str, role: str, cached: CachedCredential) -> None:
        ttl = int(cached.credentials.remaining_seconds(self._clock()))
        if ttl <= 0:
            return
        payload = {
            "v": PAYLOAD_VERSION,
            "role_arn": cached.role_arn,
            "credentials": cached.credentials.model_dump(mode="json"),
            "issued_at": cached.issued_at.isoformat(),
        }
        envelope = self._cipher.encrypt(json.dumps(payload).encode(), "cred")
        self._call(self._redis.set, self.cred_key(account_id, role), envelope, ex=ttl)

    # locks ---------------------------------------------------------------------------
    def acquire(self, key: str) -> str | None:
        token = secrets.token_hex(16)
        acquired = self._call(self._redis.set, key, token, nx=True, px=self._lock_ttl_ms)
        return token if acquired else None

    def release(self, key: str, token: str) -> None:
        try:
            self._release(keys=[key], args=[token])
        except RedisError:
            log.warning("failed to release lock; it will expire", extra={"lock_key": key})

    # helpers -------------------------------------------------------------------------
    def _read(self, key: str, kind: str) -> dict[str, Any] | None:
        """Decrypt a cache entry. Corrupt or undecryptable entries are dropped (cache miss)."""
        envelope = self._call(self._redis.get, key)
        if envelope is None:
            return None
        try:
            payload = json.loads(self._cipher.decrypt(_text(envelope), kind))
        except (BrokerError, ValueError):
            self._discard(key, "undecryptable cache entry")
            return None
        if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
            self._discard(key, "cache entry has an unknown format version")
            return None
        return payload

    def _discard(self, key: str, reason: str) -> None:
        log.warning("dropping cache entry", extra={"cache_key": key, "reason": reason})
        try:
            self._redis.delete(key)
        except RedisError:
            log.warning("failed to delete cache entry", extra={"cache_key": key})
        return None

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except RedisError as exc:
            raise BrokerError("INTERNAL", f"redis error: {type(exc).__name__}") from exc


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
