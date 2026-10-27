"""Error types shared by every component.

Callers decide what to do from ``code`` and ``retryable``; they never parse messages.
"""

from __future__ import annotations

from typing import ClassVar

THROTTLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "RequestThrottled",
        "RequestThrottledException",
        "SlowDown",
        "ProvisionedThroughputExceededException",
    }
)
ACCESS_DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess"}
)
REGION_NOT_ENABLED_CODES = frozenset({"OptInRequired", "AuthFailure"})


class InventoryError(Exception):
    default_code: ClassVar[str] = "INTERNAL_ERROR"
    default_retryable: ClassVar[bool] = False
    http_status: ClassVar[int] = 500

    def __init__(
        self, message: str = "", *, code: str | None = None, retryable: bool | None = None
    ) -> None:
        self.code = code or self.default_code
        self.message = message or self.code
        self.retryable = self.default_retryable if retryable is None else retryable
        super().__init__(self.message)


class BadRequestError(InventoryError):
    default_code = "BAD_REQUEST"
    http_status = 400


class NotFoundError(InventoryError):
    default_code = "NOT_FOUND"
    http_status = 404


class UnauthenticatedError(InventoryError):
    """No or invalid credentials (API)."""

    default_code = "UNAUTHENTICATED"
    http_status = 401


class ForbiddenError(InventoryError):
    """Authenticated, but the principal lacks the permission (API)."""

    default_code = "FORBIDDEN"
    http_status = 403


class AccessDeniedError(InventoryError):
    default_code = "ACCOUNT_ACCESS_DENIED"
    http_status = 403


class ThrottledError(InventoryError):
    default_code = "UPSTREAM_THROTTLED"
    default_retryable = True
    http_status = 503


class UpstreamError(InventoryError):
    """An AWS or broker failure; pass ``retryable=`` per instance."""

    default_code = "UPSTREAM_ERROR"
    http_status = 502


class NotImplementedFeatureError(InventoryError):
    default_code = "NOT_IMPLEMENTED"
    http_status = 501
