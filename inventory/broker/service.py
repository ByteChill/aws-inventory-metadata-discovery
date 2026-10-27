"""Broker business logic: cached credentials with single-flight refresh."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import SecretStr

from inventory.broker import BrokerError, SamlExpiredError
from inventory.broker.cache import CachedCredential, RedisCache
from inventory.broker.saml import RolePair, SamlAssertion
from inventory.config import BrokerSettings
from inventory.models import (
    AwsCredentials,
    GetCredentialsRequest,
    GetCredentialsResponse,
    GetSamlRequest,
    GetSamlResponse,
    ListAccessibleAccountsRequest,
    ListAccessibleAccountsResponse,
)
from inventory.observability import metric

log = logging.getLogger(__name__)


class SamlSource(Protocol):
    """Logs in and returns a base64 SAMLResponse (AdfsClient)."""

    def login(self) -> str: ...


class CredentialAssumer(Protocol):
    """Turns an assertion and a role into credentials (StsAssumer)."""

    def assume(self, assertion: SamlAssertion, pair: RolePair) -> AwsCredentials: ...


class BrokerService:
    def __init__(
        self,
        settings: BrokerSettings,
        cache: RedisCache,
        adfs: SamlSource,
        sts: CredentialAssumer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._adfs = adfs
        self._sts = sts
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic

    def handle(
        self, request: GetCredentialsRequest | ListAccessibleAccountsRequest | GetSamlRequest
    ) -> GetCredentialsResponse | ListAccessibleAccountsResponse | GetSamlResponse:
        if isinstance(request, GetCredentialsRequest):
            return self.get_credentials(request)
        if isinstance(request, ListAccessibleAccountsRequest):
            return self.list_accessible_accounts(request)
        return self.get_saml(request)

    # ------------------------------------------------------------------ credentials
    def get_credentials(self, request: GetCredentialsRequest) -> GetCredentialsResponse:
        account, role = request.account_id, request.role or self._settings.role_name
        fresh = self._fresh_cred_reader(account, role, request.min_ttl_seconds)

        cached = fresh()
        if cached is not None:
            metric("CacheHit")
            return self._credentials_response(account, cached, "cache")
        metric("CacheMiss")

        lock_key = self._cache.cred_lock_key(account, role)
        token, waited = self._acquire_or_wait(lock_key, fresh)
        if waited is not None:
            return self._credentials_response(account, waited, "cache")
        try:
            # the previous holder may have filled the cache just before releasing the lock
            cached = fresh()
            if cached is not None:
                return self._credentials_response(account, cached, "cache")
            obtained = self._assume(account, role)
            self._cache.set_cred(account, role, obtained)
            return self._credentials_response(account, obtained, "sts")
        finally:
            if token is not None:
                self._cache.release(lock_key, token)

    def _assume(self, account: str, role: str) -> CachedCredential:
        for attempt in range(2):
            assertion = self._assertion(force_refresh=attempt > 0)
            pair = assertion.find_role(account, role)
            if pair is None:
                metric("AccessDenied")
                raise BrokerError("ACCESS_DENIED", f"no {role} role for account {account}")
            issued_at = self._clock()
            try:
                credentials = self._sts.assume(assertion, pair)
            except SamlExpiredError:
                log.info("STS rejected cached SAML assertion; logging in again")
                self._cache.delete_saml()
                continue
            except BrokerError as exc:
                if exc.code == "ACCESS_DENIED":
                    metric("AccessDenied")
                raise
            metric("StsAssume")
            return CachedCredential(
                role_arn=pair.role_arn,
                credentials=credentials,
                issued_at=issued_at,
            )
        raise BrokerError("INTERNAL", "STS rejected a freshly obtained SAML assertion")

    def _fresh_cred_reader(
        self, account: str, role: str, min_ttl: int
    ) -> Callable[[], CachedCredential | None]:
        def read() -> CachedCredential | None:
            cached = self._cache.get_cred(account, role)
            if cached is None:
                return None
            # Never demand more than half the session lifetime, otherwise short sessions
            # (SessionDuration < min_ttl) would be re-assumed on every request.
            needed = min(min_ttl, cached.lifetime_seconds() / 2)
            if cached.credentials.remaining_seconds(self._clock()) >= needed:
                return cached
            return None

        return read

    @staticmethod
    def _credentials_response(
        account: str, cached: CachedCredential, source: str
    ) -> GetCredentialsResponse:
        return GetCredentialsResponse(
            account_id=account,
            role_arn=cached.role_arn,
            credentials=cached.credentials,
            source="cache" if source == "cache" else "sts",
        )

    # ------------------------------------------------------------------ SAML
    def list_accessible_accounts(
        self, request: ListAccessibleAccountsRequest
    ) -> ListAccessibleAccountsResponse:
        role = request.role or self._settings.role_name
        roles = self._cache.get_roles()
        if roles is not None:
            ttl = self._cache.roles_ttl_seconds()
            margin = self._settings.saml_refresh_margin_seconds
            return ListAccessibleAccountsResponse(
                role=role,
                account_ids=sorted(
                    a for a, pairs in roles.items() if any(p.role_name == role for p in pairs)
                ),
                expires_at=self._clock() + timedelta(seconds=ttl + margin),
            )
        assertion = self._assertion()
        return ListAccessibleAccountsResponse(
            role=role,
            account_ids=assertion.accounts_with_role(role),
            expires_at=assertion.expires_at,
        )

    def get_saml(self, request: GetSamlRequest) -> GetSamlResponse:
        assertion = self._assertion()
        return GetSamlResponse(
            saml_response=SecretStr(assertion.raw_b64), expires_at=assertion.expires_at
        )

    def _assertion(self, force_refresh: bool = False) -> SamlAssertion:
        reader = self._fresh_saml_reader()
        if not force_refresh:
            cached = reader()
            if cached is not None:
                return cached

        token, waited = self._acquire_or_wait(
            self._cache.saml_lock_key, (lambda: None) if force_refresh else reader
        )
        if waited is not None:
            return waited
        try:
            if not force_refresh:
                cached = reader()
                if cached is not None:
                    return cached
            return self._login()
        finally:
            if token is not None:
                self._cache.release(self._cache.saml_lock_key, token)

    def _login(self) -> SamlAssertion:
        failure = self._cache.get_adfs_failure()
        if failure is not None:
            # A recent wrong-password result: do not hammer ADFS and lock the AD account.
            raise BrokerError("ADFS_AUTH_FAILED", failure)
        try:
            saml_b64 = self._adfs.login()
        except BrokerError as exc:
            if exc.code == "ADFS_AUTH_FAILED":
                metric("AdfsLoginFailed")
                self._cache.set_adfs_failure(exc.message, self._settings.adfs_failure_cache_seconds)
            raise
        metric("AdfsLogin")
        assertion = SamlAssertion.parse(saml_b64)
        self._cache.set_saml(assertion)
        log.info(
            "ADFS login succeeded",
            extra={"accounts": len(assertion.roles), "expires_at": assertion.expires_at},
        )
        return assertion

    def _fresh_saml_reader(self) -> Callable[[], SamlAssertion | None]:
        margin = timedelta(seconds=self._settings.saml_refresh_margin_seconds)

        def read() -> SamlAssertion | None:
            cached = self._cache.get_saml()
            if cached and cached.expires_at - margin > self._clock():
                return cached
            return None

        return read

    # ------------------------------------------------------------------ locking
    def _acquire_or_wait[T](
        self, lock_key: str, reader: Callable[[], T | None]
    ) -> tuple[str | None, T | None]:
        """Take the lock, or wait for its holder's result.

        Returns (token, None) when we hold the lock, (None, value) when another request
        produced the value, and (None, None) when waiting timed out (proceed unlocked).
        The lock is retried on every poll, so a holder that failed lets exactly one
        waiter take over instead of releasing everybody at the timeout.
        """
        started = self._monotonic()
        deadline = started + self._settings.lock_wait_timeout_ms / 1000
        interval = self._settings.lock_poll_interval_ms / 1000
        while True:
            token = self._cache.acquire(lock_key)
            if token is not None:
                return token, None
            if self._monotonic() >= deadline:
                log.warning(
                    "lock wait timed out; proceeding without lock", extra={"lock_key": lock_key}
                )
                return None, None
            self._sleep(interval)
            value = reader()
            if value is not None:
                metric("LockWaitMs", (self._monotonic() - started) * 1000, unit="Milliseconds")
                return None, value
