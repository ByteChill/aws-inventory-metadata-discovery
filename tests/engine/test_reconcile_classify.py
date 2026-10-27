from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from inventory.db import IndexEntry
from inventory.engine import classify, reconcile
from inventory.errors import ThrottledError
from inventory.models import ResourceKey, ResourceRecord, TypeScope
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture

SCOPE = TypeScope("Test::Thing", "123456789012", "ap-east-1")


def record(rid: str, hash_hex: str) -> ResourceRecord:
    return ResourceRecord(
        key=ResourceKey("Test::Thing", "123456789012", "ap-east-1", rid),
        arn=f"arn:aws:test:ap-east-1:123456789012:thing/{rid}",
        name=None,
        tags={},
        relations={},
        data={"Id": rid},
        observed_at=FROZEN_NOW,
        content_hash=bytes.fromhex(hash_hex),
    )


@pytest.mark.parametrize(
    "case", load_fixture("engine/reconcile_cases.json"), ids=lambda c: c["name"]
)
def test_reconcile(case: dict[str, Any]) -> None:
    index = {
        rid: IndexEntry(
            bytes.fromhex(e["hash"]), FROZEN_NOW, e["deleted"], f"arn:aws:test:::thing/{rid}"
        )
        for rid, e in case["index"].items()
    }
    current = [record(c["id"], c["hash"]) for c in case["current"]]

    changes = reconcile(SCOPE, index, current, set(case["kept"]), FROZEN_NOW, "job-1")

    expected = case["expected"]
    assert (changes.created, changes.updated, changes.unchanged) == (
        expected["created"],
        expected["updated"],
        expected["unchanged"],
    )
    assert changes.deleted_ids == expected["deleted_ids"]
    assert [r.key.resource_id for r in changes.upserts] == expected["upsert_ids"]
    assert len(changes.events) == len(expected["upsert_ids"]) + len(expected["deleted_ids"])
    deleted_events = [e for e in changes.events if e.change_type == "deleted"]
    assert all(e.arn.endswith(e.key.resource_id) for e in deleted_events)


def test_duplicate_ids_are_a_collector_bug() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        reconcile(SCOPE, {}, [record("r-1", "aa"), record("r-1", "bb")], set(), FROZEN_NOW, "j")


def test_keep_existing_for_unknown_id_is_a_collector_bug() -> None:
    with pytest.raises(ValueError, match="KeepExisting"):
        reconcile(SCOPE, {}, [], {"r-9"}, FROZEN_NOW, "job-1")


@pytest.mark.parametrize("case", load_fixture("engine/client_errors.json"), ids=lambda c: c["code"])
def test_classify_client_errors(case: dict[str, Any]) -> None:
    error = ClientError(
        {
            "Error": {"Code": case["code"], "Message": "m"},
            "ResponseMetadata": {"HTTPStatusCode": case["status"]},
        },
        "DescribeThings",
    )

    info = classify(error)

    assert (info.code, info.retryable) == (case["expect"], case["retryable"])


def test_classify_other_errors() -> None:
    assert classify(EndpointConnectionError(endpoint_url="https://x")).code == "NETWORK_ERROR"
    assert classify(ThrottledError("slow down")).retryable
    internal = classify(RuntimeError("boom"))
    assert (internal.code, internal.retryable) == ("INTERNAL_ERROR", True)
