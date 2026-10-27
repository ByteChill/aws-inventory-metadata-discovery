"""Handler end-to-end: real Redis (compose), moto STS/KMS/Secrets Manager, mocked ADFS."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace

import boto3
import pytest
import respx
from moto import mock_aws
from redis import Redis

from inventory.config import BrokerSettings, clear_settings_cache
from inventory.handlers import broker_lambda
from tests.broker.conftest import saml_b64
from tests.helpers import load_fixture

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("frozen_time")]


@pytest.fixture
def redis_db(broker_settings: BrokerSettings) -> Iterator[Redis]:
    client = Redis.from_url(broker_settings.redis_url)
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def wired_handler(
    settings_env: dict[str, str],
    broker_settings: BrokerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    with mock_aws():
        secrets = boto3.client("secretsmanager", region_name="ap-east-1")
        secrets.create_secret(
            Name=broker_settings.adfs_credentials_secret_arn,
            SecretString=json.dumps(load_fixture("broker/adfs_secret.json")),
        )
        key_id = boto3.client("kms", region_name="ap-east-1").create_key()["KeyMetadata"]["KeyId"]
        monkeypatch.setenv("BROKER_KMS_KEY_ID", key_id)
        clear_settings_cache()
        broker_lambda.get_service.cache_clear()
        yield
        broker_lambda.get_service.cache_clear()


@pytest.mark.usefixtures("wired_handler")
def test_handler_get_credentials_end_to_end(
    broker_settings: BrokerSettings, redis_db: Redis
) -> None:
    saml_page = load_fixture("broker/adfs/saml_post.html").replace(
        "__SAML_RESPONSE__", saml_b64("broker/saml/assertion_single_role.xml")
    )
    context = SimpleNamespace(**load_fixture("broker/lambda_context.json"))
    event = load_fixture("broker/get_credentials.request.json")

    with respx.mock(assert_all_called=True) as router:
        router.get(broker_settings.adfs_idp_url).respond(
            200, html=load_fixture("broker/adfs/login_page.html")
        )
        router.post(url__startswith="https://adfs.example.invalid/").respond(200, html=saml_page)

        first = broker_lambda.handler(event, context)
        second = broker_lambda.handler(event, context)

    assert first["status"] == "ok" and first["source"] == "sts"
    assert second["source"] == "cache"
    assert second["credentials"]["access_key_id"] == first["credentials"]["access_key_id"]
    assert redis_db.ttl("broker:cred:123456789012:ReadOnly") > 0
    stored = redis_db.get("broker:cred:123456789012:ReadOnly")
    assert stored is not None
    assert first["credentials"]["secret_access_key"].encode() not in stored
