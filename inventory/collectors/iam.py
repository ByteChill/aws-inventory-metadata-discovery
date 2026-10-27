"""IAM collectors (global): Role, User and customer-managed policies.

The listing uses one paginated GetAccountAuthorizationDetails call shared by all three types
(``ctx.memo``). ``get_resource`` assembles the same view from the per-entity APIs, and both
paths go through the same ``*_view`` function so a refresh never looks like a change.
Volatile fields (RoleLastUsed) are left out of the view.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from botocore.exceptions import ClientError

from inventory.collectors.base import Collector, Raw, error_code, register
from inventory.models import KeepExisting, ResourceRecord

_ACCOUNT = r"(?P<account>\d{12})"
NOT_FOUND = "NoSuchEntity"
AUTH_DETAILS_KEY = "iam:authorization_details"


def _authorization_details(client: Any) -> dict[str, list[Raw]]:
    details: dict[str, list[Raw]] = {"RoleDetailList": [], "UserDetailList": [], "Policies": []}
    paginator = client.get_paginator("get_account_authorization_details")
    for page in paginator.paginate(Filter=["Role", "User", "LocalManagedPolicy"]):
        for key in details:
            details[key].extend(page.get(key, []))
    return details


def _sorted_by(items: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted((dict(i) for i in items), key=lambda i: str(i.get(key, "")))


def _attached(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_by(
        [{"PolicyName": i["PolicyName"], "PolicyArn": i["PolicyArn"]} for i in items], "PolicyArn"
    )


def _inline(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_by(
        [{"PolicyName": i["PolicyName"], "PolicyDocument": i["PolicyDocument"]} for i in items],
        "PolicyName",
    )


def role_view(
    core: Mapping[str, Any],
    inline: Sequence[Mapping[str, Any]],
    attached: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
) -> Raw:
    return {
        "Path": core["Path"],
        "RoleName": core["RoleName"],
        "RoleId": core["RoleId"],
        "Arn": core["Arn"],
        "CreateDate": core["CreateDate"],
        "AssumeRolePolicyDocument": core.get("AssumeRolePolicyDocument"),
        "PermissionsBoundary": core.get("PermissionsBoundary"),
        "Tags": core.get("Tags", []),
        "RolePolicyList": _inline(inline),
        "AttachedManagedPolicies": _attached(attached),
        "InstanceProfileList": _sorted_by(
            [{"InstanceProfileName": p["InstanceProfileName"], "Arn": p["Arn"]} for p in profiles],
            "Arn",
        ),
    }


def user_view(
    core: Mapping[str, Any],
    inline: Sequence[Mapping[str, Any]],
    attached: Sequence[Mapping[str, Any]],
    groups: Sequence[str],
) -> Raw:
    return {
        "Path": core["Path"],
        "UserName": core["UserName"],
        "UserId": core["UserId"],
        "Arn": core["Arn"],
        "CreateDate": core["CreateDate"],
        "PermissionsBoundary": core.get("PermissionsBoundary"),
        "Tags": core.get("Tags", []),
        "UserPolicyList": _inline(inline),
        "AttachedManagedPolicies": _attached(attached),
        "GroupList": sorted(groups),
    }


def policy_view(
    core: Mapping[str, Any],
    default_version: Mapping[str, Any] | None,
    tags: Sequence[Mapping[str, Any]],
) -> Raw:
    """Fields present in both GetAccountAuthorizationDetails and GetPolicy.

    The name is taken from the ARN (some implementations omit PolicyName in the bulk call);
    PolicyId is left out for the same reason, so list and get always hash alike.
    """
    arn = str(core["Arn"])
    return {
        "PolicyName": core.get("PolicyName") or arn.rsplit("/", 1)[-1],
        "Arn": arn,
        "Path": core["Path"],
        "DefaultVersionId": core.get("DefaultVersionId"),
        "IsAttachable": core.get("IsAttachable"),
        "CreateDate": core.get("CreateDate"),
        "UpdateDate": core.get("UpdateDate"),
        "DefaultVersion": (
            {
                "VersionId": default_version.get("VersionId"),
                "Document": default_version.get("Document"),
                "CreateDate": default_version.get("CreateDate"),
            }
            if default_version
            else None
        ),
        "Tags": list(tags),
    }


class _IamCollector(Collector):
    service = "iam"
    is_global = True

    def _details(self) -> dict[str, list[Raw]]:
        return self.ctx.memo(AUTH_DETAILS_KEY, lambda: _authorization_details(self.client))

    def _paged(self, operation: str, key: str, **params: Any) -> list[Any]:
        return [
            item
            for page in self.client.get_paginator(operation).paginate(**params)
            for item in page.get(key, [])
        ]

    def _or_none(self, call: Callable[[], Raw]) -> Raw | None:
        try:
            return call()
        except ClientError as exc:
            if error_code(exc) == NOT_FOUND:
                return None
            raise

    def build_arn(self, raw: Raw) -> str:
        return str(raw["Arn"])


@register
class RoleCollector(_IamCollector):
    resource_type = "AWS::IAM::Role"
    arn_pattern = re.compile(rf"^arn:aws:iam::{_ACCOUNT}:role/(?:[^/]+/)*(?P<id>[^/]+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        for role in self._details()["RoleDetailList"]:
            yield role_view(
                role,
                role.get("RolePolicyList", []),
                role.get("AttachedManagedPolicies", []),
                role.get("InstanceProfileList", []),
            )

    def get_resource(self, resource_id: str) -> Raw | None:
        def load() -> Raw:
            core = self.client.get_role(RoleName=resource_id)["Role"]
            names = self._paged("list_role_policies", "PolicyNames", RoleName=resource_id)
            inline = [
                {
                    "PolicyName": n,
                    "PolicyDocument": self.client.get_role_policy(
                        RoleName=resource_id, PolicyName=n
                    )["PolicyDocument"],
                }
                for n in names
            ]
            attached = self._paged(
                "list_attached_role_policies", "AttachedPolicies", RoleName=resource_id
            )
            profiles = self._paged(
                "list_instance_profiles_for_role", "InstanceProfiles", RoleName=resource_id
            )
            return role_view(core, inline, attached, profiles)

        return self._or_none(load)

    def resource_id(self, raw: Raw) -> str:
        return str(raw["RoleName"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["RoleName"])

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {
            "attached_policy_arns": [p["PolicyArn"] for p in raw["AttachedManagedPolicies"]],
            "instance_profile_arns": [p["Arn"] for p in raw["InstanceProfileList"]],
        }


@register
class UserCollector(_IamCollector):
    resource_type = "AWS::IAM::User"
    arn_pattern = re.compile(rf"^arn:aws:iam::{_ACCOUNT}:user/(?:[^/]+/)*(?P<id>[^/]+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        for user in self._details()["UserDetailList"]:
            yield user_view(
                user,
                user.get("UserPolicyList", []),
                user.get("AttachedManagedPolicies", []),
                user.get("GroupList", []),
            )

    def get_resource(self, resource_id: str) -> Raw | None:
        def load() -> Raw:
            core = self.client.get_user(UserName=resource_id)["User"]
            names = self._paged("list_user_policies", "PolicyNames", UserName=resource_id)
            inline = [
                {
                    "PolicyName": n,
                    "PolicyDocument": self.client.get_user_policy(
                        UserName=resource_id, PolicyName=n
                    )["PolicyDocument"],
                }
                for n in names
            ]
            attached = self._paged(
                "list_attached_user_policies", "AttachedPolicies", UserName=resource_id
            )
            groups = [
                g["GroupName"]
                for g in self._paged("list_groups_for_user", "Groups", UserName=resource_id)
            ]
            return user_view(core, inline, attached, groups)

        return self._or_none(load)

    def resource_id(self, raw: Raw) -> str:
        return str(raw["UserName"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["UserName"])

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {
            "attached_policy_arns": [p["PolicyArn"] for p in raw["AttachedManagedPolicies"]],
            "group_names": list(raw["GroupList"]),
        }


@register
class ManagedPolicyCollector(_IamCollector):
    """Customer-managed policies only (AWS-managed policy ARNs use the account "aws")."""

    resource_type = "AWS::IAM::ManagedPolicy"
    arn_pattern = re.compile(rf"^arn:aws:iam::{_ACCOUNT}:policy/(?:[^/]+/)*(?P<id>[^/]+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        policies = self._details()["Policies"]
        yield from self.ctx.map(self._from_listing, policies)

    def _from_listing(self, policy: Mapping[str, Any]) -> Raw:
        default = next(
            (v for v in policy.get("PolicyVersionList", []) if v.get("IsDefaultVersion")), None
        )
        return policy_view(policy, default, self._tags(policy["Arn"]))

    def _tags(self, arn: str) -> list[Any]:
        return self._paged("list_policy_tags", "Tags", PolicyArn=arn)

    def get_resource(self, resource_id: str) -> Raw | None:
        def load() -> Raw | None:
            # the name alone does not give the policy path, so find the ARN via the listing
            matches = [
                p
                for p in self._paged("list_policies", "Policies", Scope="Local")
                if p["Arn"].rsplit("/", 1)[-1] == resource_id
            ]
            if not matches:
                return None
            core = self.client.get_policy(PolicyArn=matches[0]["Arn"])["Policy"]
            version = self.client.get_policy_version(
                PolicyArn=core["Arn"], VersionId=core["DefaultVersionId"]
            )["PolicyVersion"]
            return policy_view(core, version, self._tags(core["Arn"]))

        try:
            return load()
        except ClientError as exc:
            if error_code(exc) == NOT_FOUND:
                return None
            raise

    def resource_id(self, raw: Raw) -> str:
        return str(raw["PolicyName"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["PolicyName"])
