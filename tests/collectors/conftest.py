from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from inventory.aws import ClientFactory, EndpointResolver
from inventory.collectors.base import Collector, CollectorContext
from inventory.config import get_common_settings
from inventory.models import KeepExisting, ResourceRecord
from tests.conftest import FROZEN_NOW
from tests.helpers import load_fixture

ACCOUNT = "123456789012"  # moto's default account
REGION = "ap-east-1"


def _tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def seed_ec2(ec2: Any) -> dict[str, str]:
    """Create the resources described in collectors/ec2/seed.json; returns key -> id."""
    seed = load_fixture("collectors/ec2/seed.json")
    ids: dict[str, str] = {}
    for vpc in seed["vpcs"]:
        created = ec2.create_vpc(
            CidrBlock=vpc["cidr"],
            TagSpecifications=[{"ResourceType": "vpc", "Tags": _tags(vpc["tags"])}],
        )
        ids[vpc["key"]] = created["Vpc"]["VpcId"]
    for subnet in seed["subnets"]:
        created = ec2.create_subnet(
            VpcId=ids[subnet["vpc"]],
            CidrBlock=subnet["cidr"],
            TagSpecifications=[{"ResourceType": "subnet", "Tags": _tags(subnet["tags"])}],
        )
        ids[subnet["key"]] = created["Subnet"]["SubnetId"]
    for group in seed["security_groups"]:
        created = ec2.create_security_group(
            GroupName=group["name"],
            Description=group["description"],
            VpcId=ids[group["vpc"]],
            TagSpecifications=[{"ResourceType": "security-group", "Tags": _tags(group["tags"])}],
        )
        ids[group["key"]] = created["GroupId"]
    public_image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    for instance in seed["instances"]:
        created = ec2.run_instances(
            ImageId=public_image,
            MinCount=1,
            MaxCount=1,
            SubnetId=ids[instance["subnet"]],
            SecurityGroupIds=[ids[g] for g in instance["security_groups"]],
            TagSpecifications=[{"ResourceType": "instance", "Tags": _tags(instance["tags"])}],
        )
        ids[instance["key"]] = created["Instances"][0]["InstanceId"]
        if instance.get("terminate"):
            ec2.terminate_instances(InstanceIds=[ids[instance["key"]]])
    for image in seed["images"]:
        created = ec2.create_image(InstanceId=ids[image["instance"]], Name=image["name"])
        ids[image["key"]] = created["ImageId"]
        ec2.create_tags(Resources=[created["ImageId"]], Tags=_tags(image["tags"]))
    return ids


@pytest.fixture
def aws_region(settings_env: dict[str, str]) -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def make_context() -> Callable[..., CollectorContext]:
    def build(
        region: str = REGION, known_ids: frozenset[str] = frozenset(), **kw: Any
    ) -> CollectorContext:
        resolver = EndpointResolver(get_common_settings())
        return CollectorContext(
            account_id=ACCOUNT,
            region=region,
            api_region=resolver.api_region(region),
            session=boto3.Session(region_name=resolver.api_region(region)),
            clients=ClientFactory(resolver),
            known_ids=known_ids,
            **kw,
        )

    return build


def collect(
    collector: Collector, upstream: dict[str, list[ResourceRecord]] | None = None
) -> list[ResourceRecord]:
    records = []
    for item in collector.list_resources(upstream or {}):
        assert not isinstance(item, KeepExisting)
        records.append(collector.to_record(item, FROZEN_NOW))
    return sorted(records, key=lambda r: (r.name or "", r.key.resource_id))


def project(record: ResourceRecord) -> dict[str, Any]:
    """Stable view of a record (moto generates random ids)."""
    return {
        "name": record.name,
        "tags": dict(record.tags),
        "relation_keys": sorted(record.relations),
    }
