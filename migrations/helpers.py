"""Helpers shared by migration scripts."""

from __future__ import annotations

from alembic import op

from inventory.db import partition_name


def create_resource_partition(resource_type: str) -> None:
    table = partition_name(resource_type)
    literal = resource_type.replace("'", "''")
    op.execute(f"CREATE TABLE {table} PARTITION OF resource FOR VALUES IN ('{literal}')")


def drop_resource_partition(resource_type: str) -> None:
    op.execute(f"DROP TABLE IF EXISTS {partition_name(resource_type)}")
