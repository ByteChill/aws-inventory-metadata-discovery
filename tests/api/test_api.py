from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from inventory.api.app import create_app
from inventory.bootstrap import Runtime
from inventory.db import AccountRegionRepository, AccountRepository, JobRepository, scan_task
from inventory.models import DirectoryAccount
from inventory.observability import ContextFilter
from tests.api.conftest import app_settings, new_async_engine, running_worker, without_request_id
from tests.conftest import FROZEN_NOW
from tests.helpers import assert_matches_fixture, load_fixture
from tests.planner.conftest import drain, make_runtime

pytestmark = pytest.mark.integration
VPC_A = "arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0a"


# --------------------------------------------------------------------------- request id & errors


def test_request_id_is_echoed_or_generated(client: TestClient) -> None:
    passed = client.get("/healthz", headers={"X-Request-ID": "client-req.1"})
    replaced = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    generated = client.get("/healthz")

    assert passed.headers["x-request-id"] == "client-req.1"
    assert replaced.headers["x-request-id"] != "bad id with spaces"
    assert len(generated.headers["x-request-id"]) == 26  # ULID


def test_error_bodies_carry_the_request_id(client: TestClient) -> None:
    response = client.get(
        "/v1/resources/by-arn", params={"arn": VPC_A}, headers={"X-Request-ID": "r-404"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == "r-404"
    assert without_request_id(response.json()) == load_fixture("api/errors.not_found.response.json")


def test_unhandled_errors_become_500_with_request_id(app: FastAPI) -> None:
    async def boom() -> None:
        raise RuntimeError("secret internals")

    app.add_api_route("/boom", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "r-500"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "r-500"
    assert response.json()["error"]["request_id"] == "r-500"
    assert without_request_id(response.json()) == load_fixture("api/errors.internal.response.json")


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    response = client.post(
        "/v1/resources/refresh", json=load_fixture("api/refresh.invalid.request.json")
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "not-an-arn" not in json.dumps(body["details"]).replace(body["message"], "")
    assert all("input" not in d for d in body["details"])


def test_history_is_reserved(client: TestClient) -> None:
    response = client.get("/v1/history/changes")
    assert response.status_code == 501
    assert without_request_id(response.json()) == load_fixture(
        "api/errors.not_implemented.response.json"
    )


def test_access_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="inventory.api.access")
    caplog.handler.addFilter(ContextFilter())

    client.get("/healthz")
    client.get("/v1/resources", headers={"X-Request-ID": "r-log"})

    (record,) = [r for r in caplog.records if r.name == "inventory.api.access"]
    assert record.route == "/v1/resources"  # type: ignore[attr-defined]
    assert record.status == 200  # type: ignore[attr-defined]
    assert record.request_id == "r-log"  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, int)  # type: ignore[attr-defined]


def test_concurrent_requests_keep_their_own_request_id(app: FastAPI) -> None:
    async def run() -> list[str]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                responses = await asyncio.gather(
                    *(
                        http.get(
                            "/v1/resources/by-arn",
                            params={"arn": f"{VPC_A}{i}"},
                            headers={"X-Request-ID": f"r{i}"},
                        )
                        for i in range(10)
                    )
                )
        return [r.json()["error"]["request_id"] for r in responses]

    assert asyncio.run(run()) == [f"r{i}" for i in range(10)]


# --------------------------------------------------------------------------- queries


@pytest.mark.usefixtures("seeded")
def test_list_filters_and_views(client: TestClient) -> None:
    summary = client.get("/v1/resources", params={"resource_type": "AWS::EC2::VPC"}).json()
    full = client.get("/v1/resources", params={"tag": "Env=prod", "view": "full"}).json()
    has_tag = client.get("/v1/resources", params={"tag": "Name", "with_total": "true"}).json()
    prefix = client.get("/v1/resources", params={"name_prefix": "gold"}).json()

    assert {r["resource_id"] for r in summary["items"]} == {"vpc-0a", "vpc-0b"}
    assert all(r["data"] is None for r in summary["items"])
    assert [r["resource_id"] for r in full["items"]] == ["vpc-0a"]
    assert full["items"][0]["data"]["CidrBlock"] == "10.0.0.0/16"
    assert full["items"][0]["verified_at"] is not None
    assert has_tag["total"] == 2
    assert [r["resource_id"] for r in prefix["items"]] == ["ami-0x"]


@pytest.mark.usefixtures("seeded")
def test_pagination_walks_every_row_once(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 1, "sort": "resource_id", "order": "asc"} | (
            {"cursor": cursor} if cursor else {}
        )
        page = client.get("/v1/resources", params=params).json()
        seen.extend(r["resource_id"] for r in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert seen == ["ami-0x", "vpc-0a", "vpc-0b"]
    first = client.get("/v1/resources", params={"limit": 1}).json()
    other_filters = client.get(
        "/v1/resources", params={"limit": 1, "cursor": first["next_cursor"], "region": "eu-west-1"}
    )
    assert other_filters.status_code == 400


@pytest.mark.usefixtures("seeded")
def test_search_with_jsonpath(client: TestClient) -> None:
    ok = client.post("/v1/resources/search", json=load_fixture("api/search.jsonpath.request.json"))
    bad = client.post(
        "/v1/resources/search", json=load_fixture("api/search.bad_jsonpath.request.json")
    )

    assert [r["resource_id"] for r in ok.json()["items"]] == ["vpc-0a"]
    assert bad.status_code == 422


@pytest.mark.usefixtures("seeded")
def test_by_arn(client: TestClient) -> None:
    vpc = client.get("/v1/resources/by-arn", params={"arn": VPC_A}).json()
    ami = client.get(
        "/v1/resources/by-arn", params={"arn": "arn:aws:ec2:us-east-1::image/ami-0x"}
    ).json()

    assert vpc["resource_id"] == "vpc-0a" and vpc["data"]["VpcId"] == "vpc-0a"
    assert ami["account_id"] == "210987654321"


@pytest.mark.usefixtures("seeded")
def test_resource_path_forms(client: TestClient) -> None:
    for case in load_fixture("api/resources.paths.cases.json"):
        response = client.get(case["path"], params=case.get("params"))
        body = response.json()
        assert response.status_code == case["status"], case["path"]
        if "ids" in case:
            assert sorted(r["resource_id"] for r in body["items"]) == case["ids"], case["path"]
        elif "id" in case:
            assert body["resource_id"] == case["id"] and body["data"] is not None
        else:
            assert body["error"]["code"] == case["code"], case["path"]


@pytest.mark.usefixtures("seeded")
def test_resource_path_forms_page_with_cursor(client: TestClient) -> None:
    first = client.get("/v1/resources/all", params={"limit": 2}).json()
    second = client.get(
        "/v1/resources/all", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    elsewhere = client.get(
        "/v1/resources/123456789012", params={"limit": 2, "cursor": first["next_cursor"]}
    )

    ids = [r["resource_id"] for r in first["items"] + second["items"]]
    assert sorted(ids) == ["ami-0x", "vpc-0a", "vpc-0b"] and second["next_cursor"] is None
    assert elsewhere.status_code == 400  # a cursor is bound to the filters it was issued for


@pytest.mark.usefixtures("seeded")
def test_stats_paths_agree(client: TestClient) -> None:
    fast = client.get("/v1/resources/stats", params={"group_by": "account_id,resource_type"}).json()
    counted = client.get(
        "/v1/resources/stats",
        params={"group_by": "account_id,resource_type", "include_deleted": "true"},
    ).json()
    bad = client.get("/v1/resources/stats", params={"group_by": "name"})

    assert fast == counted
    assert fast["total"] == 3
    assert bad.status_code == 422


@pytest.mark.usefixtures("seeded")
def test_coverage_and_resource_types(client: TestClient) -> None:
    client.post("/v1/scans", json=load_fixture("api/scan.account.request.json"))  # caches regions
    coverage = client.get("/v1/accounts/123456789012/coverage").json()
    types = client.get("/v1/resource-types").json()["items"]

    statuses = {(s["region"], s["resource_type"]): s["status"] for s in coverage["scopes"]}
    assert statuses[("eu-west-1", "AWS::EC2::VPC")] == "succeeded"
    assert statuses[("eu-west-1", "AWS::EC2::Instance")] == "never"
    assert coverage["enabled_regions"] == ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]
    vpc = next(t for t in types if t["resource_type"] == "AWS::EC2::VPC")
    assert vpc["enabled_regions"] == ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"]


@pytest.mark.usefixtures("seeded")
def test_eim_and_account_type_dimension(client: TestClient, db: Engine) -> None:
    accounts = [DirectoryAccount(**a) for a in load_fixture("api/accounts.seed.json")]
    with db.begin() as conn:
        AccountRepository().upsert(conn, accounts, FROZEN_NOW)

    def ids(response: Any) -> list[str]:
        return sorted(r["resource_id"] for r in response.json()["items"])

    by_eim = client.get("/v1/resources/stats", params={"group_by": "eim"}).json()
    counted = client.get(
        "/v1/resources/stats",
        params={"group_by": "eim,account_type", "include_deleted": "true"},
    ).json()
    vpc = client.get("/v1/resources/by-arn", params={"arn": VPC_A}).json()
    search = client.post("/v1/resources/search", json={"account_types": ["prod", "dev"]})
    actual = {
        "by_eim": by_eim["groups"],
        "by_eim_counted": counted["groups"],
        "eim_20142209": ids(client.get("/v1/resources", params={"eim": "20142209"})),
        "prod": ids(client.get("/v1/resources/all", params={"account_type": "prod"})),
        "search_prod_or_dev": ids(search),
        "vpc_account": {k: vpc[k] for k in ("account_name", "eim", "account_type")},
        "accounts": client.get("/v1/accounts").json()["items"],
    }
    coverage = client.get("/v1/accounts/123456789012/coverage").json()

    assert actual == load_fixture("api/accounts.eim.expected.json")
    assert coverage["account"]["name"] == "payments-dev"
    assert client.get("/v1/resources", params={"account_type": "qa"}).status_code == 422


# --------------------------------------------------------------------------- scans


def test_scan_is_queued_and_deduplicated(client: TestClient) -> None:
    body = load_fixture("api/scan.account_region.request.json")

    first = client.post("/v1/scans", json=body, headers={"X-Request-ID": "r-scan"})
    second = client.post("/v1/scans", json=body)
    fetched = client.get(
        f"/v1/scans/{first.json()['job_id']}", params={"include_tasks": "true"}
    ).json()
    listed = client.get("/v1/scans", params={"trigger": "api"}).json()

    assert first.status_code == 202
    assert first.json()["request_id"] == "r-scan" and not first.json()["deduplicated"]
    assert second.json()["deduplicated"] and second.json()["job_id"] == first.json()["job_id"]
    (message,) = drain("eu-west-1", "inventory-local-ondemand")
    assert message["MessageAttributes"]["request_id"]["StringValue"] == "r-scan"
    assert fetched["tasks"][0]["region"] == "eu-west-1"
    assert fetched["stats"]["tasks_total"] == 1
    assert [j["job_id"] for j in listed["items"]] == [first.json()["job_id"]]


def test_task_failure_reason_is_exposed(client: TestClient, db: Any) -> None:
    job_id = client.post(
        "/v1/scans", json=load_fixture("api/scan.account_region.request.json")
    ).json()["job_id"]
    seed = load_fixture("api/scan.task_failed.seed.json")
    with db.begin() as conn:
        task_id = conn.execute(
            select(scan_task.c.task_id).where(scan_task.c.job_id == job_id)
        ).scalar_one()
        JobRepository().finish_task(
            conn,
            task_id,
            seed["status"],
            error_code=seed["error_code"],
            error_message=seed["error_message"],
        )

    (task,) = client.get(f"/v1/scans/{job_id}", params={"include_tasks": "true"}).json()["tasks"]
    picked = {k: task[k] for k in ("status", "error_code", "error_message", "result")}
    assert_matches_fixture(picked, "api/scan.task_failed.expected.json")


@pytest.mark.parametrize("name", ["scan.invalid_region", "scan.missing_account"])
def test_invalid_scopes(client: TestClient, name: str) -> None:
    response = client.post("/v1/scans", json=load_fixture(f"api/{name}.request.json"))
    assert response.status_code == 422


def seed_enabled_regions(db: Any, name: str) -> None:
    seed = load_fixture(f"api/{name}.seed.json")
    with db.begin() as conn:
        AccountRegionRepository().replace(
            conn,
            seed["account_id"],
            enabled=seed["enabled"],
            supported=seed["supported"],
            checked_at=datetime.now(UTC),
        )


def test_type_scope_without_region_uses_enabled_regions(client: TestClient, db: Any) -> None:
    seed_enabled_regions(db, "scan.unscannable")  # eu-west-2 is supported but not enabled

    response = client.post(
        "/v1/scans", json=load_fixture("api/scan.type_without_region.request.json")
    )
    job = client.get(
        f"/v1/scans/{response.json()['job_id']}", params={"include_tasks": "true"}
    ).json()

    tasks = sorted(
        ({"region": t["region"], "resource_types": t["resource_types"]} for t in job["tasks"]),
        key=lambda t: t["region"],
    )
    assert_matches_fixture(
        {"status_code": response.status_code, "tasks": tasks},
        "api/scan.type_without_region.expected.json",
    )


def test_unscannable_scopes_are_rejected_without_a_job(client: TestClient, db: Any) -> None:
    seed_enabled_regions(db, "scan.unscannable")

    messages = []
    for body in load_fixture("api/scan.unscannable.request.json"):
        response = client.post("/v1/scans", json=body)
        assert response.status_code == 422, body
        messages.append(response.json()["error"]["message"])

    assert_matches_fixture(messages, "api/scan.unscannable.expected.json")
    assert client.get("/v1/scans").json()["items"] == []


def test_unknown_job(client: TestClient) -> None:
    assert client.get("/v1/scans/01NOPE").status_code == 404


# --------------------------------------------------------------------------- refresh


def test_refresh_create_update_delete(
    client: TestClient, runtime: Runtime, ec2_ids: dict[str, str]
) -> None:
    import boto3

    arn = f"arn:aws:ec2:ap-east-1:123456789012:instance/{ec2_ids['web-01']}"
    ec2 = boto3.client("ec2", region_name="ap-east-1")
    with running_worker(runtime, "ap-east-1"):
        created = client.post("/v1/resources/refresh", json={"arns": [arn]})
        ec2.create_tags(Resources=[ec2_ids["web-01"]], Tags=[{"Key": "Owner", "Value": "team-a"}])
        updated = client.post("/v1/resources/refresh", json={"arns": [arn]})
        unchanged = client.post("/v1/resources/refresh", json={"arns": [arn]})
        ec2.terminate_instances(InstanceIds=[ec2_ids["web-01"]])
        deleted = client.post("/v1/resources/refresh", json={"arns": [arn]})
        missing = client.post(
            "/v1/resources/refresh",
            json={"arns": ["arn:aws:ec2:ap-east-1:123456789012:instance/i-0123456789abcdef0"]},
        )

    assert created.status_code == 200
    assert [
        r.json()["results"][0]["status"] for r in (created, updated, unchanged, deleted, missing)
    ] == [
        "created",
        "updated",
        "unchanged",
        "deleted",
        "not_found",
    ]
    assert updated.json()["results"][0]["resource"]["tags"]["Owner"] == "team-a"
    assert created.json()["job"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("refresh.unsupported", "unsupported_type", None),
        ("refresh.unresolved_ami", "error", "ACCOUNT_UNRESOLVED"),
    ],
)
def test_refresh_unresolvable_arns(
    client: TestClient, name: str, status: str, code: str | None
) -> None:
    result = client.post(
        "/v1/resources/refresh", json=load_fixture(f"api/{name}.request.json")
    ).json()["results"][0]

    assert result["status"] == status
    assert (result["error"] or {}).get("code") == code


def test_refresh_is_queued_to_the_resource_region(client: TestClient) -> None:
    arn = "arn:aws:ec2:eu-west-1:123456789012:vpc/vpc-0123456789abcdef0"

    response = client.post("/v1/resources/refresh", json={"arns": [arn], "wait_seconds": 0.1})

    assert response.status_code == 202
    assert response.json()["results"][0]["status"] == "pending"
    (message,) = drain("eu-west-1", "inventory-local-ondemand")
    assert json.loads(message["Body"])["resource_ids"] == ["vpc-0123456789abcdef0"]


def test_many_arns_are_queued(client: TestClient) -> None:
    arns = [f"arn:aws:ec2:ap-east-1:123456789012:vpc/vpc-{i:017x}" for i in range(21)]

    response = client.post("/v1/resources/refresh", json={"arns": arns, "wait_seconds": 0})

    assert response.status_code == 202
    assert len(drain("ap-east-1", "inventory-local-ondemand")) == 21


# --------------------------------------------------------------------------- contract


def test_openapi_snapshot(client: TestClient) -> None:
    assert_matches_fixture(client.get("/openapi.json").json(), "api/openapi.json")


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize("cursor", load_fixture("api/cursor.tampered.cases.json"))
def test_tampered_cursor_is_a_400(client: TestClient, cursor: str) -> None:
    response = client.get("/v1/resources", params={"cursor": cursor})
    assert response.status_code == 400


@pytest.mark.usefixtures("seeded")
def test_cursor_with_wrong_keys_is_a_400(client: TestClient) -> None:
    import base64

    first = client.get("/v1/resources", params={"limit": 1}).json()["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
    payload["k"] = payload["k"][:2]
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    assert client.get("/v1/resources", params={"limit": 1, "cursor": forged}).status_code == 400


def test_scope_validation_message(client: TestClient) -> None:
    response = client.post("/v1/scans", json=load_fixture("api/scan.missing_account.request.json"))
    expected = load_fixture("api/scan.missing_account.response.json")["error"]

    body = response.json()["error"]
    assert (body["code"], body["message"]) == (expected["code"], expected["message"])
    assert body["details"][0]["loc"][:2] == ["body", "scope"]


def test_account_without_enabled_scan_regions_still_scans_global(
    client: TestClient, db: Any
) -> None:
    with db.begin() as conn:  # the account has none of the scan regions enabled
        AccountRegionRepository().replace(
            conn,
            "123456789012",
            enabled=[],
            supported=["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"],
            checked_at=datetime.now(UTC),
        )

    response = client.post("/v1/scans", json={"scope": {"account_id": "123456789012"}})
    job = client.get(
        f"/v1/scans/{response.json()['job_id']}", params={"include_tasks": "true"}
    ).json()

    assert response.status_code == 202
    assert [t["region"] for t in job["tasks"]] == ["global"]


@pytest.mark.usefixtures("seeded")
def test_ambiguous_account_for_arn_without_account(client: TestClient, db: Any) -> None:
    from inventory.db import ResourceRepository
    from tests.db.test_repository import rec

    copy = rec("ami_x")
    rid = "ami-0abc1234"
    copies = [
        type(copy)(
            key=type(copy.key)(copy.key.resource_type, account, copy.key.region, rid),
            arn=f"arn:aws:ec2:us-east-1::image/{rid}",
            name=copy.name,
            tags=copy.tags,
            relations=copy.relations,
            data=copy.data,
            observed_at=copy.observed_at,
            content_hash=copy.content_hash,
        )
        for account in ("123456789012", "210987654321")
    ]
    with db.begin() as conn:
        ResourceRepository().upsert(conn, copies, "seed")

    result = client.post(
        "/v1/resources/refresh",
        json={"arns": [f"arn:aws:ec2:us-east-1::image/{rid}"], "wait_seconds": 1},
    ).json()["results"][0]

    assert (result["status"], result["error"]["code"]) == ("error", "ACCOUNT_AMBIGUOUS")


def test_refresh_errors_use_contract_codes(
    db: Any, moto_env: None, ec2_ids: dict[str, str]
) -> None:
    from tests.engine.fakes import FakeCredentials

    denied = make_runtime(db, FakeCredentials(denied=True))
    app = create_app(app_settings(), runtime=denied, async_engine=new_async_engine())
    arn = f"arn:aws:ec2:ap-east-1:123456789012:instance/{ec2_ids['web-01']}"
    with TestClient(app) as client, running_worker(denied, "ap-east-1"):
        result = client.post("/v1/resources/refresh", json={"arns": [arn]}).json()["results"][0]

    assert (result["status"], result["error"]["code"]) == ("error", "ACCOUNT_ACCESS_DENIED")


@pytest.mark.usefixtures("seeded")
def test_tag_key_filter_with_quotes(client: TestClient) -> None:
    response = client.get("/v1/resources", params={"tag": 'we"ird'})
    assert response.status_code == 200 and response.json()["items"] == []


@pytest.mark.parametrize(
    "case", load_fixture("api/refresh.status_from_row.cases.json"), ids=lambda c: c["name"]
)
def test_status_from_row(case: dict[str, Any]) -> None:
    from datetime import datetime

    from inventory.api.refresh import _status_from_row
    from inventory.models import ScanTask

    task = ScanTask.model_validate(
        {
            "task_id": "t",
            "job_id": "j",
            "account_id": "123456789012",
            "region": "eu-west-1",
            "queue": "ondemand",
            "status": "succeeded",
            "queued_at": "2026-10-27T08:00:00+00:00",
            "started_at": "2026-10-27T08:00:01+00:00",
            "updated_at": "2026-10-27T08:00:09+00:00",
        }
    )
    row = (
        {k: datetime.fromisoformat(v) if v else None for k, v in case["row"].items()}
        if case["row"]
        else None
    )
    assert _status_from_row(row, task) == case["expected"]
