from __future__ import annotations

import base64
from dataclasses import asdict

import pytest

from inventory.broker import BrokerError
from inventory.broker.saml import SamlAssertion
from tests.broker.conftest import saml_b64
from tests.helpers import load_fixture


def test_parses_multi_role_assertion() -> None:
    expected = load_fixture("broker/saml/assertion_multi_role.expected.json")

    assertion = SamlAssertion.parse(saml_b64("broker/saml/assertion_multi_role.xml"))

    assert assertion.expires_at.isoformat() == expected["expires_at"]
    assert assertion.session_duration == expected["session_duration"]
    assert {a: [asdict(p) for p in pairs] for a, pairs in assertion.roles.items()} == (
        expected["roles"]
    )
    for role, accounts in expected["accounts_with_role"].items():
        assert assertion.accounts_with_role(role) == accounts


def test_role_and_principal_order_does_not_matter() -> None:
    assertion = SamlAssertion.parse(saml_b64("broker/saml/assertion_multi_role.xml"))

    pair = assertion.find_role("210987654321", "ReadOnly")

    assert pair is not None
    assert pair.role_arn.endswith(":role/ReadOnly")
    assert pair.principal_arn.endswith(":saml-provider/ADFS")
    assert assertion.find_role("111122223333", "ReadOnly") is None


def test_missing_session_duration_is_none() -> None:
    assertion = SamlAssertion.parse(saml_b64("broker/saml/assertion_no_duration.xml"))
    assert assertion.session_duration is None


@pytest.mark.parametrize(
    "raw",
    [
        "%%%not-base64%%%",
        base64.b64encode(load_fixture("broker/saml/not_xml.txt").encode()).decode(),
    ],
)
def test_garbage_is_rejected(raw: str) -> None:
    with pytest.raises(BrokerError) as info:
        SamlAssertion.parse(raw)
    assert info.value.code == "ADFS_UNAVAILABLE"
    assert info.value.retryable


def test_repr_does_not_leak_raw_assertion() -> None:
    assertion = SamlAssertion.parse(saml_b64("broker/saml/assertion_single_role.xml"))
    assert assertion.raw_b64 not in repr(assertion)
