from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from inventory.broker import BrokerError, SamlExpiredError
from inventory.broker.cache import RedisCache
from inventory.broker.service import BrokerService
from inventory.config import BrokerSettings
from inventory.models import (
    GetCredentialsRequest,
    GetSamlRequest,
    ListAccessibleAccountsRequest,
)
from tests.broker.conftest import FakeAdfs, FakeMonotonic, FakeSts, clock, saml_b64
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture


class MutableClock:
    def __init__(self) -> None:
        self.value: datetime = FROZEN_NOW

    def __call__(self) -> datetime:
        return self.value


def request(**overrides: object) -> GetCredentialsRequest:
    payload = {**load_fixture("broker/get_credentials.request.json"), **overrides}
    return GetCredentialsRequest.model_validate(payload)


def test_first_call_assumes_then_serves_from_cache(
    service: BrokerService, adfs: FakeAdfs, sts: FakeSts
) -> None:
    first = service.get_credentials(request())
    second = service.get_credentials(request())

    assert (first.source, second.source) == ("sts", "cache")
    assert first.role_arn == "arn:aws:iam::123456789012:role/ReadOnly"
    assert second.credentials.access_key_id == first.credentials.access_key_id
    assert (adfs.logins, sts.calls) == (1, 1)


def test_account_without_role_is_denied_without_sts(service: BrokerService, sts: FakeSts) -> None:
    with pytest.raises(BrokerError) as info:
        service.get_credentials(request(account_id="111122223333"))

    assert info.value.code == "ACCESS_DENIED"
    assert not info.value.retryable
    assert sts.calls == 0


def test_credentials_close_to_expiry_are_refetched(
    broker_settings: BrokerSettings, cache: RedisCache
) -> None:
    now = MutableClock()
    sts = FakeSts()
    service = BrokerService(broker_settings, cache, FakeAdfs(), sts, clock=now)

    service.get_credentials(request())  # 08:00, expires 09:00
    now.value = FROZEN_NOW + timedelta(minutes=45)  # 900s left < min_ttl 1200
    second = service.get_credentials(request())

    assert second.source == "sts"
    assert sts.calls == 2


def test_short_sessions_are_not_refetched_on_every_call(
    broker_settings: BrokerSettings, cache: RedisCache
) -> None:
    sts = FakeSts(expiration=FROZEN_NOW + timedelta(seconds=1000))  # shorter than min_ttl
    service = BrokerService(broker_settings, cache, FakeAdfs(), sts, clock=clock)

    first = service.get_credentials(request())
    second = service.get_credentials(request())

    assert (first.source, second.source) == ("sts", "cache")
    assert sts.calls == 1


def test_expired_assertion_triggers_one_relogin(
    service: BrokerService, adfs: FakeAdfs, sts: FakeSts
) -> None:
    sts.errors = [SamlExpiredError("ExpiredTokenException")]

    response = service.get_credentials(request())

    assert response.source == "sts"
    assert (adfs.logins, sts.calls) == (2, 2)


def test_assertion_rejected_twice_is_internal(service: BrokerService, sts: FakeSts) -> None:
    sts.errors = [SamlExpiredError("x"), SamlExpiredError("x")]

    with pytest.raises(BrokerError) as info:
        service.get_credentials(request())
    assert info.value.code == "INTERNAL"


def test_adfs_auth_failure_propagates(service: BrokerService, adfs: FakeAdfs) -> None:
    adfs.error = BrokerError("ADFS_AUTH_FAILED", "Incorrect user ID or password.")

    with pytest.raises(BrokerError) as info:
        service.get_credentials(request())
    assert info.value.code == "ADFS_AUTH_FAILED"


def test_concurrent_requests_assume_once(
    broker_settings: BrokerSettings, cache: RedisCache
) -> None:
    adfs, sts = FakeAdfs(), FakeSts()
    service = BrokerService(broker_settings, cache, adfs, sts, clock=clock)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda _: service.get_credentials(request()), range(100)))

    assert {r.credentials.access_key_id for r in results} == {"ASIAEXAMPLEEXAMPLE01"}
    assert (adfs.logins, sts.calls) == (1, 1)


def test_lock_wait_timeout_falls_through(
    broker_settings: BrokerSettings, cache: RedisCache
) -> None:
    fake_time = FakeMonotonic()
    adfs, sts = FakeAdfs(), FakeSts()
    service = BrokerService(
        broker_settings,
        cache,
        adfs,
        sts,  # type: ignore[arg-type]
        clock=clock,
        sleep=fake_time.sleep,
        monotonic=fake_time,
    )
    assert cache.acquire(cache.cred_lock_key("123456789012", "ReadOnly"))  # held by "someone"

    response = service.get_credentials(request())

    assert response.source == "sts"
    assert fake_time.now >= broker_settings.lock_wait_timeout_ms / 1000


def test_list_accessible_accounts_and_get_saml(service: BrokerService, adfs: FakeAdfs) -> None:
    expected = load_fixture("broker/list_accessible_accounts.response.json")

    accounts = service.list_accessible_accounts(ListAccessibleAccountsRequest())
    saml = service.get_saml(GetSamlRequest())

    assert accounts.model_dump(mode="json") == expected
    assert saml.saml_response.get_secret_value() == saml_b64("broker/saml/assertion_multi_role.xml")
    assert adfs.logins == 1


def test_cache_entries_are_encrypted(service: BrokerService, cache: RedisCache) -> None:
    service.get_credentials(request())

    raw = cache._redis.get(cache.cred_key("123456789012", "ReadOnly"))
    assert raw is not None
    assert b"fake-secret-access-key" not in raw
    assert cache.get_roles() is not None


def test_no_secrets_in_logs(service: BrokerService, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    service.get_credentials(request())

    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert "fake-secret-access-key" not in text
    assert "fake-session-token" not in text


def test_waiter_takes_over_when_lock_holder_gives_up(
    broker_settings: BrokerSettings, cache: RedisCache, sts: FakeSts
) -> None:
    service = BrokerService(broker_settings, cache, FakeAdfs(), sts, clock=clock)
    lock_key = cache.cred_lock_key("123456789012", "ReadOnly")
    token = cache.acquire(lock_key)
    assert token is not None
    releaser = threading.Timer(0.05, cache.release, args=(lock_key, token))
    releaser.start()

    started = time.monotonic()
    response = service.get_credentials(request())

    assert response.source == "sts"
    assert time.monotonic() - started < 1  # far below lock_wait_timeout_ms (3s)
    assert sts.calls == 1


def test_wrong_password_is_negatively_cached(service: BrokerService, adfs: FakeAdfs) -> None:
    adfs.error = BrokerError("ADFS_AUTH_FAILED", "Incorrect user ID or password.")

    for _ in range(3):
        with pytest.raises(BrokerError) as info:
            service.get_credentials(request())
        assert info.value.code == "ADFS_AUTH_FAILED"

    assert adfs.logins == 1  # no repeated logins that could lock the AD account


@pytest.mark.parametrize(
    "garbage", [b"not-an-envelope", b'{"v": 1, "ek": "AA==", "iv": "AA==", "ct": "AA=="}']
)
def test_corrupt_cache_entry_is_treated_as_a_miss(
    service: BrokerService, cache: RedisCache, sts: FakeSts, garbage: bytes
) -> None:
    key = cache.cred_key("123456789012", "ReadOnly")
    cache._redis.set(key, garbage)

    response = service.get_credentials(request())

    assert response.source == "sts"
    assert sts.calls == 1
    assert cache.get_cred("123456789012", "ReadOnly") is not None


def test_list_accessible_accounts_reads_the_role_map(
    service: BrokerService, cache: RedisCache, adfs: FakeAdfs
) -> None:
    service.get_saml(GetSamlRequest())
    cache.delete_saml.__self__._redis.delete(cache.saml_key)

    accounts = service.list_accessible_accounts(ListAccessibleAccountsRequest())

    assert accounts.account_ids == ["123456789012", "210987654321"]
    assert adfs.logins == 1
