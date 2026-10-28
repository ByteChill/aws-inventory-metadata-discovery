"""API keys for service callers, and who requested each scan job (spec 10).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE api_key (
          key_id       text        PRIMARY KEY,
          name         text        NOT NULL,
          key_hash     bytea       NOT NULL,
          roles        text[]      NOT NULL,
          created_by   text        NOT NULL,
          created_at   timestamptz NOT NULL,
          expires_at   timestamptz,
          revoked_at   timestamptz,
          last_used_at timestamptz
        )
        """
    )
    op.execute("ALTER TABLE scan_job ADD COLUMN requested_by text")


def downgrade() -> None:
    op.execute("ALTER TABLE scan_job DROP COLUMN requested_by")
    op.execute("DROP TABLE api_key")
