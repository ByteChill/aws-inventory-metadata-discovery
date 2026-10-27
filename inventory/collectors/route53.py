"""Route 53 hosted zone collector (global). Record sets are out of scope for v1."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from botocore.exceptions import ClientError

from inventory.collectors.base import Collector, Raw, error_code, register
from inventory.models import KeepExisting, ResourceRecord

TAGS_BATCH = 10


def zone_id(raw_id: str) -> str:
    return raw_id.rsplit("/", 1)[-1]


@register
class HostedZoneCollector(Collector):
    """The ARN has no account; refresh resolves it from the inventory."""

    resource_type = "AWS::Route53::HostedZone"
    service = "route53"
    is_global = True
    arn_pattern = re.compile(r"^arn:aws:route53:::hostedzone/(?P<id>[A-Z0-9]+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        zones = [
            zone
            for page in self.client.get_paginator("list_hosted_zones").paginate()
            for zone in page.get("HostedZones", [])
        ]
        tags: dict[str, list[Any]] = {}
        ids = [zone_id(z["Id"]) for z in zones]
        for start in range(0, len(ids), TAGS_BATCH):
            response = self.client.list_tags_for_resources(
                ResourceType="hostedzone", ResourceIds=ids[start : start + TAGS_BATCH]
            )
            for tag_set in response.get("ResourceTagSets", []):
                tags[tag_set["ResourceId"]] = tag_set.get("Tags", [])
        # sequential: Route 53 allows ~5 requests/second per account across all its APIs
        for zone in zones:
            vpcs = self._vpcs(zone) if _is_private(zone) else []
            yield self._view(zone, vpcs, tags.get(zone_id(zone["Id"]), []))

    def get_resource(self, resource_id: str) -> Raw | None:
        try:
            response = self.client.get_hosted_zone(Id=resource_id)
            tag_set = self.client.list_tags_for_resource(
                ResourceType="hostedzone", ResourceId=resource_id
            )
        except ClientError as exc:
            if error_code(exc) == "NoSuchHostedZone":
                return None
            raise
        zone = response["HostedZone"]
        vpcs = response.get("VPCs", []) if _is_private(zone) else []
        return self._view(zone, vpcs, tag_set.get("ResourceTagSet", {}).get("Tags", []))

    def _vpcs(self, zone: Mapping[str, Any]) -> list[Any]:
        return list(self.client.get_hosted_zone(Id=zone_id(zone["Id"])).get("VPCs", []))

    @staticmethod
    def _view(zone: Mapping[str, Any], vpcs: list[Any], tags: list[Any]) -> Raw:
        # ResourceRecordSetCount changes with every record (external-dns, ACM validation)
        hosted_zone = {k: v for k, v in zone.items() if k != "ResourceRecordSetCount"}
        return {
            "HostedZone": hosted_zone,
            "VPCs": sorted(vpcs, key=lambda v: v.get("VPCId", "")),
            "Tags": tags,
        }

    def resource_id(self, raw: Raw) -> str:
        return zone_id(str(raw["HostedZone"]["Id"]))

    def build_arn(self, raw: Raw) -> str:
        return f"arn:aws:route53:::hostedzone/{self.resource_id(raw)}"

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["HostedZone"]["Name"]).rstrip(".")

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {"vpc_ids": [v["VPCId"] for v in raw["VPCs"]]}


def _is_private(zone: Mapping[str, Any]) -> bool:
    return bool((zone.get("Config") or {}).get("PrivateZone"))
