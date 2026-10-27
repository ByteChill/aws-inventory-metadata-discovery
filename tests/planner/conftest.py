from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import Engine

from inventory.aws import ClientFactory, EndpointResolver
from inventory.bootstrap import Runtime
from inventory.collectors import registry
from inventory.config import clear_settings_cache, get_common_settings
from inventory.engine import ScanEngine
from inventory.scheduling import Scheduler
from inventory.transport import PostgresResultSink, SqsDirectDispatcher
from tests.engine.fakes import FakeCredentials
from tests.helpers import load_fixture

REGIONS = ("ap-east-1", "eu-west-1", "eu-west-2", "us-east-1")
QUEUES = ("inventory-test-scheduled", "inventory-test-ondemand")


class BrokerCredentials(FakeCredentials):
    """Fake broker: every account may be assumed; the accessible set comes from a fixture."""

    def accessible_accounts(self) -> set[str]:
        return set(load_fixture("planner/accessible_accounts.json"))


@pytest.fixture
def moto_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Settings whose endpoints are all default DNS, plus scheduled/ondemand queues per region."""
    for key, value in load_fixture("foundation/settings.moto.json").items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    with mock_aws():
        for region in REGIONS:
            sqs = boto3.client("sqs", region_name=region)
            for name in QUEUES:
                sqs.create_queue(QueueName=name)
        yield


def make_runtime(db: Engine, credentials: Any = None) -> Runtime:
    common = get_common_settings()
    resolver = EndpointResolver(common)
    clients = ClientFactory(resolver)
    creds = credentials or BrokerCredentials()
    dispatcher = SqsDirectDispatcher(common, clients)
    sink = PostgresResultSink(db)
    engine = ScanEngine(registry, clients, resolver, creds, sink)
    scheduler = Scheduler(common, db, creds, clients, registry, resolver, dispatcher)
    return Runtime(
        common, resolver, clients, registry, db, creds, dispatcher, sink, engine, scheduler
    )


def drain(region: str, queue: str) -> list[dict[str, Any]]:
    sqs = boto3.client("sqs", region_name=region)
    url = sqs.get_queue_url(QueueName=queue)["QueueUrl"]
    messages: list[dict[str, Any]] = []
    while True:
        batch = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=10, MessageAttributeNames=["All"]
        ).get("Messages", [])
        if not batch:
            return messages
        messages.extend(batch)
        sqs.delete_message_batch(
            QueueUrl=url,
            Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(batch)
            ],
        )
