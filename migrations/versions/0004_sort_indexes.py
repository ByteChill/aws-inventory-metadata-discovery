"""Indexes matching the API keyset sort keys exactly (both sort orders).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX ix_resource_changed")
    op.execute(
        "CREATE INDEX ix_resource_changed "
        "ON resource (last_changed_at, resource_type, account_id, region, resource_id)"
    )
    op.execute(
        "CREATE INDEX ix_resource_id_sort "
        "ON resource (resource_id, resource_type, account_id, region)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_resource_id_sort")
    op.execute("DROP INDEX ix_resource_changed")
    op.execute(
        "CREATE INDEX ix_resource_changed "
        "ON resource (last_changed_at, account_id, region, resource_id)"
    )
