"""Request id, access log and last-resort error handling (pure ASGI middleware)."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from ulid import ULID

from inventory.observability import bind_context, reset_context

log = logging.getLogger("inventory.api.access")
REQUEST_ID_HEADER = b"x-request-id"
TRACE_ID_HEADER = b"x-trace-id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware:
    """Binds ``request_id`` for the whole request and echoes it in every response.

    Implemented as raw ASGI (not BaseHTTPMiddleware) so contextvars flow into the endpoint
    and streaming is untouched. Unhandled exceptions become the standard 500 error body here,
    because Starlette's own error middleware sits outside and would lose the request id.
    """

    def __init__(self, app: ASGIApp, trace_id: Callable[[], str | None] | None = None) -> None:
        self.app = app
        self.trace_id = trace_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(REQUEST_ID_HEADER, b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else str(ULID())
        token = bind_context(request_id=request_id)
        state: dict[str, Any] = {"status": 500, "started": False}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                extra = [(REQUEST_ID_HEADER, request_id.encode())]
                trace = self.trace_id() if self.trace_id else None
                if trace:
                    extra.append((TRACE_ID_HEADER, trace.encode()))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error")
            if not state["started"]:
                await _internal_error(send_wrapper, request_id)
        finally:
            path = scope.get("path", "")
            if path not in QUIET_PATHS:
                route = scope.get("route")
                client = scope.get("client") or ("", 0)
                log.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": path,
                        "route": getattr(route, "path", None),
                        "status": state["status"],
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "client_ip": client[0],
                        "user_agent": headers.get(b"user-agent", b"").decode("latin-1")[:200],
                        "client_request_id": incoming
                        if incoming and incoming != request_id
                        else None,
                    },
                )
            reset_context(token)


async def _internal_error(send: Send, request_id: str) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "internal error",
                "request_id": request_id,
                "details": None,
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
