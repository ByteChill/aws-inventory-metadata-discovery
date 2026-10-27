"""S3 bucket collector (regional: a bucket belongs to the region it lives in)."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from botocore.exceptions import ClientError

from inventory.collectors.base import Collector, Raw, error_code, register
from inventory.models import KeepExisting, ResourceRecord

NO_TAGS = "NoSuchTagSet"
NO_ENCRYPTION = "ServerSideEncryptionConfigurationNotFoundError"
NO_PUBLIC_ACCESS_BLOCK = "NoSuchPublicAccessBlockConfiguration"


@register
class BucketCollector(Collector):
    """The ARN has neither account nor region; refresh resolves them from the inventory."""

    resource_type = "AWS::S3::Bucket"
    service = "s3"
    # legacy us-east-1 bucket names may contain upper case and "_"
    arn_pattern = re.compile(r"^arn:aws:s3:::(?P<id>[A-Za-z0-9._-]{3,255})$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        buckets = self._list()
        yield from (d for d in self.ctx.map(self._details, buckets) if d is not None)

    def get_resource(self, resource_id: str) -> Raw | None:
        # ListBuckets only returns buckets this account owns in this region, so it answers
        # "does it exist (for us, here)" without HeadBucket's 403 ambiguity
        matches = [b for b in self._list(prefix=resource_id) if b["Name"] == resource_id]
        return self._details(matches[0]) if matches else None

    def _list(self, prefix: str | None = None) -> list[Raw]:
        params: dict[str, Any] = {
            "BucketRegion": self.ctx.region,
            "PaginationConfig": {"PageSize": 1000},  # required above 10,000 buckets
        }
        if prefix:
            params["Prefix"] = prefix
        return [
            dict(bucket)
            for page in self.client.get_paginator("list_buckets").paginate(**params)
            for bucket in page.get("Buckets", [])
        ]

    def _details(self, bucket: Mapping[str, Any]) -> Raw | None:
        name = bucket["Name"]
        try:
            return {
                "Bucket": dict(bucket),
                "Tags": self._optional(
                    NO_TAGS, lambda: self.client.get_bucket_tagging(Bucket=name), "TagSet"
                )
                or [],
                "Versioning": _clean(self.client.get_bucket_versioning(Bucket=name)),
                "Encryption": self._optional(
                    NO_ENCRYPTION,
                    lambda: self.client.get_bucket_encryption(Bucket=name),
                    "ServerSideEncryptionConfiguration",
                ),
                "PublicAccessBlock": self._optional(
                    NO_PUBLIC_ACCESS_BLOCK,
                    lambda: self.client.get_public_access_block(Bucket=name),
                    "PublicAccessBlockConfiguration",
                ),
            }
        except ClientError as exc:
            if error_code(exc) == "NoSuchBucket":  # deleted between listing and details
                return None
            raise

    @staticmethod
    def _optional(missing_code: str, call: Any, key: str) -> Any:
        try:
            return call().get(key)
        except ClientError as exc:
            if error_code(exc) == missing_code:
                return None
            raise

    def resource_id(self, raw: Raw) -> str:
        return str(raw["Bucket"]["Name"])

    def build_arn(self, raw: Raw) -> str:
        return f"arn:aws:s3:::{self.resource_id(raw)}"

    def extract_name(self, raw: Raw) -> str | None:
        return self.resource_id(raw)


def _clean(response: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in response.items() if k != "ResponseMetadata"}
