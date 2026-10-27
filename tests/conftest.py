from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import time_machine
from alembic import command
from alembic.config import Config
from moto import mock_aws
from sqlalchemy import Engine, create_engine, make_url, text

from inventory.config import clear_settings_cache
from inventory.db import make_engine
from tests.helpers import load_fixture

FROZEN_NOW = datetime(2026, 10, 27, 8, 0, 0, tzinfo=UTC)

# Isolate tests from any real AWS profile or local .env file.
for _var in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
    os.environ.pop(_var, None)
os.environ.update(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-east-1",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "POWERTOOLS_METRICS_DISABLED": "true",
        "POWERTOOLS_TRACE_DISABLED": "true",
        "POWERTOOLS_DEV": "false",
    }
)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    # pydantic-settings reads .env from the CWD; run each test from an empty directory view
    # by pointing the settings at a non-existent env file.
    monkeypatch.chdir(tmp_path_factory.getbasetemp())
    clear_settings_cache()


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env: dict[str, str] = load_fixture("foundation/settings.local.json")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    return env


@pytest.fixture
def frozen_time() -> Iterator[time_machine.Coordinates]:
    with time_machine.travel(FROZEN_NOW, tick=False) as traveller:
        yield traveller


# --------------------------------------------------------------------------- PostgreSQL

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_URL = os.environ.get(
    "INVENTORY_TEST_DATABASE_URL",
    "postgresql+psycopg://inventory:inventory@localhost:5432/inventory_test",
)
TABLES = ("scan_task", "scan_job", "scope_state", "account_region", "resource", "api_key")


def run_migrations(url: str, revision: str = "head", *, downgrade: bool = False) -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    if downgrade:
        command.downgrade(cfg, revision)
    else:
        command.upgrade(cfg, revision)


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    url = make_url(TEST_DB_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    run_migrations(TEST_DB_URL)
    engine = make_engine(
        TEST_DB_URL, application_name="inventory-tests", statement_timeout_ms=30_000
    )
    yield engine
    engine.dispose()


@pytest.fixture
def db(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    return pg_engine
