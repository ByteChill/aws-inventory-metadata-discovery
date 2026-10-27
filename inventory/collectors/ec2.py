"""EC2 collectors: VPC, Subnet, SecurityGroup, Instance, Image."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from inventory.collectors.base import Ec2DescribeCollector, Raw, register

_ACCOUNT = r"(?P<account>\d{12})"
_REGION = r"(?P<region>[a-z0-9-]+)"


@register
class VpcCollector(Ec2DescribeCollector):
    resource_type = "AWS::EC2::VPC"
    operation = "describe_vpcs"
    result_key = "Vpcs"
    id_field = "VpcId"
    ids_param = "VpcIds"
    arn_resource = "vpc"
    not_found_codes = ("InvalidVpcID.NotFound",)
    arn_pattern = re.compile(rf"^arn:aws:ec2:{_REGION}:{_ACCOUNT}:vpc/(?P<id>vpc-[0-9a-f]+)$")


@register
class SubnetCollector(Ec2DescribeCollector):
    resource_type = "AWS::EC2::Subnet"
    operation = "describe_subnets"
    result_key = "Subnets"
    id_field = "SubnetId"
    ids_param = "SubnetIds"
    arn_resource = "subnet"
    not_found_codes = ("InvalidSubnetID.NotFound",)
    arn_pattern = re.compile(rf"^arn:aws:ec2:{_REGION}:{_ACCOUNT}:subnet/(?P<id>subnet-[0-9a-f]+)$")

    def build_arn(self, raw: Raw) -> str:
        return str(raw.get("SubnetArn") or super().build_arn(raw))

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {"vpc_id": raw.get("VpcId")}


@register
class SecurityGroupCollector(Ec2DescribeCollector):
    resource_type = "AWS::EC2::SecurityGroup"
    operation = "describe_security_groups"
    result_key = "SecurityGroups"
    id_field = "GroupId"
    ids_param = "GroupIds"
    arn_resource = "security-group"
    not_found_codes = ("InvalidGroup.NotFound", "InvalidGroupId.NotFound")
    arn_pattern = re.compile(
        rf"^arn:aws:ec2:{_REGION}:{_ACCOUNT}:security-group/(?P<id>sg-[0-9a-f]+)$"
    )

    def extract_name(self, raw: Raw) -> str | None:
        name = raw.get("GroupName")
        return str(name) if name else super().extract_name(raw)

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {"vpc_id": raw.get("VpcId")}


@register
class InstanceCollector(Ec2DescribeCollector):
    resource_type = "AWS::EC2::Instance"
    operation = "describe_instances"
    result_key = "Reservations"
    id_field = "InstanceId"
    ids_param = "InstanceIds"
    arn_resource = "instance"
    not_found_codes = ("InvalidInstanceID.NotFound",)
    arn_pattern = re.compile(rf"^arn:aws:ec2:{_REGION}:{_ACCOUNT}:instance/(?P<id>i-[0-9a-f]+)$")

    def items(self, page: Mapping[str, Any]) -> list[Raw]:
        return [i for r in page.get("Reservations", []) for i in r.get("Instances", [])]

    def is_live(self, raw: Raw) -> bool:
        return bool(raw.get("State", {}).get("Name") != "terminated")

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {
            "vpc_id": raw.get("VpcId"),
            "subnet_id": raw.get("SubnetId"),
            "security_group_ids": sorted(g["GroupId"] for g in raw.get("SecurityGroups", [])),
            "image_id": raw.get("ImageId"),
        }


@register
class ImageCollector(Ec2DescribeCollector):
    """Only AMIs owned by the account; the ARN has no account part."""

    resource_type = "AWS::EC2::Image"
    operation = "describe_images"
    result_key = "Images"
    id_field = "ImageId"
    ids_param = "ImageIds"
    arn_resource = "image"
    arn_has_account = False
    not_found_codes = ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable")
    extra_params: ClassVar[dict[str, Any]] = {"Owners": ["self"]}
    drop_fields = ("LastLaunchedTime",)  # changes on every launch; not inventory data
    arn_pattern = re.compile(rf"^arn:aws:ec2:{_REGION}::image/(?P<id>ami-[0-9a-f]+)$")

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        snapshots = [
            m["Ebs"]["SnapshotId"]
            for m in raw.get("BlockDeviceMappings", [])
            if m.get("Ebs", {}).get("SnapshotId")
        ]
        return {"snapshot_ids": sorted(snapshots)}
