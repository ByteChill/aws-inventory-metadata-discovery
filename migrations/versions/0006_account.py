"""Accounts as account-directory lists them: name, EIM and account type (spec 05 §6).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE account (
          account_id   char(12)    PRIMARY KEY,
          name         text,
          eim          text,
          account_type text,
          status       text        NOT NULL,
          listed_at    timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_account_eim ON account (eim)")


def downgrade() -> None:
    op.execute("DROP TABLE account")
