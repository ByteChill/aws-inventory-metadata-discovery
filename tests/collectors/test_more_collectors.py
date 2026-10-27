"""ECS, S3, IAM and Route 53 collectors against moto."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import boto3
import pytest

from inventory.collectors import registry
from inventory.collectors.base import CollectorContext
from inventory.models import KeepExisting, ResourceRecord
from tests.collectors.conftest import ACCOUNT, collect, project
from tests.conftest import FROZEN_NOW
from tests.helpers import assert_matches_fixture, load_fixture

pytestmark = pytest.mark.usefixtures("aws_region")


def _tags(tags: dict[str, str], lower: bool = False) -> list[dict[str, str]]:
    k, v = ("key", "value") if lower else ("Key", "Value")
    return [{k: key, v: value} for key, value in tags.items()]


# --------------------------------------------------------------------------- seeding


def seed_ecs() -> None:
    seed = load_fixture("collectors/ecs/seed.json")
    ecs = boto3.client("ecs", region_name="ap-east-1")
    for cluster in seed["clusters"]:
        ecs.create_cluster(clusterName=cluster["name"], tags=_tags(cluster["tags"], lower=True))
    for td in seed["task_definitions"]:
        ecs.register_task_definition(
            family=td["family"],
            containerDefinitions=[{"name": "app", "image": td["image"], "memory": 128}],
            tags=_tags(td["tags"], lower=True) or [{"key": "seeded", "value": "true"}],
        )
    for svc in seed["services"]:
        ecs.create_service(
            cluster=svc["cluster"],
            serviceName=svc["name"],
            taskDefinition=svc["task_definition"],
            desiredCount=1,
            tags=_tags(svc["tags"], lower=True) or [{"key": "seeded", "value": "true"}],
        )


def seed_s3() -> None:
    for bucket in load_fixture("collectors/s3/seed.json")["buckets"]:
        s3 = boto3.client("s3", region_name=bucket["region"])
        config = (
            {}
            if bucket["region"] == "us-east-1"
            else {"CreateBucketConfiguration": {"LocationConstraint": bucket["region"]}}
        )
        s3.create_bucket(Bucket=bucket["name"], **config)
        if bucket["tags"]:
            s3.put_bucket_tagging(Bucket=bucket["name"], Tagging={"TagSet": _tags(bucket["tags"])})
        if bucket["versioning"]:
            s3.put_bucket_versioning(
                Bucket=bucket["name"], VersioningConfiguration={"Status": "Enabled"}
            )
        if bucket["encryption"]:
            s3.put_bucket_encryption(
                Bucket=bucket["name"],
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                },
            )


def seed_iam() -> None:
    seed = load_fixture("collectors/iam/seed.json")
    iam = boto3.client("iam", region_name="us-east-1")
    arns = {}
    for policy in seed["policies"]:
        arns[policy["name"]] = iam.create_policy(
            PolicyName=policy["name"],
            Path=policy["path"],
            PolicyDocument=json.dumps(policy["document"]),
            Tags=_tags(policy["tags"]),
        )["Policy"]["Arn"]
    for group in seed["groups"]:
        iam.create_group(GroupName=group)
    for role in seed["roles"]:
        iam.create_role(
            RoleName=role["name"],
            Path=role["path"],
            AssumeRolePolicyDocument=json.dumps(seed["assume_role_policy"]),
            Tags=_tags(role["tags"]),
        )
        for name in role["attach"]:
            iam.attach_role_policy(RoleName=role["name"], PolicyArn=arns[name])
        for name, document in role["inline"].items():
            iam.put_role_policy(
                RoleName=role["name"], PolicyName=name, PolicyDocument=json.dumps(document)
            )
        iam.create_instance_profile(InstanceProfileName=role["instance_profile"])
        iam.add_role_to_instance_profile(
            InstanceProfileName=role["instance_profile"], RoleName=role["name"]
        )
    for user in seed["users"]:
        iam.create_user(UserName=user["name"], Path=user["path"], Tags=_tags(user["tags"]))
        for name in user["attach"]:
            iam.attach_user_policy(UserName=user["name"], PolicyArn=arns[name])
        for group in user["groups"]:
            iam.add_user_to_group(UserName=user["name"], GroupName=group)


def seed_route53() -> dict[str, str]:
    r53 = boto3.client("route53", region_name="us-east-1")
    ids: dict[str, str] = {}
    for i, zone in enumerate(load_fixture("collectors/route53/seed.json")["zones"]):
        params: dict[str, Any] = {"Name": zone["name"], "CallerReference": f"seed-{i}"}
        if zone["private"]:
            vpc = boto3.client("ec2", region_name=zone["vpc_region"]).create_vpc(
                CidrBlock="10.9.0.0/16"
            )
            params |= {
                "VPC": {"VPCRegion": zone["vpc_region"], "VPCId": vpc["Vpc"]["VpcId"]},
                "HostedZoneConfig": {"PrivateZone": True, "Comment": "private"},
            }
        zone_id = r53.create_hosted_zone(**params)["HostedZone"]["Id"].split("/")[-1]
        ids[zone["name"]] = zone_id
        if zone["tags"]:
            r53.change_tags_for_resource(
                ResourceType="hostedzone", ResourceId=zone_id, AddTags=_tags(zone["tags"])
            )
    return ids


SEEDERS: dict[str, tuple[Callable[[], Any], str, str]] = {
    # resource_type: (seeder, region label, fixture folder)
    "AWS::ECS::Cluster": (seed_ecs, "ap-east-1", "ecs"),
    "AWS::ECS::Service": (seed_ecs, "ap-east-1", "ecs"),
    "AWS::ECS::TaskDefinition": (seed_ecs, "ap-east-1", "ecs"),
    "AWS::S3::Bucket": (seed_s3, "ap-east-1", "s3"),
    "AWS::IAM::Role": (seed_iam, "global", "iam"),
    "AWS::IAM::User": (seed_iam, "global", "iam"),
    "AWS::IAM::ManagedPolicy": (seed_iam, "global", "iam"),
    "AWS::Route53::HostedZone": (seed_route53, "global", "route53"),
}


def scan(resource_type: str, ctx: CollectorContext) -> list[ResourceRecord]:
    """Run the type and its dependencies in order, like the engine does."""
    upstream: dict[str, list[ResourceRecord]] = {}
    for group in registry.groups([resource_type]):
        for cls in group:
            upstream[cls.resource_type] = collect(cls(ctx), upstream)
    return upstream[resource_type]


def short(resource_type: str) -> str:
    return resource_type.split("::")[-1].lower()


# --------------------------------------------------------------------------- generic


@pytest.mark.parametrize("resource_type", list(SEEDERS))
def test_list_matches_fixture(
    resource_type: str, make_context: Callable[..., CollectorContext]
) -> None:
    seeder, region, folder = SEEDERS[resource_type]
    seeder()

    records = scan(resource_type, make_context(region=region))

    assert_matches_fixture(
        [project(r) for r in records], f"collectors/{folder}/{short(resource_type)}.expected.json"
    )
    assert {r.key.region for r in records} <= {region}


@pytest.mark.parametrize("resource_type", list(SEEDERS))
def test_get_matches_list_and_arn_round_trips(
    resource_type: str, make_context: Callable[..., CollectorContext]
) -> None:
    seeder, region, _ = SEEDERS[resource_type]
    seeder()
    ctx = make_context(region=region)
    cls = registry.get(resource_type)
    collector = cls(ctx)

    for listed in scan(resource_type, ctx):
        fetched = collector.get_resource(listed.key.resource_id)
        assert fetched is not None, listed.key
        assert collector.to_record(fetched, FROZEN_NOW).content_hash == listed.content_hash, (
            listed.key
        )
        routed = registry.route_arn(listed.arn)
        assert routed is not None and routed[0] is cls
        parsed = routed[1]
        assert parsed.resource_id == listed.key.resource_id or parsed.id_is_suffix
        assert parsed.account_id in (ACCOUNT, None)


@pytest.mark.parametrize("resource_type", list(SEEDERS))
def test_get_missing_returns_none(
    resource_type: str, make_context: Callable[..., CollectorContext]
) -> None:
    seeder, region, folder = SEEDERS[resource_type]
    seeder()
    missing = load_fixture(f"collectors/{folder}/seed.json")["missing_ids"][resource_type]

    assert registry.get(resource_type)(make_context(region=region)).get_resource(missing) is None


# --------------------------------------------------------------------------- type specific


def test_ecs_services_and_in_use_task_definitions(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_ecs()
    ctx = make_context()

    services = {r.key.resource_id: r for r in scan("AWS::ECS::Service", ctx)}
    task_defs = sorted(r.key.resource_id for r in scan("AWS::ECS::TaskDefinition", ctx))

    assert sorted(services) == ["dev/web", "prod/web", "prod/worker"]  # same name in two clusters
    assert services["prod/web"].relations["task_definition_arns"] == [
        f"arn:aws:ecs:ap-east-1:{ACCOUNT}:task-definition/web:2"
    ]
    assert "events" not in services["prod/web"].data
    assert task_defs == ["web:1", "web:2", "worker:1"]  # "unused" is not in use


def test_ecs_known_revisions_are_kept_unless_full_refresh(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_ecs()
    ctx = make_context(known_ids=frozenset({"web:1"}))
    services = scan("AWS::ECS::Service", ctx)
    collector = registry.get("AWS::ECS::TaskDefinition")(ctx)

    incremental = list(collector.list_resources({"AWS::ECS::Service": services}))
    full_ctx = make_context(known_ids=frozenset({"web:1"}), full_refresh=True)
    full = list(
        registry.get("AWS::ECS::TaskDefinition")(full_ctx).list_resources(
            {"AWS::ECS::Service": services}
        )
    )

    assert KeepExisting("web:1") in incremental
    assert sum(isinstance(i, KeepExisting) for i in incremental) == 1
    assert not any(isinstance(i, KeepExisting) for i in full)


def test_ecs_old_service_arn_is_a_suffix() -> None:
    routed = registry.route_arn(f"arn:aws:ecs:ap-east-1:{ACCOUNT}:service/web")
    assert routed is not None
    assert routed[1].id_is_suffix and routed[1].resource_id == "web"


def test_s3_lists_only_the_region_and_reads_settings(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_s3()

    records = {r.key.resource_id: r for r in scan("AWS::S3::Bucket", make_context())}

    assert sorted(records) == ["inventory-test-logs", "inventory-test-plain"]
    logs = records["inventory-test-logs"]
    assert logs.tags == {"Env": "prod"}
    assert logs.data["Versioning"]["Status"] == "Enabled"
    assert (
        logs.data["Encryption"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        == "AES256"
    )
    assert records["inventory-test-plain"].data["Encryption"] is None
    assert logs.arn == "arn:aws:s3:::inventory-test-logs"


def test_iam_views(make_context: Callable[..., CollectorContext]) -> None:
    seed_iam()
    ctx = make_context(region="global")

    role = scan("AWS::IAM::Role", ctx)
    users = scan("AWS::IAM::User", ctx)
    policies = scan("AWS::IAM::ManagedPolicy", ctx)

    (app_role,) = [r for r in role if r.key.resource_id == "app-role"]
    assert app_role.key.region == "global"
    assert app_role.relations["attached_policy_arns"] == [
        f"arn:aws:iam::{ACCOUNT}:policy/team/read-logs"
    ]
    assert "RoleLastUsed" not in app_role.data
    assert [r.relations["group_names"] for r in users if r.key.resource_id == "deployer"] == [
        ["ops"]
    ]
    assert [p.tags for p in policies] == [{"Owner": "team-a"}]
    assert registry.route_arn("arn:aws:iam::aws:policy/ReadOnlyAccess") is None


def test_iam_listing_shares_one_authorization_details_call(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_iam()
    ctx = make_context(region="global")
    calls: list[str] = []
    client = boto3.client("iam", region_name="us-east-1")
    client.meta.events.register(
        "before-call.iam.GetAccountAuthorizationDetails", lambda **_: calls.append("x")
    )
    ctx.clients.client = lambda *a, **k: client  # type: ignore[method-assign]

    for resource_type in ("AWS::IAM::Role", "AWS::IAM::User", "AWS::IAM::ManagedPolicy"):
        scan(resource_type, ctx)

    assert len(calls) == 1


def test_private_zone_lists_its_vpcs(make_context: Callable[..., CollectorContext]) -> None:
    ids = seed_route53()

    zones = {r.name: r for r in scan("AWS::Route53::HostedZone", make_context(region="global"))}

    assert zones["internal.example.com"].relations["vpc_ids"]
    assert zones["example.com"].relations["vpc_ids"] == []
    assert zones["example.com"].tags == {"Env": "prod"}
    assert zones["example.com"].arn == f"arn:aws:route53:::hostedzone/{ids['example.com']}"


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize(
    "case", load_fixture("collectors/ecs/in_use.cases.json"), ids=lambda c: c["name"]
)
def test_task_definitions_in_use(case: dict[str, Any]) -> None:
    from inventory.collectors.ecs import task_definitions_in_use

    assert task_definitions_in_use(case["service"]) == case["expected"]


def test_volatile_fields_are_not_stored(make_context: Callable[..., CollectorContext]) -> None:
    seed_ecs()
    seed_iam()
    seed_route53()
    ctx = make_context()

    service = scan("AWS::ECS::Service", ctx)[0]
    cluster = scan("AWS::ECS::Cluster", ctx)[0]
    zone = scan("AWS::Route53::HostedZone", make_context(region="global"))[0]
    policy = scan("AWS::IAM::ManagedPolicy", make_context(region="global"))[0]

    assert not {"runningCount", "pendingCount", "events"} & set(service.data)
    assert all(
        "runningCount" not in d and "updatedAt" not in d for d in service.data["deployments"]
    )
    assert "runningTasksCount" not in cluster.data
    assert "ResourceRecordSetCount" not in zone.data["HostedZone"]
    assert "AttachmentCount" not in policy.data


def test_deleted_service_does_not_exist(make_context: Callable[..., CollectorContext]) -> None:
    seed_ecs()
    ecs = boto3.client("ecs", region_name="ap-east-1")
    ecs.update_service(cluster="prod", service="worker", desiredCount=0)
    ecs.delete_service(cluster="prod", service="worker")
    ctx = make_context()

    listed = {r.key.resource_id for r in scan("AWS::ECS::Service", ctx)}

    assert "prod/worker" not in listed
    assert registry.get("AWS::ECS::Service")(ctx).get_resource("prod/worker") is None
    assert registry.get("AWS::ECS::Service")(ctx).get_resource("gone/web") is None  # no cluster


def test_service_describe_batches_beyond_ten(make_context: Callable[..., CollectorContext]) -> None:
    ecs = boto3.client("ecs", region_name="ap-east-1")
    ecs.create_cluster(clusterName="big")
    td = ecs.register_task_definition(
        family="many", containerDefinitions=[{"name": "a", "image": "busybox", "memory": 64}]
    )["taskDefinition"]["taskDefinitionArn"]
    for i in range(23):
        ecs.create_service(
            cluster="big", serviceName=f"svc-{i:02d}", taskDefinition=td, desiredCount=1
        )

    services = scan("AWS::ECS::Service", make_context())

    assert len(services) == 23


def test_ecs_non_missing_failures_fail_the_listing(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_ecs()
    collector = registry.get("AWS::ECS::Cluster")(make_context())
    original = collector.client.describe_clusters

    def with_failure(**kwargs: Any) -> Any:
        response = original(**kwargs)
        response["failures"] = [
            {"arn": "arn:aws:ecs:ap-east-1:1:cluster/x", "reason": "ACCESS_DENIED"}
        ]
        return response

    collector.client.describe_clusters = with_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ACCESS_DENIED"):
        list(collector.list_resources({}))


def test_bucket_in_another_region_is_not_found_here(
    make_context: Callable[..., CollectorContext],
) -> None:
    seed_s3()
    collector = registry.get("AWS::S3::Bucket")(make_context())

    assert collector.get_resource("inventory-test-elsewhere") is None


def test_s3_detail_errors_fail_the_listing(make_context: Callable[..., CollectorContext]) -> None:
    from botocore.exceptions import ClientError

    seed_s3()
    collector = registry.get("AWS::S3::Bucket")(make_context())

    def denied(**_: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetBucketTagging"
        )

    collector.client.get_bucket_tagging = denied  # type: ignore[method-assign]
    with pytest.raises(ClientError):
        list(collector.list_resources({}))
