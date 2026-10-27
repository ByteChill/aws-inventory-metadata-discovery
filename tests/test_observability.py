from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest

from inventory.observability import (
    ContextFilter,
    RedactionFilter,
    bind_context,
    context_scope,
    get_context,
    redact,
    register_call_logging,
    setup_logging,
    submit_with_context,
    timed,
)
from tests.helpers import load_fixture


def test_context_is_isolated_between_async_tasks() -> None:
    async def handle(request_id: str) -> str | None:
        bind_context(request_id=request_id)
        await asyncio.sleep(0)
        return get_context().get("request_id")

    async def main() -> list[str | None]:
        return list(await asyncio.gather(*(handle(f"req-{i}") for i in range(20))))

    assert asyncio.run(main()) == [f"req-{i}" for i in range(20)]


def test_submit_with_context_carries_fields_into_threads() -> None:
    with context_scope(task_id="t-1", account_id="123456789012"), ThreadPoolExecutor(2) as pool:
        plain = pool.submit(get_context).result()
        carried = submit_with_context(pool, get_context).result()

    assert carried == {"task_id": "t-1", "account_id": "123456789012"}
    assert "task_id" not in plain
    assert get_context() == {}


def test_redaction_filter_masks_nested_secrets() -> None:
    case = load_fixture("foundation/redaction.input.json")
    expected = load_fixture("foundation/redaction.expected.json")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, case["msg"], None, None)
    for key, value in case["extra"].items():
        setattr(record, key, value)

    RedactionFilter().filter(record)

    assert record.msg == expected["msg"]
    for key, value in expected["extra"].items():
        assert getattr(record, key) == value


def test_redact_leaves_scalars_alone() -> None:
    assert redact("plain") == "plain"
    assert redact(["a", {"token": 1}]) == ["a", {"token": 1}]


def test_setup_logging_emits_json_with_context(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("inventory-test", level="INFO")
    with context_scope(request_id="req-1", job_id="job-1"):
        logging.getLogger("inventory.test").info(
            "hello", extra={"resource_type": "AWS::EC2::VPC", "password": "pw"}
        )

    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["service"] == "inventory-test"
    assert payload["request_id"] == "req-1"
    assert payload["job_id"] == "job-1"
    assert payload["resource_type"] == "AWS::EC2::VPC"
    assert payload["password"] == "***"


def test_timed_logs_duration_and_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with timed("work done", step="a"):
        pass
    with pytest.raises(ValueError), timed("work failed"):
        raise ValueError("boom")

    done, failed = [r for r in caplog.records if r.getMessage().startswith("work")]
    assert done.step == "a" and isinstance(done.duration_ms, int)  # type: ignore[attr-defined]
    assert failed.levelno == logging.WARNING
    assert failed.error_code == "ValueError"  # type: ignore[attr-defined]


def test_context_filter_does_not_override_explicit_fields() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    record.region = "explicit"
    with context_scope(region="from-context", job_id="j"):
        ContextFilter().filter(record)
    assert record.region == "explicit"
    assert record.job_id == "j"  # type: ignore[attr-defined]


def test_aws_calls_are_logged(aws: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="inventory.aws.calls")
    client = boto3.client("ec2", region_name="eu-west-1")
    register_call_logging(client)

    client.describe_vpcs()

    record = next(r for r in caplog.records if r.name == "inventory.aws.calls")
    assert record.aws_service == "ec2"  # type: ignore[attr-defined]
    assert record.aws_operation == "DescribeVpcs"  # type: ignore[attr-defined]
    assert record.aws_region == "eu-west-1"  # type: ignore[attr-defined]
    assert record.http_status == 200  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, int)  # type: ignore[attr-defined]


def test_metric_api_mode_aggregates_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    import inventory.observability as obs

    sent: list[dict[str, object]] = []

    class FakeCloudWatch:
        def put_metric_data(self, Namespace: str, MetricData: list[dict[str, object]]) -> None:
            assert Namespace == "Inventory"
            sent.extend(MetricData)

    monkeypatch.setattr(obs, "_buffer", None)
    obs.enable_metric_api(FakeCloudWatch(), interval_seconds=3600)
    try:
        obs.metric("TaskSucceeded", dims={"Region": "eu-west-1"})
        obs.metric("TaskSucceeded", dims={"Region": "eu-west-1"})
        obs.metric("TaskDuration", 120, unit="Milliseconds", dims={"Region": "eu-west-1"})
        assert obs._buffer is not None
        obs._buffer.flush()
    finally:
        monkeypatch.setattr(obs, "_buffer", None)

    by_name = {d["MetricName"]: d for d in sent}
    assert by_name["TaskSucceeded"]["StatisticValues"]["Sum"] == 2.0  # type: ignore[index]
    assert by_name["TaskDuration"]["Unit"] == "Milliseconds"
    assert {"Name": "Region", "Value": "eu-west-1"} in by_name["TaskSucceeded"]["Dimensions"]  # type: ignore[operator]
