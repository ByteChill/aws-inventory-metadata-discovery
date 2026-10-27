from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import create_async_engine

from inventory.api.app import create_app
from inventory.bootstrap import Runtime
from inventory.config import ApiSettings, WorkerSettings
from inventory.engine import TaskExecutor, reconcile
from inventory.handlers.worker import Worker
from inventory.models import TypeResult, TypeScope, TypeStatus
from inventory.transport import PostgresResultSink, QueueLocator
from tests.collectors.conftest import seed_ec2
from tests.conftest import FROZEN_NOW, TEST_DB_URL
from tests.db.test_repository import rec
from tests.planner.conftest import make_runtime, moto_env

__all__ = ["moto_env"]


def seed_resources(db: Engine) -> None:
    """VPCs in eu-west-1 (with scope_state) and one AMI in us-east-1."""
    sink = PostgresResultSink(db, clock=lambda: FROZEN_NOW)
    for scope, names in (
        (TypeScope("AWS::EC2::VPC", "123456789012", "eu-west-1"), ["vpc_a_v1", "vpc_b"]),
        (TypeScope("AWS::EC2::Image", "210987654321", "us-east-1"), ["ami_x"]),
    ):
        records = [rec(n, FROZEN_NOW - timedelta(hours=1)) for n in names]
        changes = reconcile(scope, {}, records, set(), FROZEN_NOW - timedelta(hours=1), "seed")
        result = TypeResult(TypeStatus.SUCCEEDED, created=len(records))
        sink.apply(scope, changes, result, "seed", len(records))


def app_settings() -> ApiSettings:
    return ApiSettings(wait_poll_interval_ms=20)


def new_async_engine() -> Any:
    return create_async_engine(TEST_DB_URL)


@contextmanager
def running_worker(runtime: Runtime, region: str = "ap-east-1") -> Iterator[None]:
    """A worker thread consuming the region's queues, as the ECS worker does (refreshes are
    always queued)."""
    executor = TaskExecutor(
        runtime.engine, runtime.db, runtime.dispatcher, worker_id="test-worker", region=region
    )
    queues = QueueLocator(runtime.common, runtime.clients)
    worker = Worker(
        WorkerSettings(region=region, ondemand_wait_seconds=0, scheduled_wait_seconds=0),
        runtime.clients.client("sqs", region),
        executor.handle,
        ondemand_url=queues.url(region, "ondemand"),
        scheduled_url=queues.url(region, "scheduled"),
    )
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            if not worker.poll_once():
                stop.wait(0.02)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


@pytest.fixture
def runtime(db: Engine, moto_env: None) -> Runtime:
    return make_runtime(db)


@pytest.fixture
def app(runtime: Runtime) -> Iterator[FastAPI]:
    yield create_app(app_settings(), runtime=runtime, async_engine=new_async_engine())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def seeded(db: Engine) -> None:
    seed_resources(db)


@pytest.fixture
def ec2_ids() -> dict[str, str]:
    return seed_ec2(boto3.client("ec2", region_name="ap-east-1"))


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    error = dict(body["error"])
    error.pop("request_id", None)
    return {"error": error}
