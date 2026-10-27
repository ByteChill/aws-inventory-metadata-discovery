from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import boto3
import fakeredis
import pytest
from moto import mock_aws

from inventory.broker.cache import RedisCache
from inventory.broker.crypto import EnvelopeCipher
from inventory.broker.saml import RolePair, SamlAssertion
from inventory.broker.service import BrokerService
from inventory.config import BrokerSettings
from inventory.models import AwsCredentials
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture


def saml_b64(rel_path: str) -> str:
    return base64.b64encode(load_fixture(rel_path).encode()).decode()


class FakeAdfs:
    """Test double for AdfsClient; returns a fixture assertion and counts logins."""

    def __init__(self, rel_path: str = "broker/saml/assertion_multi_role.xml") -> None:
        self.saml = saml_b64(rel_path)
        self.logins = 0
        self.error: Exception | None = None
        self._lock = threading.Lock()

    def login(self) -> str:
        with self._lock:
            self.logins += 1
        if self.error:
            raise self.error
        return self.saml


class FakeSts:
    """Test double for StsAssumer; returns fixture credentials and counts calls."""

    def __init__(self, expiration: datetime | None = None) -> None:
        self.expiration = expiration
        self.calls = 0
        self.errors: list[Exception] = []
        self._lock = threading.Lock()

    def assume(self, assertion: SamlAssertion, pair: RolePair) -> AwsCredentials:
        with self._lock:
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
        creds = AwsCredentials.model_validate(load_fixture("broker/sts_credentials.json"))
        return (
            creds.model_copy(update={"expiration": self.expiration}) if self.expiration else creds
        )


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def broker_settings(monkeypatch: pytest.MonkeyPatch) -> BrokerSettings:
    for key, value in load_fixture("broker/broker_settings.json").items():
        monkeypatch.setenv(key, value)
    return BrokerSettings()  # type: ignore[call-arg]


@pytest.fixture
def kms_key() -> Iterator[tuple[Any, str]]:
    with mock_aws():
        kms = boto3.client("kms", region_name="ap-east-1")
        key_id = kms.create_key()["KeyMetadata"]["KeyId"]
        yield kms, key_id


@pytest.fixture
def cipher(kms_key: tuple[Any, str]) -> EnvelopeCipher:
    kms, key_id = kms_key
    return EnvelopeCipher(kms, key_id)


@pytest.fixture
def fake_redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(server=fakeredis.FakeServer())


def clock() -> datetime:
    return FROZEN_NOW


@pytest.fixture
def cache(fake_redis: fakeredis.FakeRedis, cipher: EnvelopeCipher) -> RedisCache:
    return RedisCache(fake_redis, cipher, "default", clock=clock)


@pytest.fixture
def adfs() -> FakeAdfs:
    return FakeAdfs()


@pytest.fixture
def sts() -> FakeSts:
    return FakeSts()


@pytest.fixture
def service(
    broker_settings: BrokerSettings, cache: RedisCache, adfs: FakeAdfs, sts: FakeSts
) -> BrokerService:
    return BrokerService(broker_settings, cache, adfs, sts, clock=clock)  # type: ignore[arg-type]
