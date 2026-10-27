"""Credential broker v2: ADFS SAML login + AssumeRoleWithSAML, cached in Redis."""

from __future__ import annotations

from inventory.models import RETRYABLE_BROKER_CODES, BrokerErrorCode


class BrokerError(Exception):
    def __init__(self, code: BrokerErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: BrokerErrorCode = code
        self.message = message
        self.retryable = code in RETRYABLE_BROKER_CODES


class SamlExpiredError(Exception):
    """STS rejected the cached assertion as expired or invalid; the caller re-logs in once."""
