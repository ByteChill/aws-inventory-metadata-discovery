"""Initial schema: partitioned resource table, scope state, account regions, jobs and tasks.

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op

from migrations.helpers import create_resource_partition, drop_resource_partition

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

V1_RESOURCE_TYPES = (
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::SecurityGroup",
    "AWS::EC2::Instance",
    "AWS::EC2::Image",
    "AWS::ECS::Cluster",
    "AWS::ECS::Service",
    "AWS::ECS::TaskDefinition",
    "AWS::S3::Bucket",
    "AWS::IAM::Role",
    "AWS::IAM::User",
    "AWS::IAM::ManagedPolicy",
    "AWS::Route53::HostedZone",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE resource (
          resource_type   text        NOT NULL,
          account_id      char(12)    NOT NULL,
          region          text        NOT NULL,
          resource_id     text        NOT NULL,
          arn             text        NOT NULL,
          name            text,
          tags            jsonb       NOT NULL DEFAULT '{}',
          relations       jsonb       NOT NULL DEFAULT '{}',
          data            jsonb       NOT NULL,
          content_hash    bytea       NOT NULL,
          observed_at     timestamptz NOT NULL,
          first_seen_at   timestamptz NOT NULL,
          last_changed_at timestamptz NOT NULL,
          deleted_at      timestamptz,
          last_job_id     text,
          PRIMARY KEY (resource_type, account_id, region, resource_id)
        ) PARTITION BY LIST (resource_type)
        """
    )
    op.execute("CREATE INDEX ix_resource_arn ON resource (arn)")
    op.execute(
        "CREATE INDEX ix_resource_scope ON resource (account_id, region) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_resource_changed "
        "ON resource (last_changed_at, account_id, region, resource_id)"
    )
    op.execute("CREATE INDEX ix_resource_name ON resource (name text_pattern_ops)")
    op.execute("CREATE INDEX ix_resource_tags ON resource USING gin (tags jsonb_path_ops)")
    op.execute(
        "CREATE INDEX ix_resource_deleted ON resource (deleted_at) WHERE deleted_at IS NOT NULL"
    )
    op.execute("CREATE TABLE r_default PARTITION OF resource DEFAULT")
    for resource_type in V1_RESOURCE_TYPES:
        create_resource_partition(resource_type)

    op.execute(
        """
        CREATE TABLE scope_state (
          account_id      char(12)    NOT NULL,
          region          text        NOT NULL,
          resource_type   text        NOT NULL,
          status          text        NOT NULL,
          resource_count  integer,
          last_attempt_at timestamptz NOT NULL,
          last_success_at timestamptz,
          last_job_id     text,
          error_code      text,
          error_message   text,
          PRIMARY KEY (account_id, region, resource_type)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE account_region (
          account_id char(12)    NOT NULL,
          region     text        NOT NULL,
          enabled    boolean     NOT NULL,
          checked_at timestamptz NOT NULL,
          PRIMARY KEY (account_id, region)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE scan_job (
          job_id      text        PRIMARY KEY,
          trigger     text        NOT NULL,
          scope       jsonb       NOT NULL,
          scope_hash  text        NOT NULL,
          status      text        NOT NULL,
          request_id  text,
          flags       jsonb       NOT NULL DEFAULT '{}',
          stats       jsonb       NOT NULL DEFAULT '{}',
          error       text,
          created_at  timestamptz NOT NULL,
          started_at  timestamptz,
          finished_at timestamptz,
          deadline_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_scan_job_active_scope ON scan_job (scope_hash) "
        "WHERE status IN ('queued', 'running')"
    )
    op.execute("CREATE INDEX ix_scan_job_created ON scan_job (created_at DESC)")
    op.execute(
        """
        CREATE TABLE scan_task (
          task_id        text        PRIMARY KEY,
          job_id         text        NOT NULL REFERENCES scan_job(job_id),
          account_id     char(12)    NOT NULL,
          region         text        NOT NULL,
          resource_types text[],
          resource_ids   text[],
          queue          text        NOT NULL,
          status         text        NOT NULL,
          attempts       integer     NOT NULL DEFAULT 0,
          result         jsonb       NOT NULL DEFAULT '{}',
          error_code     text,
          error_message  text,
          worker_id      text,
          queued_at      timestamptz NOT NULL,
          started_at     timestamptz,
          finished_at    timestamptz,
          updated_at     timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_scan_task_job ON scan_task (job_id, status)")
    op.execute(
        "CREATE INDEX ix_scan_task_active ON scan_task (status, updated_at) "
        "WHERE status IN ('queued', 'running', 'retrying')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE scan_task")
    op.execute("DROP TABLE scan_job")
    op.execute("DROP TABLE account_region")
    op.execute("DROP TABLE scope_state")
    for resource_type in V1_RESOURCE_TYPES:
        drop_resource_partition(resource_type)
    op.execute("DROP TABLE r_default")
    op.execute("DROP TABLE resource")
