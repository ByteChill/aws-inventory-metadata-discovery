"""Worker process for ECS Fargate: poll the region's queues and run scan tasks.

Run with ``python -m inventory.handlers.worker``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from inventory.aws import AwsClient
from inventory.bootstrap import build_runtime
from inventory.config import WorkerSettings, get_common_settings, get_worker_settings
from inventory.engine import HandleOutcome, TaskExecutor
from inventory.observability import (
    context_scope,
    enable_metric_api,
    setup_logging,
    submit_with_context,
)

SERVICE_NAME = "inventory-worker"
log = logging.getLogger(__name__)

Handler = Callable[[str, Callable[[], None]], HandleOutcome]


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        sqs: AwsClient,
        handle: Handler,
        *,
        ondemand_url: str,
        scheduled_url: str,
    ) -> None:
        self.settings = settings
        self.sqs = sqs
        self.handle = handle
        self.ondemand_url = ondemand_url
        self.scheduled_url = scheduled_url
        self._stopping = threading.Event()
        self._in_flight: set[Future[None]] = set()
        self._lock = threading.Lock()

    def stop(self, *_: Any) -> None:
        if not self._stopping.is_set():
            log.info("shutdown requested; finishing in-flight messages")
        self._stopping.set()

    def receive(self, free_slots: int) -> list[tuple[str, dict[str, Any]]]:
        """On-demand work first; only then the scheduled queue."""
        for url, wait_seconds in (
            (self.ondemand_url, self.settings.ondemand_wait_seconds),
            (self.scheduled_url, self.settings.scheduled_wait_seconds),
        ):
            response = self.sqs.receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=min(10, free_slots),
                WaitTimeSeconds=wait_seconds,
                VisibilityTimeout=self.settings.visibility_timeout_seconds,
                MessageAttributeNames=["All"],
            )
            messages = response.get("Messages", [])
            if messages:
                return [(url, m) for m in messages]
        return []

    def process(self, url: str, message: dict[str, Any]) -> None:
        receipt = message["ReceiptHandle"]

        def extend() -> None:
            self.sqs.change_message_visibility(
                QueueUrl=url,
                ReceiptHandle=receipt,
                VisibilityTimeout=self.settings.visibility_timeout_seconds,
            )

        try:
            outcome = self.handle(message["Body"], extend)
        except Exception:
            log.exception("message handling failed; leaving it for redelivery")
            return
        if outcome is HandleOutcome.DELETE:
            self.sqs.delete_message(QueueUrl=url, ReceiptHandle=receipt)

    def poll_once(self) -> int:
        """Receive and process one batch synchronously (used by tests and local runs)."""
        batch = self.receive(self.settings.message_concurrency)
        for url, message in batch:
            self.process(url, message)
        return len(batch)

    def run(self) -> int:
        """Poll until stopped; returns the number of messages still running at exit."""
        concurrency = self.settings.message_concurrency
        # not a context manager: on shutdown we wait at most shutdown_grace_seconds
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="task")
        try:
            while not self._stopping.is_set():
                with self._lock:
                    free = concurrency - len(self._in_flight)
                if free <= 0:
                    wait(list(self._in_flight), timeout=1, return_when="FIRST_COMPLETED")
                    continue
                try:
                    batch = self.receive(free)
                except Exception:
                    log.exception("receive_message failed")
                    self._stopping.wait(5)
                    continue
                for url, message in batch:
                    future = submit_with_context(pool, self.process, url, message)
                    with self._lock:
                        self._in_flight.add(future)
                    future.add_done_callback(self._done)
            with self._lock:
                pending = list(self._in_flight)
            _, not_done = wait(pending, timeout=self.settings.shutdown_grace_seconds)
            if not_done:
                log.warning(
                    "shutting down with unfinished messages; they will be redelivered",
                    extra={"unfinished": len(not_done)},
                )
            return len(not_done)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _done(self, future: Future[None]) -> None:
        with self._lock:
            self._in_flight.discard(future)


def worker_id(settings: WorkerSettings) -> str:
    task = (settings.ecs_metadata_uri or "").rstrip("/").rsplit("/", 1)[-1]
    return f"{task or socket.gethostname()}:{os.getpid()}"


def main() -> None:
    settings = get_worker_settings()
    common = get_common_settings()
    # logging first, so startup failures (secrets, database) are logged as JSON
    setup_logging(SERVICE_NAME, common.log_level, common.log_sample_rate)
    runtime = build_runtime(
        SERVICE_NAME,
        statement_timeout_ms=60_000,
        pool_size=settings.message_concurrency + 2,
        type_concurrency=settings.type_concurrency,
    )
    if common.metrics_mode == "api":
        enable_metric_api(runtime.clients.client("monitoring", settings.region))
    cell = runtime.common.cell(settings.region)
    if not cell.ondemand_queue_url or not cell.scheduled_queue_url:
        raise SystemExit(f"cell {settings.region} has no queue URLs configured")
    executor = TaskExecutor(
        runtime.engine,
        runtime.db,
        runtime.dispatcher,
        worker_id=worker_id(settings),
        max_attempts=settings.max_attempts,
        retry_base_delay_seconds=settings.retry_base_delay_seconds,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        task_stale_minutes=settings.task_stale_minutes,
        region=settings.region,
    )
    worker = Worker(
        settings,
        runtime.clients.client("sqs", settings.region),
        executor.handle,
        ondemand_url=cell.ondemand_queue_url,
        scheduled_url=cell.scheduled_queue_url,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    with context_scope(region=settings.region):
        log.info("worker started")
        unfinished = worker.run()
        log.info("worker stopped", extra={"unfinished": unfinished})
    if unfinished:
        # pool threads are not daemons: exit now instead of blocking until SIGKILL; the
        # unfinished messages are redelivered and taken over once their heartbeat is stale
        logging.shutdown()
        os._exit(0)


if __name__ == "__main__":
    main()
