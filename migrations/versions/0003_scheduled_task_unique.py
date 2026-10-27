"""One scheduled task per (job, account, region): makes planner batch retries idempotent.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ux_scan_task_scheduled ON scan_task (job_id, account_id, region) "
        "WHERE queue = 'scheduled'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ux_scan_task_scheduled")
