"""ECS collectors: Cluster, Service and the task-definition revisions in use."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from botocore.exceptions import ClientError

from inventory.collectors.base import Collector, ParsedArn, Raw, error_code, register
from inventory.models import KeepExisting, ResourceRecord

_PREFIX = r"^arn:aws:ecs:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):"
CLUSTER_TYPE = "AWS::ECS::Cluster"
SERVICE_TYPE = "AWS::ECS::Service"
DESCRIBE_CLUSTERS_BATCH = 100
DESCRIBE_SERVICES_BATCH = 10


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _name_from_arn(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


# Runtime counters and rollout progress: they change with every task start/stop and would
# make the service look "updated" on almost every scan.
SERVICE_VOLATILE = ("events", "runningCount", "pendingCount")
DEPLOYMENT_VOLATILE = (
    "runningCount",
    "pendingCount",
    "failedTasks",
    "updatedAt",
    "rolloutState",
    "rolloutStateReason",
)
TASK_SET_VOLATILE = (
    "runningCount",
    "pendingCount",
    "computedDesiredCount",
    "stabilityStatus",
    "stabilityStatusAt",
    "updatedAt",
)


def _check_failures(response: Mapping[str, Any], what: str) -> None:
    """``MISSING`` means not found; any other failure must fail the listing, not hide items."""
    other = [f for f in response.get("failures", []) if f.get("reason") != "MISSING"]
    if other:
        reason = other[0].get("reason")
        raise RuntimeError(f"ECS could not describe {len(other)} {what} (first reason: {reason})")


def _without(item: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in fields}


def normalize_service(service: Mapping[str, Any]) -> Raw:
    data = _without(service, SERVICE_VOLATILE)
    data["deployments"] = [_without(d, DEPLOYMENT_VOLATILE) for d in service.get("deployments", [])]
    data["taskSets"] = [_without(t, TASK_SET_VOLATILE) for t in service.get("taskSets", [])]
    return data


@register
class ClusterCollector(Collector):
    resource_type = CLUSTER_TYPE
    service = "ecs"
    drop_fields = (
        "runningTasksCount",
        "pendingTasksCount",
        "activeServicesCount",
        "registeredContainerInstancesCount",
        "statistics",
    )
    arn_pattern = re.compile(_PREFIX + r"cluster/(?P<id>[^/]+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        arns = [
            arn
            for page in self.client.get_paginator("list_clusters").paginate()
            for arn in page.get("clusterArns", [])
        ]
        for batch in _chunks(arns, DESCRIBE_CLUSTERS_BATCH):
            yield from (c for c in self._describe(batch) if c.get("status") != "INACTIVE")

    def get_resource(self, resource_id: str) -> Raw | None:
        found = [c for c in self._describe([resource_id]) if c.get("status") != "INACTIVE"]
        return found[0] if found else None

    def _describe(self, clusters: list[str]) -> list[Raw]:
        response = self.client.describe_clusters(
            clusters=clusters, include=["TAGS", "SETTINGS", "CONFIGURATIONS"]
        )
        _check_failures(response, "clusters")
        return list(response.get("clusters", []))

    def resource_id(self, raw: Raw) -> str:
        return str(raw["clusterName"])

    def build_arn(self, raw: Raw) -> str:
        return str(raw["clusterArn"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["clusterName"])


@register
class ServiceCollector(Collector):
    """Resource id is ``{cluster}/{service}`` because service names repeat across clusters."""

    resource_type = SERVICE_TYPE
    service = "ecs"
    depends_on = (CLUSTER_TYPE,)
    arn_pattern = re.compile(_PREFIX + r"service/(?P<cluster>[^/]+)/(?P<name>[^/]+)$")
    old_arn_pattern = re.compile(_PREFIX + r"service/(?P<name>[^/]+)$")

    @classmethod
    def parse_arn(cls, arn: str) -> ParsedArn | None:
        match = cls.arn_pattern.match(arn)
        if match:
            return ParsedArn(
                match["account"], match["region"], f"{match['cluster']}/{match['name']}"
            )
        old = cls.old_arn_pattern.match(arn)
        if old:  # old ARN format has no cluster: resolved from the stored inventory
            return ParsedArn(old["account"], old["region"], old["name"], id_is_suffix=True)
        return None

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        clusters = [r.arn for r in upstream.get(CLUSTER_TYPE, [])]
        for services in self.ctx.map(self._services_in, clusters):
            yield from services

    def _services_in(self, cluster_arn: str) -> list[Raw]:
        arns = [
            arn
            for page in self.client.get_paginator("list_services").paginate(
                cluster=cluster_arn, PaginationConfig={"PageSize": 100}
            )
            for arn in page.get("serviceArns", [])
        ]
        services: list[Raw] = []
        for batch in _chunks(arns, DESCRIBE_SERVICES_BATCH):
            services.extend(s for s in self._describe(cluster_arn, batch) if self._is_live(s))
        return services

    def get_resource(self, resource_id: str) -> Raw | None:
        cluster, _, name = resource_id.partition("/")
        try:
            found = [s for s in self._describe(cluster, [name]) if self._is_live(s)]
        except ClientError as exc:
            if error_code(exc) == "ClusterNotFoundException":  # the whole cluster is gone
                return None
            raise
        return found[0] if found else None

    def _describe(self, cluster: str, services: list[str]) -> list[Raw]:
        response = self.client.describe_services(
            cluster=cluster, services=services, include=["TAGS"]
        )
        _check_failures(response, "services")
        return [normalize_service(s) for s in response.get("services", [])]

    @staticmethod
    def _is_live(raw: Raw) -> bool:
        return raw.get("status") != "INACTIVE"

    def resource_id(self, raw: Raw) -> str:
        return f"{_name_from_arn(str(raw['clusterArn']))}/{raw['serviceName']}"

    def build_arn(self, raw: Raw) -> str:
        return str(raw["serviceArn"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["serviceName"])

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {
            "cluster_arn": raw.get("clusterArn"),
            "task_definition_arns": task_definitions_in_use(raw),
        }


def task_definitions_in_use(service: Raw) -> list[str]:
    """Revisions actually running: every deployment (rolling) or task set (CodeDeploy/external)."""
    controller = (service.get("deploymentController") or {}).get("type", "ECS")
    items = service.get("deployments" if controller == "ECS" else "taskSets", [])
    # a finished deployment can linger briefly as INACTIVE; it no longer runs tasks
    arns = [i.get("taskDefinition") for i in items if i.get("status") != "INACTIVE"]
    in_use = sorted({a for a in arns if a})
    if not in_use and service.get("taskDefinition"):
        in_use = [str(service["taskDefinition"])]
    return in_use


@register
class TaskDefinitionCollector(Collector):
    """Only the revisions referenced by services; id is ``family:revision``.

    A revision is immutable, so known revisions are kept without re-describing them; the
    daily ``full_refresh`` re-reads everything to pick up tag changes.
    """

    resource_type = "AWS::ECS::TaskDefinition"
    service = "ecs"
    depends_on = (SERVICE_TYPE,)
    arn_pattern = re.compile(_PREFIX + r"task-definition/(?P<id>[^:/]+:\d+)$")

    def list_resources(
        self, upstream: Mapping[str, Sequence[ResourceRecord]]
    ) -> Iterator[Raw | KeepExisting]:
        in_use = sorted(
            {
                arn
                for service in upstream.get(SERVICE_TYPE, [])
                for arn in service.relations.get("task_definition_arns", [])
            }
        )
        to_describe: list[str] = []
        for arn in in_use:
            revision = _name_from_arn(arn)
            if not self.ctx.full_refresh and revision in self.ctx.known_ids:
                yield KeepExisting(revision)
            else:
                to_describe.append(arn)
        # in-use revisions always exist (even deregistered ones): any failure fails the
        # listing instead of turning into a deletion
        yield from self.ctx.map(self._describe, to_describe)

    def get_resource(self, resource_id: str) -> Raw | None:
        try:
            return self._describe(resource_id)
        except ClientError as exc:
            message = str(exc.response.get("Error", {}).get("Message", ""))
            if error_code(exc) == "ClientException" and "unable to describe" in message.lower():
                return None
            raise

    def _describe(self, task_definition: str) -> Raw:
        response = self.client.describe_task_definition(
            taskDefinition=task_definition, include=["TAGS"]
        )
        return {**response["taskDefinition"], "tags": response.get("tags", [])}

    def resource_id(self, raw: Raw) -> str:
        return f"{raw['family']}:{raw['revision']}"

    def build_arn(self, raw: Raw) -> str:
        return str(raw["taskDefinitionArn"])

    def extract_name(self, raw: Raw) -> str | None:
        return str(raw["family"])

    def extract_relations(self, raw: Raw) -> dict[str, Any]:
        return {"family": raw.get("family")}
