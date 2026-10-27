from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import create_async_engine

from inventory.api.app import create_app
from inventory.config import ApiSettings
from inventory.engine import reconcile
from inventory.models import TypeResult, TypeScope, TypeStatus
from inventory.transport import PostgresResultSink
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
    return ApiSettings(wait_poll_interval_ms=20, refresh_inline_max=20)


def new_async_engine() -> Any:
    return create_async_engine(TEST_DB_URL)


@pytest.fixture
def app(db: Engine, moto_env: None) -> Iterator[FastAPI]:
    yield create_app(app_settings(), runtime=make_runtime(db), async_engine=new_async_engine())


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
