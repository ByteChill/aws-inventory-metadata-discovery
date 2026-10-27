"""Logging, request context, redaction, AWS call logging and metrics.

Business code logs with ``logging.getLogger(__name__)`` and passes structured fields via
``extra``. Entry points call :func:`setup_logging` once.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import random
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from typing import Any

from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter
from aws_lambda_powertools.metrics import MetricUnit, single_metric

from inventory.errors import THROTTLE_CODES

CONTEXT_FIELDS = (
    "request_id",
    "job_id",
    "task_id",
    "account_id",
    "region",
    "resource_type",
    "arn",
)
METRIC_NAMESPACE = "Inventory"
SENSITIVE_KEYS = frozenset(
    {
        "secretaccesskey",
        "secret_access_key",
        "sessiontoken",
        "session_token",
        "samlresponse",
        "saml_response",
        "samlassertion",
        "saml_assertion",
        "saml_b64",
        "password",
        "credentials",
        "authorization",
    }
)
REDACTED = "***"
NOISY_LOGGERS = ("botocore", "boto3", "urllib3", "httpx", "httpcore", "s3transfer", "asyncio")

_context: contextvars.ContextVar[Mapping[str, str] | None] = contextvars.ContextVar(
    "inventory_log_context", default=None
)
_service_name = "inventory"
_RESERVED_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- context


def _current() -> Mapping[str, str]:
    return _context.get() or {}


def get_context() -> dict[str, str]:
    return dict(_current())


def bind_context(**fields: str | None) -> contextvars.Token[Mapping[str, str] | None]:
    merged = {**_current(), **{k: v for k, v in fields.items() if v is not None}}
    return _context.set(merged)


def reset_context(token: contextvars.Token[Mapping[str, str] | None]) -> None:
    _context.reset(token)


@contextmanager
def context_scope(**fields: str | None) -> Iterator[None]:
    token = bind_context(**fields)
    try:
        yield
    finally:
        _context.reset(token)


def submit_with_context[**P, R](
    executor: Executor, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> Future[R]:
    """Submit to a thread pool carrying the caller's contextvars (log context)."""
    ctx = contextvars.copy_context()
    return executor.submit(lambda: ctx.run(fn, *args, **kwargs))


# --------------------------------------------------------------------------- filters


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _current().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: REDACTED if str(k).lower() in SENSITIVE_KEYS else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, Mapping):
            record.msg = redact(record.msg)
        if isinstance(record.args, Mapping):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        for key in list(record.__dict__):
            if key in _RESERVED_RECORD_ATTRS:
                continue
            if key.lower() in SENSITIVE_KEYS:
                setattr(record, key, REDACTED)
            else:
                setattr(record, key, redact(record.__dict__[key]))
        return True


# --------------------------------------------------------------------------- setup


def setup_logging(service_name: str, level: str = "INFO", sample_rate: float = 0.0) -> None:
    """Configure the root logger to emit JSON with context and redaction."""
    global _service_name
    _service_name = service_name

    effective = "DEBUG" if sample_rate and random.random() < sample_rate else level.upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        LambdaPowertoolsFormatter(utc=True, use_rfc3339=True, service=service_name)
    )
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(effective)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def install_filters(logger: logging.Logger) -> None:
    """Attach context + redaction filters to an existing logger's handlers (e.g. Powertools)."""
    for handler in logger.handlers:
        handler.addFilter(ContextFilter())
        handler.addFilter(RedactionFilter())


# --------------------------------------------------------------------------- metrics


class _MetricBuffer:
    """Aggregates metrics in memory and sends them with PutMetricData every ``interval``."""

    def __init__(self, put: Callable[[list[dict[str, Any]]], None], interval: float) -> None:
        self._put = put
        self._interval = interval
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[float]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="metrics")
        self._thread.start()

    def add(self, name: str, value: float, unit: str, dims: Mapping[str, str]) -> None:
        key = (name, unit, tuple(sorted(dims.items())))
        with self._lock:
            self._data.setdefault(key, []).append(value)

    def flush(self) -> None:
        with self._lock:
            data, self._data = self._data, {}
        datums = [
            {
                "MetricName": name,
                "Unit": unit,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims],
                "StatisticValues": {
                    "SampleCount": float(len(values)),
                    "Sum": float(sum(values)),
                    "Minimum": float(min(values)),
                    "Maximum": float(max(values)),
                },
            }
            for (name, unit, dims), values in data.items()
        ]
        for start in range(0, len(datums), 1000):
            try:
                self._put(datums[start : start + 1000])
            except Exception:
                log.warning("PutMetricData failed; metrics dropped", exc_info=True)

    def close(self) -> None:
        self._stop.set()
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()


_buffer: _MetricBuffer | None = None


def enable_metric_api(cloudwatch: Any, interval_seconds: float = 60) -> None:
    """Switch metric() to buffered PutMetricData (for ECS, where EMF is not extracted)."""
    global _buffer

    def put(datums: list[dict[str, Any]]) -> None:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=datums)

    _buffer = _MetricBuffer(put, interval_seconds)
    atexit.register(_buffer.close)


def metric(
    name: str,
    value: float = 1,
    unit: str = "Count",
    dims: Mapping[str, str] | None = None,
) -> None:
    """Emit one CloudWatch metric (EMF line, or buffered API call). Never raises."""
    if _buffer is not None:
        _buffer.add(name, value, unit, {"Service": _service_name, **(dims or {})})
        return
    try:
        with single_metric(
            name=name,
            unit=MetricUnit(unit),
            value=value,
            namespace=METRIC_NAMESPACE,
            default_dimensions={"Service": _service_name},
        ) as m:
            for key, dim_value in (dims or {}).items():
                m.add_dimension(name=key, value=dim_value)
    except Exception:  # metrics must never break the caller
        log.debug("metric emit failed", extra={"metric": name}, exc_info=True)


# --------------------------------------------------------------------------- timing


@contextmanager
def timed(event: str, level: int = logging.INFO, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log ``event`` with ``duration_ms`` on exit. The yielded dict can add more fields."""
    extra: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    try:
        yield extra
    except Exception as exc:
        extra["duration_ms"] = int((time.perf_counter() - start) * 1000)
        extra["error_code"] = getattr(exc, "code", type(exc).__name__)
        log.log(max(level, logging.WARNING), event, extra=extra)
        raise
    extra["duration_ms"] = int((time.perf_counter() - start) * 1000)
    log.log(level, event, extra=extra)


# --------------------------------------------------------------------------- AWS calls

_aws_log = logging.getLogger("inventory.aws.calls")


def register_call_logging(client: Any) -> None:
    """Log every API call made by a boto3 client (service, operation, latency, retries)."""
    region = client.meta.region_name

    def before_call(context: dict[str, Any], **_: Any) -> None:
        context["inventory_started"] = time.perf_counter()

    def after_call(
        http_response: Any, parsed: Mapping[str, Any], model: Any, context: dict[str, Any], **_: Any
    ) -> None:
        meta = parsed.get("ResponseMetadata", {})
        error_code = parsed.get("Error", {}).get("Code")
        retries = int(meta.get("RetryAttempts", 0))
        fields = {
            "aws_service": model.service_model.service_name,
            "aws_operation": model.name,
            "aws_region": region,
            "http_status": meta.get("HTTPStatusCode", getattr(http_response, "status_code", None)),
            "aws_request_id": meta.get("RequestId"),
            "retry_attempts": retries,
            "duration_ms": _elapsed_ms(context),
        }
        if error_code:
            fields["error_code"] = error_code
        throttled = error_code in THROTTLE_CODES
        if throttled or retries > 0:
            _aws_log.warning("aws call retried or throttled", extra=fields)
            metric(
                "AwsThrottle",
                dims={"AwsService": fields["aws_service"], "Operation": model.name},
            )
        else:
            _aws_log.debug("aws call", extra=fields)

    def after_call_error(exception: BaseException, context: dict[str, Any], **kw: Any) -> None:
        _aws_log.debug(
            "aws call failed",
            extra={
                "aws_region": region,
                "error_code": type(exception).__name__,
                "duration_ms": _elapsed_ms(context),
            },
        )

    events = client.meta.events
    events.register("before-call", before_call)
    events.register("after-call", after_call)
    events.register("after-call-error", after_call_error)


def _elapsed_ms(context: Mapping[str, Any]) -> int | None:
    started = context.get("inventory_started")
    return None if started is None else int((time.perf_counter() - started) * 1000)
