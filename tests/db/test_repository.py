from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from inventory.collectors import registry
from inventory.db import (
    AccountRegionRepository,
    JobRepository,
    ResourceRepository,
    ScopeStateRepository,
    partition_name,
    resource,
    scan_task,
    scope_state,
)
from inventory.models import NewTask, ResourceKey, ResourceRecord, ScanScope, TypeStatus
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture

pytestmark = pytest.mark.integration
T0 = FROZEN_NOW
T1 = FROZEN_NOW + timedelta(hours=4)
repo = ResourceRepository()
jobs = JobRepository()


def rec(name: str, observed_at: datetime = T0) -> ResourceRecord:
    raw = load_fixture("db/records.json")[name]
    key = ResourceKey(raw["resource_type"], raw["account_id"], raw["region"], raw["resource_id"])
    return ResourceRecord.build(
        key,
        arn=raw["arn"],
        name=raw["name"],
        tags=raw["tags"],
        relations=raw["relations"],
        data=raw["data"],
        observed_at=observed_at,
    )


def row(db: Engine, record: ResourceRecord) -> dict[str, Any]:
    with db.connect() as conn:
        found = repo.get(conn, record.key)
    assert found is not None
    return dict(found)


# --------------------------------------------------------------------------- schema


def test_every_collector_has_a_partition(db: Engine) -> None:
    with db.connect() as conn:
        partitions = set(
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = 'resource'::regclass"
                )
            ).scalars()
        )
    assert {partition_name(c.resource_type) for c in registry.all()} <= partitions


def test_rows_land_in_their_type_partition(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1"), rec("ami_x")], "job-1")
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM r_ec2_vpc")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM r_ec2_image")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM r_default")).scalar_one() == 0


@pytest.mark.parametrize("case", load_fixture("db/hash_cases.json"))
def test_content_hash_is_order_independent(case: dict[str, Any]) -> None:
    key = ResourceKey("AWS::EC2::VPC", "123456789012", "eu-west-1", "vpc-1")

    def build(payload: dict[str, Any]) -> bytes:
        return ResourceRecord.build(
            key,
            arn="a",
            name=None,
            tags=payload["tags"],
            relations={},
            data=payload["data"],
            observed_at=T0,
        ).content_hash

    assert (build(case["a"]) == build(case["b"])) is case["equal"]


def test_unknown_value_types_are_rejected() -> None:
    key = ResourceKey("AWS::EC2::VPC", "123456789012", "eu-west-1", "vpc-1")
    with pytest.raises(TypeError):
        ResourceRecord.build(
            key, arn="a", name=None, tags={}, relations={}, data={"x": object()}, observed_at=T0
        )


# --------------------------------------------------------------------------- resources


def test_upsert_tracks_changes(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1", T0)], "job-1")
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1", T0 + timedelta(hours=1))], "job-2")  # unchanged
    unchanged = row(db, rec("vpc_a_v1"))
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v2", T1)], "job-3")  # tags changed
    changed = row(db, rec("vpc_a_v2"))

    assert unchanged["first_seen_at"] == T0
    assert unchanged["last_changed_at"] == T0
    assert unchanged["observed_at"] == T0 + timedelta(hours=1)
    assert changed["last_changed_at"] == T1
    assert changed["first_seen_at"] == T0
    assert changed["tags"] == {"Name": "main", "Env": "staging"}
    assert changed["last_job_id"] == "job-3"


def test_older_observation_never_overwrites_newer(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v2", T1)], "refresh")
        repo.upsert(conn, [rec("vpc_a_v1", T0)], "slow-scan")

    assert row(db, rec("vpc_a_v1"))["tags"]["Env"] == "staging"


def test_soft_delete_spares_rows_observed_after_scan_start(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1", T0), rec("vpc_b", T1)], "job-1")
        deleted = repo.soft_delete(
            conn,
            "AWS::EC2::VPC",
            "123456789012",
            "eu-west-1",
            ["vpc-0a", "vpc-0b"],
            before=T0 + timedelta(minutes=1),
            job_id="job-2",
            now=T1,
        )

    assert deleted == 1
    assert row(db, rec("vpc_a_v1"))["deleted_at"] == T1
    assert row(db, rec("vpc_b"))["deleted_at"] is None


def test_deleted_resource_reappears(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1", T0)], "job-1")
        repo.soft_delete(
            conn,
            "AWS::EC2::VPC",
            "123456789012",
            "eu-west-1",
            ["vpc-0a"],
            before=T1,
            job_id="job-2",
            now=T0 + timedelta(hours=1),
        )
        repo.upsert(conn, [rec("vpc_a_v1", T1)], "job-3")

    revived = row(db, rec("vpc_a_v1"))
    assert revived["deleted_at"] is None
    assert revived["last_changed_at"] == T1


def test_index_find_account_and_purge(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1"), rec("vpc_b"), rec("ami_x")], "job-1")
        repo.soft_delete(
            conn,
            "AWS::EC2::VPC",
            "123456789012",
            "eu-west-1",
            ["vpc-0b"],
            before=T1,
            job_id="job-2",
            now=T0 - timedelta(days=40),
        )
    with db.begin() as conn:
        index = repo.load_index(conn, "AWS::EC2::VPC", "123456789012", "eu-west-1")
        accounts = repo.find_account(conn, "AWS::EC2::Image", "ami-0x")
        purged = repo.purge_deleted(conn, older_than=T0 - timedelta(days=30))

    assert {k: v.deleted for k, v in index.items()} == {"vpc-0a": False, "vpc-0b": True}
    assert accounts == [("210987654321", "us-east-1", "ami-0x")]
    assert purged == 1


# --------------------------------------------------------------------------- scope & regions


def test_scope_state_keeps_last_success_on_failure(db: Engine) -> None:
    scopes = ScopeStateRepository()
    common = {"account_id": "123456789012", "region": "eu-west-1", "resource_type": "AWS::EC2::VPC"}
    with db.begin() as conn:
        scopes.upsert(
            conn,
            **common,
            status=TypeStatus.SUCCEEDED,
            attempt_at=T0,
            success_at=T0,
            resource_count=7,
            job_id="job-1",
        )
        scopes.upsert(
            conn,
            **common,
            status=TypeStatus.FAILED,
            attempt_at=T1,
            job_id="job-2",
            error_code="THROTTLED",
            error_message="Rate exceeded",
        )
    with db.connect() as conn:
        state = dict(conn.execute(select(scope_state)).mappings().one())

    assert state["status"] == "failed"
    assert state["last_success_at"] == T0
    assert state["resource_count"] == 7
    assert state["error_code"] == "THROTTLED"


def test_account_region_cache(db: Engine) -> None:
    regions = AccountRegionRepository()
    with db.begin() as conn:
        regions.replace(
            conn,
            "123456789012",
            enabled=["eu-west-1"],
            supported=["ap-east-1", "eu-west-1"],
            checked_at=T0,
        )
    with db.connect() as conn:
        fresh = regions.get_enabled(
            conn, "123456789012", max_age=timedelta(hours=24), now=T0 + timedelta(hours=1)
        )
        stale = regions.get_enabled(
            conn, "123456789012", max_age=timedelta(hours=24), now=T0 + timedelta(hours=25)
        )
        unknown = regions.get_enabled(conn, "210987654321", max_age=timedelta(hours=24), now=T0)

    assert (fresh, stale, unknown) == (["eu-west-1"], None, None)


# --------------------------------------------------------------------------- jobs


def test_active_scope_is_deduplicated(db: Engine) -> None:
    scope = ScanScope(account_id="123456789012", region="eu-west-1")
    with db.begin() as conn:
        first, created = jobs.create_job(conn, trigger="api", scope=scope, request_id="r1", now=T0)
        second, created_again = jobs.create_job(
            conn, trigger="api", scope=scope, request_id="r2", now=T0
        )

    assert created and not created_again
    assert second.job_id == first.job_id


def test_claim_is_exclusive_until_heartbeat_goes_stale(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="schedule", scope=ScanScope(all=True), now=T0)
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "scheduled")], now=T0
        )
        claimed = jobs.claim_task(
            conn, task.task_id, "w1", stale_before=T0 - timedelta(minutes=30), now=T0
        )
        duplicate = jobs.claim_task(
            conn, task.task_id, "w2", stale_before=T0 - timedelta(minutes=30), now=T0
        )
        reclaimed = jobs.claim_task(
            conn, task.task_id, "w3", stale_before=T0 + timedelta(minutes=1), now=T1
        )
        job_after = jobs.get_job(conn, job.job_id)

    assert claimed is not None and claimed.attempts == 1
    assert duplicate is None
    # a stale takeover continues the same attempt
    assert reclaimed is not None and reclaimed.worker_id == "w3" and reclaimed.attempts == 1
    assert job_after is not None and job_after.status == "running"


@pytest.mark.parametrize(
    "case", load_fixture("db/job_status_cases.json"), ids=lambda c: "+".join(c["statuses"])
)
def test_job_status_from_task_statuses(db: Engine, case: dict[str, Any]) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="schedule", scope=ScanScope(all=True), now=T0)
        tasks = jobs.create_tasks(
            conn,
            job.job_id,
            [NewTask("123456789012", f"r{i}", "scheduled") for i in range(len(case["statuses"]))],
            now=T0,
        )
        result = None
        for task, status in zip(tasks, case["statuses"], strict=True):
            if status == "running":
                jobs.claim_task(conn, task.task_id, "w", stale_before=T0, now=T0)
            else:
                result = jobs.finish_task(
                    conn,
                    task.task_id,
                    status,
                    result={"AWS::EC2::VPC": {"status": "succeeded", "created": 2, "updated": 1}},
                    now=T1,
                )
        final = jobs.get_job(conn, job.job_id)

    assert final is not None
    assert final.status == case["expected"]
    if case["expected"] != "running":
        assert result is not None and result.finished_at == T1
        assert final.stats["tasks_total"] == len(case["statuses"])
        assert final.stats["created"] == 2 * len(case["statuses"])


def test_stale_heartbeat_times_out_running_tasks_but_not_queued_ones(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(
            conn,
            trigger="api",
            scope=ScanScope(account_id="123456789012"),
            deadline_at=T0 + timedelta(hours=1),
            now=T0,
        )
        a, b = jobs.create_tasks(
            conn,
            job.job_id,
            [
                NewTask("123456789012", "eu-west-1", "ondemand"),
                NewTask("123456789012", "us-east-1", "ondemand"),
            ],
            now=T0,
        )
        jobs.claim_task(conn, a.task_id, "w", stale_before=T0, now=T0)
        stale_jobs = jobs.timeout_stale_tasks(conn, stale_before=T0 + timedelta(minutes=30), now=T1)
        overdue = jobs.overdue_jobs(conn, now=T1)
        statuses = dict(conn.execute(select(scan_task.c.task_id, scan_task.c.status)).all())
        timed_out = jobs.timeout_job(conn, job.job_id, now=T1)
        final = dict(conn.execute(select(scan_task.c.task_id, scan_task.c.status)).all())

    assert stale_jobs == [job.job_id]
    assert statuses == {a.task_id: "timed_out", b.task_id: "queued"}  # queued waits in SQS
    assert overdue == [job.job_id]
    assert final == {a.task_id: "timed_out", b.task_id: "timed_out"}
    assert timed_out is not None and timed_out.status == "timed_out"
    assert timed_out.stats["by_status"] == {"timed_out": 2}


def test_finished_task_cannot_be_overwritten(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="schedule", scope=ScanScope(all=True), now=T0)
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "scheduled")], now=T0
        )
        jobs.claim_task(conn, task.task_id, "slow", stale_before=T0, now=T0)
        jobs.claim_task(conn, task.task_id, "fast", stale_before=T1, now=T1)  # takeover
        slow_retry = jobs.mark_retrying(conn, task.task_id, {}, worker_id="slow", now=T1)
        slow_finish = jobs.finish_task(conn, task.task_id, "failed", worker_id="slow", now=T1)
        fast_finish = jobs.finish_task(conn, task.task_id, "succeeded", worker_id="fast", now=T1)
        late = jobs.finish_task(conn, task.task_id, "failed", now=T1)
        final = jobs.get_task(conn, task.task_id)

    assert not slow_retry and slow_finish is None
    assert fast_finish is not None and fast_finish.status == "succeeded"
    assert late is None
    assert final is not None and final.status == "succeeded"


def test_dispatching_job_waits_for_mark_dispatched(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(
            conn, trigger="schedule", scope=ScanScope(all=True), flags={"dispatching": True}, now=T0
        )
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "scheduled")], now=T0
        )
        early = jobs.finish_task(conn, task.task_id, "succeeded", now=T0)
        done = jobs.mark_dispatched(conn, job.job_id, now=T1)

    assert early is not None and early.status == "queued"
    assert done is not None and done.status == "succeeded" and "dispatching" not in done.flags


def test_soft_delete_blocks_resurrection_by_an_older_scan(db: Engine) -> None:
    with db.begin() as conn:
        repo.upsert(conn, [rec("vpc_a_v1", T0)], "job-1")
        repo.soft_delete(
            conn,
            "AWS::EC2::VPC",
            "123456789012",
            "eu-west-1",
            ["vpc-0a"],
            before=T0 + timedelta(minutes=2),
            job_id="refresh",
            now=T0 + timedelta(minutes=2),
        )
        # a scan that started at T0+1min (before the refresh saw the deletion) lists it again
        repo.upsert(conn, [rec("vpc_a_v1", T0 + timedelta(minutes=1))], "scan")

    assert row(db, rec("vpc_a_v1"))["deleted_at"] is not None


def test_migrations_round_trip(pg_engine: Engine) -> None:
    from tests.conftest import TEST_DB_URL, run_migrations

    run_migrations(TEST_DB_URL, "base", downgrade=True)
    run_migrations(TEST_DB_URL)
    with pg_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT to_regclass('ix_resource_type_id')")).scalar_one() is not None
        )


def test_resource_table_columns_match_model() -> None:
    assert {c.name for c in resource.columns} >= {"tags", "relations", "data", "content_hash"}


def test_claim_states_for_redelivered_messages(db: Engine) -> None:
    stale = T0 - timedelta(minutes=10)
    with db.begin() as conn:
        job, _ = jobs.create_job(
            conn, trigger="api", scope=ScanScope(account_id="123456789012"), now=T0
        )
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "ondemand")], now=T0
        )
        first = jobs.claim(conn, task.task_id, "w1", attempt=1, stale_before=stale, now=T0)
        redelivered = jobs.claim(conn, task.task_id, "w2", attempt=1, stale_before=stale, now=T0)
        jobs.mark_retrying(conn, task.task_id, {}, worker_id="w1", now=T0)
        old_attempt = jobs.claim(conn, task.task_id, "w2", attempt=1, stale_before=stale, now=T0)
        retry = jobs.claim(conn, task.task_id, "w2", attempt=2, stale_before=stale, now=T0)
        jobs.finish_task(conn, task.task_id, "succeeded", worker_id="w2", now=T0)
        after_finish = jobs.claim(conn, task.task_id, "w3", attempt=2, stale_before=stale, now=T0)

    assert first.state == "claimed"
    assert redelivered.state == "busy"  # keep the message, do not drop it
    assert old_attempt.state == "done"  # superseded by the retry
    assert retry.state == "claimed" and retry.task is not None and retry.task.attempts == 2
    assert after_finish.state == "done"


def test_concurrent_finishers_always_close_the_job(db: Engine) -> None:
    import threading

    for _ in range(10):
        with db.begin() as conn:
            job, _ = jobs.create_job(conn, trigger="schedule", scope=ScanScope(all=True), now=T0)
            tasks = jobs.create_tasks(
                conn,
                job.job_id,
                [NewTask("123456789012", r, "scheduled") for r in ("eu-west-1", "us-east-1")],
                now=T0,
            )
        barrier = threading.Barrier(2)

        def finish(task_id: str, barrier: threading.Barrier = barrier) -> None:
            with db.begin() as conn:
                barrier.wait()
                jobs.finish_task(conn, task_id, "succeeded")

        threads = [threading.Thread(target=finish, args=(t.task_id,)) for t in tasks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        with db.connect() as conn:
            final = jobs.get_job(conn, job.job_id)
        assert final is not None and final.status == "succeeded"


def test_retrying_tasks_are_left_to_the_deadline(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(
            conn, trigger="api", scope=ScanScope(account_id="123456789012"), now=T0
        )
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "ondemand")], now=T0
        )
        jobs.claim_task(conn, task.task_id, "w", stale_before=T0, now=T0)
        jobs.mark_retrying(conn, task.task_id, {}, worker_id="w", now=T0)
        affected = jobs.timeout_stale_tasks(conn, stale_before=T1, now=T1)

    assert affected == []


def test_timeout_job_uses_the_real_status_when_everything_finished(db: Engine) -> None:
    with db.begin() as conn:
        job, _ = jobs.create_job(
            conn, trigger="schedule", scope=ScanScope(all=True), flags={"dispatching": True}, now=T0
        )
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("123456789012", "eu-west-1", "scheduled")], now=T0
        )
        jobs.finish_task(conn, task.task_id, "succeeded", now=T0)
        final = jobs.timeout_job(conn, job.job_id, now=T1)

    assert final is not None and final.status == "succeeded"


def test_engine_session_settings_and_fresh_credentials(pg_engine: Engine) -> None:
    from sqlalchemy import make_url

    from inventory.db import make_engine
    from tests.conftest import TEST_DB_URL

    calls: list[int] = []

    def credentials() -> tuple[str, str]:
        calls.append(1)
        return "inventory", "inventory"

    wrong_password = (
        make_url(TEST_DB_URL).set(password="stale").render_as_string(hide_password=False)
    )
    engine = make_engine(
        wrong_password, application_name="t", statement_timeout_ms=1234, credentials=credentials
    )
    with engine.connect() as conn:
        timeout = conn.execute(text("SHOW statement_timeout")).scalar_one()
    engine.dispose()

    assert timeout == "1234ms"
    assert calls  # the current secret was used instead of the stale URL password
