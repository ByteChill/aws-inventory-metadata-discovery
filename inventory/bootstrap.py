"""Wire the shared runtime (settings, clients, database, broker, engine) for an entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aws_lambda_powertools.utilities.parameters import SecretsProvider
from sqlalchemy import Engine

from inventory.aws import (
    BrokerLambdaProvider,
    ClientFactory,
    EndpointResolver,
    LambdaBrokerTransport,
)
from inventory.collectors import registry
from inventory.collectors.base import Registry
from inventory.config import CommonSettings, get_common_settings
from inventory.db import Credentials, make_engine, resolve_database_url
from inventory.engine import ScanEngine
from inventory.scheduling import Scheduler
from inventory.transport import PostgresResultSink, SqsDirectDispatcher


@dataclass
class Runtime:
    common: CommonSettings
    resolver: EndpointResolver
    clients: ClientFactory
    registry: Registry
    db: Engine
    credentials: BrokerLambdaProvider
    dispatcher: SqsDirectDispatcher
    sink: PostgresResultSink
    engine: ScanEngine
    scheduler: Scheduler
    db_credentials: Credentials | None = None


SECRET_MAX_AGE_SECONDS = 60  # bounds how long a rotated database password is unknown


def database_access(
    common: CommonSettings, clients: ClientFactory
) -> tuple[str, Credentials | None]:
    """Database URL plus, on AWS, a callable returning the current secret credentials."""
    if common.database_url is not None or not common.database_secret_arn:
        return resolve_database_url(common), None
    provider = SecretsProvider(boto3_client=clients.client("secretsmanager", common.primary_region))
    arn = common.database_secret_arn

    def secret() -> dict[str, Any]:
        value: dict[str, Any] = provider.get(arn, transform="json", max_age=SECRET_MAX_AGE_SECONDS)  # type: ignore[assignment]
        return value

    def credentials() -> tuple[str, str]:
        current = secret()
        return str(current["username"]), str(current["password"])

    return resolve_database_url(common, secret()), credentials


def database_url(common: CommonSettings, clients: ClientFactory) -> str:
    return database_access(common, clients)[0]


def build_runtime(
    service_name: str,
    *,
    statement_timeout_ms: int,
    pool_size: int = 5,
    max_overflow: int = 5,
    type_concurrency: int = 4,
) -> Runtime:
    common = get_common_settings()
    resolver = EndpointResolver(common)
    clients = ClientFactory(resolver)
    url, db_credentials = database_access(common, clients)
    db = make_engine(
        url,
        application_name=service_name,
        statement_timeout_ms=statement_timeout_ms,
        pool_size=pool_size,
        max_overflow=max_overflow,
        credentials=db_credentials,
    )
    credentials = BrokerLambdaProvider(
        LambdaBrokerTransport(clients, common.broker_function_name, common.primary_region)
    )
    dispatcher = SqsDirectDispatcher(common, clients)
    sink = PostgresResultSink(db)
    engine = ScanEngine(
        registry, clients, resolver, credentials, sink, type_concurrency=type_concurrency
    )
    scheduler = Scheduler(
        common,
        db,
        credentials,
        clients,
        registry,
        dispatcher,
        account_region_max_age=timedelta(hours=common.account_region_max_age_hours),
    )
    return Runtime(
        common,
        resolver,
        clients,
        registry,
        db,
        credentials,
        dispatcher,
        sink,
        engine,
        scheduler,
        db_credentials,
    )
