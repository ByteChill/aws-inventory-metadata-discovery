"""Alembic environment. Migrations are hand-written SQL; there is no autogenerate."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from inventory.aws import ClientFactory, EndpointResolver
from inventory.bootstrap import database_url as service_database_url
from inventory.config import get_common_settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    configured = config.attributes.get("url")
    if configured:
        return str(configured)
    # same resolution as the services: local database_url, or the RDS secret + proxy host
    common = get_common_settings()
    return service_database_url(common, ClientFactory(EndpointResolver(common)))


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
