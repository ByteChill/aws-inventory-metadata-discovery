from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from sqlalchemy import Engine, update

from inventory.aws import ClientFactory, EndpointResolver
from inventory.config import WorkerSettings, get_common_settings
from inventory.db import JobRepository, scan_task
from inventory.engine import HandleOutcome, TaskExecutor, _Heartbeat
from inventory.handlers.worker import Worker
from inventory.models import NewTask, ScanScope, ScanTaskMessage, TypeResult
from inventory.transport import SqsDirectDispatcher
from tests.helpers import load_fixture
from tests.planner.conftest import drain

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("moto_env")]
jobs = JobRepository()


class FakeEngine:
    def __init__(self, *scenarios: str, delay: float = 0.0, crash: bool = False) -> None:
        self.scenarios = list(scenarios)
        self.delay = delay
        self.crash = crash
        self.messages: list[ScanTaskMessage] = []

    def scan(self, message: ScanTaskMessage) -> dict[str, TypeResult]:
        self.messages.append(message)
        time.sleep(self.delay)
        if self.crash:
            raise RuntimeError("collector exploded")
        results = load_fixture(f"worker/results.{self.scenarios.pop(0)}.json")
        return {t: TypeResult.from_json(r) for t, r in results.items()}


def new_task(db: Engine, queue: str = "ondemand") -> tuple[str, str]:
    with db.begin() as conn:
        job, _ = jobs.create_job(conn, trigger="api", scope=ScanScope(account_id="123456789012"))
        (task,) = jobs.create_tasks(conn, job.job_id, [NewTask("123456789012", "eu-west-1", queue)])  # type: ignore[arg-type]
    return job.job_id, task.task_id


def body(job_id: str, task_id: str, **overrides: Any) -> str:
    message = (
        load_fixture("worker/message.ok.json") | {"job_id": job_id, "task_id": task_id} | overrides
    )
    return json.dumps(message)


def executor(db: Engine, engine: FakeEngine, **kw: Any) -> TaskExecutor:
    common = get_common_settings()
    dispatcher = SqsDirectDispatcher(common, ClientFactory(EndpointResolver(common)))
    return TaskExecutor(engine, db, dispatcher, worker_id="w-1", retry_base_delay_seconds=0, **kw)  # type: ignore[arg-type]


def noop() -> None:
    return None


def test_successful_task(db: Engine) -> None:
    job_id, task_id = new_task(db)

    outcome = executor(db, FakeEngine("ok")).handle(body(job_id, task_id), noop)

    with db.connect() as conn:
        task, job = jobs.get_task(conn, task_id), jobs.get_job(conn, job_id)
    assert outcome is HandleOutcome.DELETE
    assert task is not None and task.status == "succeeded" and task.attempts == 1
    assert job is not None and job.status == "succeeded" and job.stats["created"] == 2


def test_duplicate_delivery_is_dropped(db: Engine) -> None:
    job_id, task_id = new_task(db)
    engine = FakeEngine("ok", "ok")
    ex = executor(db, engine)

    ex.handle(body(job_id, task_id), noop)
    second = ex.handle(body(job_id, task_id), noop)

    assert second is HandleOutcome.DELETE
    assert len(engine.messages) == 1


def test_retryable_failure_requeues_only_failed_types(db: Engine) -> None:
    job_id, task_id = new_task(db)

    executor(db, FakeEngine("partial_retryable")).handle(body(job_id, task_id), noop)

    (retry,) = drain("eu-west-1", "inventory-local-ondemand")
    message = ScanTaskMessage.model_validate_json(retry["Body"])
    with db.connect() as conn:
        task = jobs.get_task(conn, task_id)
    assert (message.attempt, message.resource_types) == (2, ["AWS::EC2::Instance"])
    assert message.task_id == task_id
    assert task is not None and task.status == "retrying"
    assert task.result["AWS::EC2::VPC"]["status"] == "succeeded"


def test_retry_success_merges_results(db: Engine) -> None:
    job_id, task_id = new_task(db)
    ex = executor(db, FakeEngine("partial_retryable", "ok"))
    ex.handle(body(job_id, task_id), noop)
    (retry,) = drain("eu-west-1", "inventory-local-ondemand")

    ex.handle(retry["Body"], noop)

    with db.connect() as conn:
        task = jobs.get_task(conn, task_id)
    assert task is not None and task.status == "succeeded" and task.attempts == 2


@pytest.mark.parametrize(
    ("scenario", "attempt", "status"),
    [("exhausted", 3, "partial"), ("all_failed", 1, "failed"), ("auth_denied", 1, "auth_denied")],
)
def test_final_statuses(db: Engine, scenario: str, attempt: int, status: str) -> None:
    job_id, task_id = new_task(db)
    with db.begin() as conn:  # the state after attempt-1 earlier tries
        conn.execute(
            update(scan_task)
            .where(scan_task.c.task_id == task_id)
            .values(attempts=attempt - 1, status="retrying" if attempt > 1 else "queued")
        )

    executor(db, FakeEngine(scenario)).handle(body(job_id, task_id, attempt=attempt), noop)

    with db.connect() as conn:
        task = jobs.get_task(conn, task_id)
    assert task is not None and task.status == status
    assert drain("eu-west-1", "inventory-local-ondemand") == []


def test_crash_retries_the_whole_message(db: Engine) -> None:
    job_id, task_id = new_task(db)

    executor(db, FakeEngine(crash=True)).handle(body(job_id, task_id), noop)

    (retry,) = drain("eu-west-1", "inventory-local-ondemand")
    message = ScanTaskMessage.model_validate_json(retry["Body"])
    assert message.resource_types is None and message.attempt == 2


def test_invalid_message_is_kept_for_the_dlq(db: Engine) -> None:
    outcome = executor(db, FakeEngine()).handle(
        json.dumps(load_fixture("worker/message.invalid.json")), noop
    )
    assert outcome is HandleOutcome.KEEP


def test_heartbeat_extends_visibility_during_long_tasks(db: Engine) -> None:
    job_id, task_id = new_task(db)
    beats: list[float] = []

    executor(db, FakeEngine("ok", delay=0.2), heartbeat_interval_seconds=0.03).handle(
        body(job_id, task_id), lambda: beats.append(time.monotonic())
    )

    assert len(beats) >= 2


def test_heartbeat_survives_errors() -> None:
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("visibility call failed")

    with _Heartbeat(0.01, boom):
        time.sleep(0.05)
    assert len(calls) >= 2


# --------------------------------------------------------------------------- polling loop


def worker_settings(**overrides: Any) -> WorkerSettings:
    values = {
        "region": "eu-west-1",
        "ondemand_wait_seconds": 0,
        "scheduled_wait_seconds": 0,
        "visibility_timeout_seconds": 30,
        "message_concurrency": 2,
        **overrides,
    }
    return WorkerSettings(**values)


QUEUE_KINDS = ("ondemand", "scheduled")


def make_worker(handle: Any, **overrides: Any) -> tuple[Worker, Any]:
    sqs = boto3.client("sqs", region_name="eu-west-1")
    url = {k: sqs.get_queue_url(QueueName=f"inventory-local-{k}")["QueueUrl"] for k in QUEUE_KINDS}
    worker = Worker(
        worker_settings(**overrides),
        sqs,
        handle,
        ondemand_url=url["ondemand"],
        scheduled_url=url["scheduled"],
    )
    return worker, sqs


def test_ondemand_queue_is_polled_first() -> None:
    seen: list[str] = []
    worker, sqs = make_worker(lambda b, _: seen.append(b) or HandleOutcome.DELETE)
    sqs.send_message(QueueUrl=worker.scheduled_url, MessageBody="scheduled")
    sqs.send_message(QueueUrl=worker.ondemand_url, MessageBody="ondemand")

    worker.poll_once()
    worker.poll_once()

    assert seen == ["ondemand", "scheduled"]
    assert worker.poll_once() == 0  # both deleted


def test_kept_messages_are_not_deleted() -> None:
    worker, sqs = make_worker(lambda b, _: HandleOutcome.KEEP)
    sqs.send_message(QueueUrl=worker.ondemand_url, MessageBody="bad")

    worker.poll_once()

    attrs = sqs.get_queue_attributes(
        QueueUrl=worker.ondemand_url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    assert attrs["ApproximateNumberOfMessagesNotVisible"] == "1"


def test_stop_finishes_in_flight_work() -> None:
    started, release = threading.Event(), threading.Event()
    finished: list[str] = []

    def handle(message_body: str, _: Any) -> HandleOutcome:
        started.set()
        release.wait(5)
        finished.append(message_body)
        return HandleOutcome.DELETE

    worker, sqs = make_worker(handle, shutdown_grace_seconds=5)
    sqs.send_message(QueueUrl=worker.ondemand_url, MessageBody="long-task")
    runner = threading.Thread(target=worker.run)
    runner.start()
    assert started.wait(5)

    worker.stop()
    release.set()
    runner.join(10)

    assert not runner.is_alive()
    assert finished == ["long-task"]


def test_redelivery_while_running_is_kept(db: Engine) -> None:
    job_id, task_id = new_task(db)
    with db.begin() as conn:
        jobs.claim_task(
            conn, task_id, "other-worker", stale_before=datetime.now(UTC) - timedelta(hours=1)
        )

    outcome = executor(db, FakeEngine("ok")).handle(body(job_id, task_id), noop)

    assert outcome is HandleOutcome.KEEP


def test_bad_message_is_logged(db: Engine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="inventory.engine")

    executor(db, FakeEngine()).handle("{not json", noop)

    assert any(r.getMessage() == "invalid scan task message" for r in caplog.records)
