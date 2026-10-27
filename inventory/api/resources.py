"""Resource query endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.engine import RowMapping

from inventory.api.errors import ValidationFailedError
from inventory.api.queries import GROUPABLE, PageRequest, ResourceFilters, ResourceQueries
from inventory.api.schemas import (
    CoverageResponse,
    CoverageScope,
    Page,
    Resource,
    ResourceTypeOut,
    ResourceTypesResponse,
    SearchRequest,
    SortField,
    SortOrder,
    StatsResponse,
    View,
)
from inventory.api.state import ApiState, get_state
from inventory.config import GLOBAL_REGION
from inventory.errors import NotFoundError

router = APIRouter(prefix="/v1", tags=["resources"])
State = Annotated[ApiState, Depends(get_state)]


def to_resource(row: RowMapping) -> Resource:
    return Resource.model_validate(dict(row))


def parse_tags(values: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """``Key=Value`` (split at the first '=') or a bare ``Key`` meaning "has this tag"."""
    tags: dict[str, str] = {}
    keys: list[str] = []
    for value in values:
        key, sep, tag_value = value.partition("=")
        if sep:
            tags[key] = tag_value
        else:
            keys.append(key)
    return tags, tuple(keys)


async def _page(
    queries: ResourceQueries, filters: ResourceFilters, page: PageRequest
) -> Page[Resource]:
    result = await queries.list_resources(filters, page)
    return Page[Resource](
        items=[to_resource(r) for r in result.rows],
        next_cursor=result.next_cursor,
        total=result.total,
    )


@router.get("/resources", response_model=Page[Resource], response_model_exclude_none=False)
async def list_resources(
    state: State,
    account_id: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    name_prefix: str | None = None,
    changed_since: datetime | None = None,
    include_deleted: bool = False,
    view: View = "summary",
    sort: SortField = "last_changed_at",
    order: SortOrder = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
    with_total: bool = False,
) -> Page[Resource]:
    tags, tag_keys = parse_tags(tag or [])
    filters = ResourceFilters(
        account_ids=tuple(account_id or ()),
        regions=tuple(region or ()),
        resource_types=tuple(resource_type or ()),
        tags=tags,
        tag_keys=tag_keys,
        name_prefix=name_prefix,
        changed_since=changed_since,
        include_deleted=include_deleted,
    )
    page = PageRequest(sort, order, limit, cursor, with_total, full=view == "full")
    return await _page(state.queries, filters, page)


@router.post("/resources/search", response_model=Page[Resource])
async def search_resources(state: State, body: SearchRequest) -> Page[Resource]:
    filters = ResourceFilters(
        account_ids=tuple(body.account_ids or ()),
        regions=tuple(body.regions or ()),
        resource_types=tuple(body.resource_types or ()),
        tags=body.tags or {},
        tag_keys=tuple(body.tag_keys or ()),
        name_prefix=body.name_prefix,
        changed_since=body.changed_since,
        include_deleted=body.include_deleted,
        data_jsonpath=body.data_jsonpath,
    )
    page = PageRequest(
        body.sort, body.order, body.limit, body.cursor, body.with_total, body.view == "full"
    )
    return await _page(state.queries, filters, page)


@router.get("/resources/by-arn", response_model=Resource)
async def get_by_arn(state: State, arn: str, include_deleted: bool = False) -> Resource:
    row = None
    routed = state.runtime.registry.route_arn(arn)
    if routed is not None:
        cls, parsed = routed
        region = GLOBAL_REGION if cls.is_global else parsed.region
        if parsed.account_id and region and not parsed.id_is_suffix:
            key = (cls.resource_type, parsed.account_id, region, parsed.resource_id)
            row = await state.queries.get(key, include_deleted)  # single-partition lookup
    if row is None:
        row = await state.queries.get_by_arn(arn, include_deleted)
    if row is None:
        raise NotFoundError("resource not found")
    return to_resource(row)


@router.get("/resources/stats", response_model=StatsResponse)
async def resource_stats(
    state: State,
    group_by: str = "resource_type",
    account_id: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
    include_deleted: bool = False,
) -> StatsResponse:
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    invalid = [g for g in groups if g not in GROUPABLE]
    if invalid or len(set(groups)) != len(groups):
        raise ValidationFailedError(f"group_by accepts {', '.join(GROUPABLE)}")
    filters = ResourceFilters(
        account_ids=tuple(account_id or ()),
        regions=tuple(region or ()),
        resource_types=tuple(resource_type or ()),
        include_deleted=include_deleted,
    )
    result, total = await state.queries.stats(groups, filters)
    return StatsResponse(groups=result, total=total)


@router.get("/accounts/{account_id}/coverage", response_model=CoverageResponse)
async def coverage(state: State, account_id: str) -> CoverageResponse:
    regions, scopes = await state.queries.coverage(account_id)
    enabled = sorted(r["region"] for r in regions if r["enabled"])
    seen = {(s["region"], s["resource_type"]) for s in scopes}
    items = [
        CoverageScope.model_validate({k: s[k] for k in CoverageScope.model_fields if k in s})
        for s in scopes
    ]
    registry, resolver = state.runtime.registry, state.runtime.resolver
    expected_regions = [*enabled, GLOBAL_REGION] if enabled else []
    for region in expected_regions:
        available, _ = registry.for_region(region, resolver)
        for cls in available:
            if (region, cls.resource_type) not in seen:
                items.append(
                    CoverageScope(region=region, resource_type=cls.resource_type, status="never")
                )
    items.sort(key=lambda s: (s.region, s.resource_type))
    checked = min((r["checked_at"] for r in regions), default=None)
    return CoverageResponse(
        account_id=account_id, enabled_regions=enabled, regions_checked_at=checked, scopes=items
    )


@router.get("/resource-types", response_model=ResourceTypesResponse)
async def resource_types(state: State) -> ResourceTypesResponse:
    runtime = state.runtime
    items: list[ResourceTypeOut] = []
    for cls in runtime.registry.all():
        regions = [GLOBAL_REGION] if cls.is_global else runtime.common.supported_regions
        enabled = [
            r for r in regions if all(runtime.resolver.has_endpoint(s, r) for s in cls.endpoints())
        ]
        items.append(
            ResourceTypeOut(
                resource_type=cls.resource_type,
                service=cls.service,
                is_global=cls.is_global,
                depends_on=list(cls.depends_on),
                enabled_regions=enabled,
            )
        )
    return ResourceTypesResponse(items=items)
