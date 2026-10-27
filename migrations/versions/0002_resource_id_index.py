"""Index for resolving ARNs without an account (AMI, S3 bucket, hosted zone).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_resource_type_id ON resource (resource_type, resource_id)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_resource_type_id")
