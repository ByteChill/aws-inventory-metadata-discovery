from __future__ import annotations

from collections.abc import Callable

import boto3
import pytest

from inventory.collectors import registry
from inventory.collectors.base import CollectorContext
from tests.collectors.conftest import ACCOUNT, REGION, collect, project, seed_ec2
from tests.conftest import FROZEN_NOW
from tests.helpers import assert_matches_fixture, load_fixture

EC2_TYPES = [
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::SecurityGroup",
    "AWS::EC2::Instance",
    "AWS::EC2::Image",
]
pytestmark = pytest.mark.usefixtures("aws_region")


@pytest.fixture
def seeded() -> dict[str, str]:
    return seed_ec2(boto3.client("ec2", region_name=REGION))


def short(resource_type: str) -> str:
    return resource_type.split("::")[-1].lower()


@pytest.mark.parametrize("resource_type", EC2_TYPES)
def test_list_matches_fixture(
    resource_type: str, seeded: dict[str, str], make_context: Callable[..., CollectorContext]
) -> None:
    records = collect(registry.get(resource_type)(make_context()))

    assert_matches_fixture(
        [project(r) for r in records], f"collectors/ec2/{short(resource_type)}.expected.json"
    )


@pytest.mark.parametrize("resource_type", EC2_TYPES)
def test_get_matches_list_structure_and_arn_round_trips(
    resource_type: str, seeded: dict[str, str], make_context: Callable[..., CollectorContext]
) -> None:
    cls = registry.get(resource_type)
    collector = cls(make_context())

    for listed in collect(collector):
        fetched = collector.get_resource(listed.key.resource_id)
        assert fetched is not None
        assert collector.to_record(fetched, FROZEN_NOW).content_hash == listed.content_hash
        parsed = cls.parse_arn(listed.arn)
        assert parsed is not None
        assert parsed.resource_id == listed.key.resource_id
        assert parsed.region == REGION
        assert parsed.account_id == (None if resource_type == "AWS::EC2::Image" else ACCOUNT)
        assert registry.route_arn(listed.arn) == (cls, parsed)


@pytest.mark.parametrize("resource_type", EC2_TYPES)
def test_get_missing_returns_none(
    resource_type: str, make_context: Callable[..., CollectorContext]
) -> None:
    missing = load_fixture("collectors/ec2/seed.json")["missing_ids"][resource_type]
    assert registry.get(resource_type)(make_context()).get_resource(missing) is None


def test_terminated_instances_do_not_exist(
    seeded: dict[str, str], make_context: Callable[..., CollectorContext]
) -> None:
    collector = registry.get("AWS::EC2::Instance")(make_context())

    listed = {r.key.resource_id for r in collect(collector)}

    assert seeded["web-01"] in listed
    assert seeded["old-01"] not in listed
    assert collector.get_resource(seeded["old-01"]) is None


def test_instance_relations(
    seeded: dict[str, str], make_context: Callable[..., CollectorContext]
) -> None:
    collector = registry.get("AWS::EC2::Instance")(make_context())
    raw = collector.get_resource(seeded["web-01"])
    assert raw is not None

    relations = collector.to_record(raw, FROZEN_NOW).relations

    assert relations["vpc_id"] == seeded["main"]
    assert relations["subnet_id"] == seeded["private-a"]
    assert relations["security_group_ids"] == [seeded["web"]]


def test_only_owned_images_are_listed(
    seeded: dict[str, str], make_context: Callable[..., CollectorContext]
) -> None:
    records = collect(registry.get("AWS::EC2::Image")(make_context()))

    assert [r.key.resource_id for r in records] == [seeded["web-ami"]]
    assert records[0].arn == f"arn:aws:ec2:{REGION}::image/{seeded['web-ami']}"
