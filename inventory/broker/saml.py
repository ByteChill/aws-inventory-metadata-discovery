"""Parse the AWS-relevant parts of a SAML response. Signatures are verified by STS."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree

from inventory.broker import BrokerError

NS_ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
ROLE_ATTRIBUTE = "https://aws.amazon.com/SAML/Attributes/Role"
SESSION_DURATION_ATTRIBUTE = "https://aws.amazon.com/SAML/Attributes/SessionDuration"


@dataclass(frozen=True, slots=True)
class RolePair:
    role_arn: str
    principal_arn: str

    @property
    def account_id(self) -> str:
        return self.role_arn.split(":")[4]

    @property
    def role_name(self) -> str:
        return self.role_arn.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class SamlAssertion:
    raw_b64: str = field(repr=False)
    expires_at: datetime
    session_duration: int | None
    roles: dict[str, list[RolePair]]

    @classmethod
    def parse(cls, saml_b64: str) -> SamlAssertion:
        try:
            root = ElementTree.fromstring(base64.b64decode(saml_b64, validate=True))
        except (binascii.Error, ParseError, ValueError) as exc:
            raise BrokerError("ADFS_UNAVAILABLE", "SAMLResponse is not valid base64 XML") from exc

        expires_at = _expiry(root)
        if expires_at is None:
            raise BrokerError("ADFS_UNAVAILABLE", "SAMLResponse has no NotOnOrAfter")

        roles: dict[str, list[RolePair]] = {}
        for pair in _role_pairs(root):
            roles.setdefault(pair.account_id, []).append(pair)
        duration_values = _attribute_values(root, SESSION_DURATION_ATTRIBUTE)
        try:
            duration = int(duration_values[0]) if duration_values else None
        except ValueError as exc:
            raise BrokerError("ADFS_UNAVAILABLE", "SAML SessionDuration is not an integer") from exc
        return cls(saml_b64, expires_at, duration, roles)

    def find_role(self, account_id: str, role_name: str) -> RolePair | None:
        return next((p for p in self.roles.get(account_id, []) if p.role_name == role_name), None)

    def accounts_with_role(self, role_name: str) -> list[str]:
        return sorted(a for a in self.roles if self.find_role(a, role_name) is not None)


def _attribute_values(root: Element, name: str) -> list[str]:
    values: list[str] = []
    for attribute in root.iter(f"{{{NS_ASSERTION}}}Attribute"):
        if attribute.get("Name") == name:
            values.extend(
                (v.text or "").strip() for v in attribute.iter(f"{{{NS_ASSERTION}}}AttributeValue")
            )
    return values


def _role_pairs(root: Element) -> list[RolePair]:
    pairs = []
    for value in _attribute_values(root, ROLE_ATTRIBUTE):
        parts = [p.strip() for p in value.split(",")]
        role = next((p for p in parts if ":role/" in p), None)
        principal = next((p for p in parts if ":saml-provider/" in p), None)
        if role and principal:
            pairs.append(RolePair(role, principal))
    return pairs


def _expiry(root: Element) -> datetime | None:
    for tag in ("Conditions", "SubjectConfirmationData"):
        for element in root.iter(f"{{{NS_ASSERTION}}}{tag}"):
            value = element.get("NotOnOrAfter")
            if value:
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError as exc:
                    raise BrokerError("ADFS_UNAVAILABLE", "SAML NotOnOrAfter is invalid") from exc
                # SAML times are UTC; treat a missing offset as UTC instead of naive.
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
