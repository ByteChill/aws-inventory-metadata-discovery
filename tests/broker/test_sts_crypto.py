from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from inventory.broker import BrokerError, SamlExpiredError
from inventory.broker.crypto import EnvelopeCipher
from inventory.broker.saml import SamlAssertion
from inventory.broker.sts import StsAssumer
from tests.broker.conftest import saml_b64
from tests.helpers import load_fixture


def single_role() -> SamlAssertion:
    return SamlAssertion.parse(saml_b64("broker/saml/assertion_single_role.xml"))


def test_assume_with_moto_returns_credentials() -> None:
    assertion = single_role()
    pair = assertion.find_role("123456789012", "ReadOnly")
    assert pair is not None
    with mock_aws():
        sts = boto3.client("sts", region_name="ap-east-1")
        creds = StsAssumer(sts, session_duration_seconds=7200).assume(assertion, pair)

    assert creds.access_key_id.startswith("ASIA")
    assert creds.secret_access_key.get_secret_value()
    assert "**" in repr(creds)


@pytest.mark.parametrize("case", load_fixture("broker/sts_errors.json"))
def test_sts_errors_are_mapped(case: dict[str, str]) -> None:
    assertion = single_role()
    pair = assertion.find_role("123456789012", "ReadOnly")
    assert pair is not None
    sts = boto3.client("sts", region_name="ap-east-1")
    with Stubber(sts) as stub:
        stub.add_client_error("assume_role_with_saml", service_error_code=case["aws_code"])
        assumer = StsAssumer(sts)
        if case["expect"] == "saml_expired":
            with pytest.raises(SamlExpiredError):
                assumer.assume(assertion, pair)
        else:
            with pytest.raises(BrokerError) as info:
                assumer.assume(assertion, pair)
            assert info.value.code == case["expect"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_envelope_round_trip_and_rotation(kms_key: tuple[Any, str]) -> None:
    kms, key_id = kms_key
    clock = Clock()
    cipher = EnvelopeCipher(kms, key_id, data_key_ttl_seconds=60, clock=clock)

    first = cipher.encrypt(b"secret-1", "cred")
    clock.now = 120  # past the data key TTL
    second = cipher.encrypt(b"secret-2", "cred")

    assert first != second
    assert '"ek"' in first
    assert cipher.decrypt(first, "cred") == b"secret-1"
    assert cipher.decrypt(second, "cred") == b"secret-2"
    # a fresh cipher (new Lambda environment) can decrypt through KMS
    assert EnvelopeCipher(kms, key_id).decrypt(first, "cred") == b"secret-1"


def test_envelope_kind_is_authenticated(kms_key: tuple[Any, str]) -> None:
    kms, key_id = kms_key
    envelope = EnvelopeCipher(kms, key_id).encrypt(b"secret", "cred")

    for cipher in (EnvelopeCipher(kms, key_id),):
        with pytest.raises(BrokerError) as info:
            cipher.decrypt(envelope, "saml")
        assert info.value.code == "INTERNAL"


def test_malformed_envelope(kms_key: tuple[Any, str]) -> None:
    kms, key_id = kms_key
    with pytest.raises(BrokerError):
        EnvelopeCipher(kms, key_id).decrypt("not json", "cred")
