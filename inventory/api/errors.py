"""Uniform error responses: {"error": {"code", "message", "request_id", "details"}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from inventory.api.schemas import ErrorBody, ErrorResponse
from inventory.errors import InventoryError
from inventory.observability import get_context

log = logging.getLogger(__name__)
_STATUS_CODES = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "VALIDATION_ERROR",
}


class ValidationFailedError(InventoryError):
    default_code = "VALIDATION_ERROR"
    http_status = 422

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details


def error_response(
    status: int, code: str, message: str, details: list[dict[str, Any]] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=get_context().get("request_id"),
            details=details,
        )
    )
    level = logging.ERROR if status >= 500 else logging.INFO
    log.log(level, "request failed", extra={"status": status, "error_code": code})
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


async def _inventory_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, InventoryError)
    response = error_response(exc.http_status, exc.code, exc.message, getattr(exc, "details", None))
    if exc.http_status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()  # "input" is dropped on purpose: never echo request values
    ]
    return error_response(422, "VALIDATION_ERROR", "request validation failed", details)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
    return error_response(exc.status_code, code, str(exc.detail))


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(InventoryError, _inventory_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
