from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from inventory.config import clear_settings_cache, get_planner_settings
from inventory.db import JobRepository, ResourceRepository, account_region, scan_task
from inventory.errors import BadRequestError, UpstreamError
from inventory.handlers.planner_lambda import Planner
from inventory.models import NewTask, ScanScope, ScanTaskMessage
from inventory.scheduling import AccountDirectoryClient, parse_account_directory
from inventory.transport import DispatchResult
from tests.helpers import load_fixture
from tests.planner.conftest import REGIONS, drain, make_runtime

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("moto_env", "frozen_time")]


class FixtureDirectory:
    def list_accounts(self) -> list[str]:
        return parse_account_directory(load_fixture("planner/account_directory.response.json"))


def event(name: str, job_id: str | None = None) -> dict[str, Any]:
    payload = json.loads(
        json.dumps(load_fixture(f"planner/{name}.event.json")).replace("__JOB_ID__", job_id or "")
    )
    return payload  # type: ignore[no-any-return]


def planner(db: Engine) -> Planner:
    return Planner(make_runtime(db), get_planner_settings(), FixtureDirectory())  # type: ignore[arg-type]


def no_time_left() -> int:
    return 0


def test_scheduled_run_end_to_end(db: Engine) -> None:
    p = planner(db)

    created = p.handle(event("create_job"), no_time_left)
    job_id = created["job_id"]
    targets = p.handle(event("list_targets", job_id), no_time_left)
    dispatched = p.handle(event("dispatch_batch", job_id), no_time_left)

    expected = load_fixture("planner/list_targets.response.json")
    global_tasks = 1
    assert created["full_refresh"] is True and not created["skipped"]
    assert {k: targets[k] for k in expected} == expected
    per_account = len(REGIONS) + global_tasks
    assert dispatched == {
        "accounts": 5,
        "skipped_accounts": 0,
        "tasks": 5 * per_account,
        "failed_accounts": 0,
        "failed_tasks": 0,
    }
    retried = p.handle(event("dispatch_batch", job_id), no_time_left)  # Map retry
    assert (retried["skipped_accounts"], retried["tasks"]) == (5, 0)
    messages = [m for r in REGIONS for m in drain(r, "inventory-local-scheduled")]
    assert len(messages) == 5 * per_account
    body = ScanTaskMessage.model_validate_json(messages[0]["Body"])
    assert body.job_id == job_id and body.trigger == "schedule" and body.full_refresh
    assert messages[0]["MessageAttributes"]["job_id"]["StringValue"] == job_id

    # workers finish everything before dispatch is marked complete: job must stay open
    jobs = JobRepository()
    with db.begin() as conn:
        for task in jobs.list_tasks(conn, job_id):
            if task.status == "queued":
                jobs.finish_task(conn, task.task_id, "succeeded")
    assert p.handle(event("check_progress", job_id), no_time_left)["done"] is False

    p.handle(event("mark_dispatched", job_id), no_time_left)
    progress = p.handle(event("check_progress", job_id), no_time_left)
    final = p.handle(event("finalize", job_id), no_time_left)

    assert progress["done"] is True
    assert final["status"] == "succeeded"  # the denied account does not fail the run
    assert final["stats"]["by_status"] == {"succeeded": 5 * per_account, "auth_denied": 1}


def test_overlapping_schedule_is_skipped(db: Engine) -> None:
    p = planner(db)
    first = p.handle(event("create_job"), no_time_left)

    second = p.handle(event("create_job"), no_time_left)

    assert second["skipped"] is True and second["active_job_id"] == first["job_id"]


def test_enabled_regions_are_cached(db: Engine) -> None:
    scheduler = make_runtime(db).scheduler

    first = scheduler.enabled_regions("100000000001")
    with db.connect() as conn:
        cached = conn.execute(select(account_region.c.region, account_region.c.enabled)).all()

    assert first == sorted(REGIONS)
    assert sorted(cached) == sorted((r, True) for r in REGIONS)
    assert scheduler.enabled_regions("100000000001") == first


class RecordingClients:
    """ec2.describe_regions answering for an account without the opt-in primary region."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def client(self, service: str, region: str, session: Any = None) -> Any:
        clients = self

        class Ec2:
            def describe_regions(self, **_: Any) -> dict[str, Any]:
                clients.calls.append(region)
                return {"Regions": [{"RegionName": "eu-west-1"}, {"RegionName": "us-east-1"}]}

        return Ec2()


def test_enabled_regions_are_read_in_us_east_1(db: Engine) -> None:
    scheduler = make_runtime(db).scheduler
    fake = RecordingClients()
    scheduler.clients = fake  # type: ignore[assignment]

    assert scheduler.enabled_regions("100000000002") == ["eu-west-1", "us-east-1"]
    assert fake.calls == ["us-east-1"]


@pytest.mark.parametrize(
    "scope",
    [
        {"account_id": "100000000001", "region": "sa-east-1"},
        {"account_id": "100000000001", "region": "eu-west-1", "resource_type": "AWS::Nope::Thing"},
        {"account_id": "100000000001", "region": "global", "resource_type": "AWS::EC2::VPC"},
    ],
)
def test_expand_rejects_invalid_scopes(db: Engine, scope: dict[str, str]) -> None:
    with pytest.raises(BadRequestError):
        make_runtime(db).scheduler.expand(ScanScope.model_validate(scope), "ondemand")


def test_expand_type_scope(db: Engine) -> None:
    tasks = make_runtime(db).scheduler.expand(
        ScanScope(account_id="100000000001", region="eu-west-1", resource_type="AWS::EC2::VPC"),
        "ondemand",
    )
    assert tasks == [
        NewTask("100000000001", "eu-west-1", "ondemand", resource_types=["AWS::EC2::VPC"])
    ]


def test_expand_type_scope_without_region(db: Engine) -> None:
    scheduler = make_runtime(db).scheduler
    tasks = scheduler.expand(
        ScanScope(account_id="100000000001", resource_type="AWS::EC2::VPC"), "ondemand"
    )

    assert [(t.region, t.resource_types) for t in tasks] == [
        (r, ["AWS::EC2::VPC"]) for r in scheduler.enabled_regions("100000000001")
    ]
    global_tasks = scheduler.expand(
        ScanScope(account_id="100000000001", resource_type="AWS::IAM::Role"), "ondemand"
    )
    assert [(t.region, t.resource_types) for t in global_tasks] == [("global", ["AWS::IAM::Role"])]


class FailingDispatcher:
    def dispatch(self, messages: Any, delay_seconds: int = 0) -> DispatchResult:
        return DispatchResult(failed=[(m.task_id, "QueueDoesNotExist") for m, _ in messages])


def test_failed_dispatch_marks_tasks(db: Engine) -> None:
    runtime = make_runtime(db)
    runtime.scheduler.dispatcher = FailingDispatcher()
    jobs = JobRepository()
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="api", scope=ScanScope(account_id="100000000001"))

    runtime.scheduler.submit(job, [NewTask("100000000001", "eu-west-1", "ondemand")])

    with db.connect() as conn:
        task = conn.execute(select(scan_task)).mappings().one()
        final = jobs.get_job(conn, job.job_id)
    assert (task["status"], task["error_code"]) == ("failed", "DISPATCH_FAILED")
    assert final is not None and final.status == "failed"


def test_maintenance_runs(db: Engine) -> None:
    result = planner(db).handle(event("maintenance"), lambda: 60_000)
    assert result == {"stale_jobs": [], "overdue_jobs": [], "purged": 0}


def test_maintenance_purges_after_the_configured_retention(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.db.test_repository import FROZEN_NOW, rec

    deleted_at = FROZEN_NOW - timedelta(days=100)
    resources = ResourceRepository()
    with db.begin() as conn:
        resources.upsert(conn, [rec("vpc_b", deleted_at - timedelta(hours=1))], "job-1")
        resources.soft_delete(
            conn,
            "AWS::EC2::VPC",
            "123456789012",
            "eu-west-1",
            ["vpc-0b"],
            before=deleted_at,
            job_id="job-2",
            now=deleted_at,
        )

    kept = planner(db).handle(event("maintenance"), lambda: 60_000)["purged"]  # default 180 days
    monkeypatch.setenv("PLANNER_DELETED_RETENTION_DAYS", "90")
    clear_settings_cache()
    purged = planner(db).handle(event("maintenance"), lambda: 60_000)["purged"]

    assert (kept, purged) == (0, 1)


def test_account_directory_adapter() -> None:
    body = load_fixture("planner/account_directory.response.json")
    assert parse_account_directory(body)[-1] == "100000000006"  # SUSPENDED dropped
    with pytest.raises(UpstreamError):
        parse_account_directory({"unexpected": True})

    class FakeLambda:
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            assert json.loads(kwargs["Payload"]) == load_fixture(
                "planner/account_directory.request.json"
            )
            return {"Payload": io.BytesIO(json.dumps(body).encode())}

    class Clients:
        def client(self, service: str, region: str) -> FakeLambda:
            return FakeLambda()

    client = AccountDirectoryClient(Clients(), "aws-inventory-account-directory", "ap-east-1")  # type: ignore[arg-type]
    assert len(client.list_accounts()) == 6


def test_message_contract_fixture() -> None:
    message = ScanTaskMessage.model_validate(load_fixture("planner/scan_task_message.json"))
    assert message.model_dump(mode="json") == load_fixture("planner/scan_task_message.json")


def test_state_machine_definition() -> None:
    from tests.conftest import REPO_ROOT

    text = (REPO_ROOT / "terraform/statemachines/scan_run.asl.json").read_text()
    definition = json.loads(text)
    actions = {state["Parameters"]["Payload"]["action"] for state in _task_states(definition)}
    assert actions == {
        "create_job",
        "list_targets",
        "dispatch_batch",
        "mark_dispatched",
        "check_progress",
        "finalize",
    }
    assert set(_placeholders(text)) == {"planner_function_arn"}


def _task_states(definition: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for state in definition["States"].values():
        if state["Type"] == "Task":
            found.append(state)
        if state["Type"] == "Map":
            found.extend(_task_states(state["ItemProcessor"]))
    return found


def _placeholders(text: str) -> list[str]:
    import re

    return re.findall(r"\$\{([a-z_]+)\}", text)


def test_second_run_of_the_day_is_incremental(db: Engine) -> None:
    p = planner(db)
    first = p.handle(event("create_job"), no_time_left)
    p.handle(event("mark_dispatched", first["job_id"]), no_time_left)  # no tasks: job finishes

    second = p.handle(event("create_job"), no_time_left)

    assert first["full_refresh"] is True
    assert second["full_refresh"] is False and not second["skipped"]


def test_finalize_with_error_before_deadline_keeps_the_work(db: Engine) -> None:
    p = planner(db)
    job_id = p.handle(event("create_job"), no_time_left)["job_id"]
    p.handle(event("list_targets", job_id), no_time_left)
    p.handle(event("dispatch_batch", job_id), no_time_left)

    result = p.handle(
        event("finalize", job_id) | {"error": {"Error": "Lambda.Timeout"}}, no_time_left
    )

    with db.connect() as conn:
        statuses = {t.status for t in JobRepository().list_tasks(conn, job_id)}
    assert result["status"] in ("queued", "running")  # still open, nothing thrown away
    assert "queued" in statuses and "timed_out" not in statuses


def test_maintenance_times_out_dead_workers(db: Engine) -> None:
    from datetime import timedelta

    from tests.conftest import FROZEN_NOW

    jobs = JobRepository()
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="api", scope=ScanScope(account_id="100000000001"))
        (task,) = jobs.create_tasks(
            conn, job.job_id, [NewTask("100000000001", "eu-west-1", "ondemand")]
        )
        jobs.claim_task(
            conn,
            task.task_id,
            "dead-worker",
            stale_before=FROZEN_NOW,
            now=FROZEN_NOW - timedelta(hours=1),
        )

    result = planner(db).handle(event("maintenance"), lambda: 60_000)

    assert result["stale_jobs"] == [job.job_id]
    with db.connect() as conn:
        final = jobs.get_job(conn, job.job_id)
    assert final is not None and final.status == "partial"


def test_dispatcher_routes_by_region_and_global(db: Engine) -> None:
    runtime = make_runtime(db)
    base = load_fixture("planner/scan_task_message.json")
    messages = [
        (ScanTaskMessage.model_validate(base | {"task_id": f"t-{r}", "region": r}), "scheduled")
        for r in (*REGIONS, "global")
    ]

    result = runtime.dispatcher.dispatch(messages)

    assert sorted(result.succeeded) == sorted(f"t-{r}" for r in (*REGIONS, "global"))
    us_east = [
        json.loads(m["Body"])["region"] for m in drain("us-east-1", "inventory-local-scheduled")
    ]
    assert sorted(us_east) == ["global", "us-east-1"]
    assert len(drain("eu-west-2", "inventory-local-scheduled")) == 1


def test_expand_account_scope_always_adds_global(db: Engine) -> None:
    tasks = make_runtime(db).scheduler.expand(ScanScope(account_id="100000000001"), "ondemand")

    assert sorted(t.region for t in tasks) == sorted([*REGIONS, "global"])


class RaisingDispatcher:
    def dispatch(self, messages: Any, delay_seconds: int = 0) -> DispatchResult:
        raise KeyError("no cell configured")


def test_dispatch_exception_marks_tasks_failed(db: Engine) -> None:
    runtime = make_runtime(db)
    runtime.scheduler.dispatcher = RaisingDispatcher()
    jobs = JobRepository()
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="api", scope=ScanScope(account_id="100000000001"))

    runtime.scheduler.submit(job, [NewTask("100000000001", "eu-west-1", "ondemand")])

    with db.connect() as conn:
        (task,) = jobs.list_tasks(conn, job.job_id)
    assert (task.status, task.error_code) == ("failed", "DISPATCH_FAILED")
