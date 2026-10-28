"""Resource query endpoints."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.engine import RowMapping
from starlette.convertors import PathConvertor, StringConvertor, register_url_convertor

from inventory.api.auth import require
from inventory.api.errors import ValidationFailedError
from inventory.api.queries import GROUPABLE, PageRequest, ResourceFilters, ResourceQueries
from inventory.api.schemas import (
    Account,
    AccountsResponse,
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
from inventory.models import AccountType

router = APIRouter(
    prefix="/v1", tags=["resources"], dependencies=[Depends(require("resources:read"))]
)
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


@dataclass(frozen=True, slots=True)
class ListOptions:
    """Query parameters shared by /v1/resources and its path forms (everything but the scope)."""

    eims: tuple[str, ...]
    account_types: tuple[str, ...]
    tags: dict[str, str]
    tag_keys: tuple[str, ...]
    name_prefix: str | None
    changed_since: datetime | None
    include_deleted: bool
    page: PageRequest

    def filters(
        self, account_ids: Sequence[str], regions: Sequence[str], resource_types: Sequence[str]
    ) -> ResourceFilters:
        return ResourceFilters(
            account_ids=tuple(account_ids),
            regions=tuple(regions),
            resource_types=tuple(resource_types),
            eims=self.eims,
            account_types=self.account_types,
            tags=self.tags,
            tag_keys=self.tag_keys,
            name_prefix=self.name_prefix,
            changed_since=self.changed_since,
            include_deleted=self.include_deleted,
        )


def list_options(
    eim: Annotated[list[str] | None, Query()] = None,
    account_type: Annotated[list[AccountType] | None, Query()] = None,
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
) -> ListOptions:
    tags, tag_keys = parse_tags(tag or [])
    page = PageRequest(sort, order, limit, cursor, with_total, full=view == "full")
    return ListOptions(
        tuple(eim or ()),
        tuple(account_type or ()),
        tags,
        tag_keys,
        name_prefix,
        changed_since,
        include_deleted,
        page,
    )


Options = Annotated[ListOptions, Depends(list_options)]


@router.get("/resources", response_model=Page[Resource], response_model_exclude_none=False)
async def list_resources(
    state: State,
    options: Options,
    account_id: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
) -> Page[Resource]:
    filters = options.filters(account_id or (), region or (), resource_type or ())
    return await _page(state.queries, filters, options.page)


@router.post("/resources/search", response_model=Page[Resource])
async def search_resources(state: State, body: SearchRequest) -> Page[Resource]:
    filters = ResourceFilters(
        account_ids=tuple(body.account_ids or ()),
        regions=tuple(body.regions or ()),
        resource_types=tuple(body.resource_types or ()),
        eims=tuple(body.eims or ()),
        account_types=tuple(body.account_types or ()),
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
    row = await find_by_arn(state, arn, include_deleted)
    if row is None:
        raise NotFoundError("resource not found")
    return to_resource(row)


async def find_by_arn(state: ApiState, arn: str, include_deleted: bool) -> RowMapping | None:
    routed = state.runtime.registry.route_arn(arn)
    if routed is not None:
        cls, parsed = routed
        region = GLOBAL_REGION if cls.is_global else parsed.region
        if parsed.account_id and region and not parsed.id_is_suffix:
            key = (cls.resource_type, parsed.account_id, region, parsed.resource_id)
            row = await state.queries.get(key, include_deleted)  # single-partition lookup
            if row is not None:
                return row
    return await state.queries.get_by_arn(arn, include_deleted)


@router.get("/resources/stats", response_model=StatsResponse)
async def resource_stats(
    state: State,
    group_by: str = "resource_type",
    account_id: Annotated[list[str] | None, Query()] = None,
    region: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
    eim: Annotated[list[str] | None, Query()] = None,
    account_type: Annotated[list[AccountType] | None, Query()] = None,
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
        eims=tuple(eim or ()),
        account_types=tuple(account_type or ()),
        include_deleted=include_deleted,
    )
    result, total = await state.queries.stats(groups, filters)
    return StatsResponse(groups=result, total=total)


@router.get("/accounts", response_model=AccountsResponse)
async def accounts(state: State) -> AccountsResponse:
    """Every account account-directory has listed (a few thousand at most: not paged)."""
    rows = await state.queries.accounts()
    return AccountsResponse(items=[Account.model_validate(dict(r)) for r in rows])


@router.get("/accounts/{account_id}/coverage", response_model=CoverageResponse)
async def coverage(state: State, account_id: str) -> CoverageResponse:
    regions, scopes = await state.queries.coverage(account_id)
    known = await state.queries.account(account_id)
    enabled = sorted(r["region"] for r in regions if r["enabled"])
    seen = {(s["region"], s["resource_type"]) for s in scopes}
    items = [
        CoverageScope.model_validate({k: s[k] for k in CoverageScope.model_fields if k in s})
        for s in scopes
    ]
    registry = state.runtime.registry
    expected_regions = [*enabled, GLOBAL_REGION] if enabled else []
    for region in expected_regions:
        for cls in registry.applicable(region):
            if (region, cls.resource_type) not in seen:
                items.append(
                    CoverageScope(region=region, resource_type=cls.resource_type, status="never")
                )
    items.sort(key=lambda s: (s.region, s.resource_type))
    checked = min((r["checked_at"] for r in regions), default=None)
    return CoverageResponse(
        account_id=account_id,
        account=Account.model_validate(dict(known)) if known else None,
        enabled_regions=enabled,
        regions_checked_at=checked,
        scopes=items,
    )


@router.get("/resource-types", response_model=ResourceTypesResponse)
async def resource_types(state: State) -> ResourceTypesResponse:
    runtime = state.runtime
    items: list[ResourceTypeOut] = []
    for cls in runtime.registry.all():
        enabled = [GLOBAL_REGION] if cls.is_global else list(runtime.common.scan_regions)
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


# --------------------------------------------------------------------------- path forms
# /v1/resources/all | /{account} | /{account}/{region} | /{account}/{region}/{type | arn}.
# The convertors keep these from shadowing /resources/stats, /resources/by-arn, ...


class _AccountConvertor(StringConvertor):
    regex = "[0-9]{12}"


class _ArnConvertor(PathConvertor):
    regex = "arn:.+"  # ARNs contain '/', resource types never start with 'arn:'


register_url_convertor("account", _AccountConvertor())
register_url_convertor("arn", _ArnConvertor())


def check_path(state: ApiState, region: str, resource_type: str | None = None) -> None:
    if region not in (GLOBAL_REGION, *state.runtime.common.scan_regions):
        raise ValidationFailedError(f"region {region} is not supported")
    if resource_type is None:
        return
    registry = state.runtime.registry
    if not registry.has(resource_type):
        raise ValidationFailedError(f"unknown resource_type {resource_type}")
    if registry.get(resource_type).is_global != (region == GLOBAL_REGION):
        raise ValidationFailedError(f"{resource_type} is not in region {region}")


@router.get("/resources/all", response_model=Page[Resource])
async def list_all(state: State, options: Options) -> Page[Resource]:
    return await _page(state.queries, options.filters((), (), ()), options.page)


@router.get("/resources/{account_id:account}", response_model=Page[Resource])
async def list_account(state: State, options: Options, account_id: str) -> Page[Resource]:
    return await _page(state.queries, options.filters([account_id], (), ()), options.page)


@router.get("/resources/{account_id:account}/{region}", response_model=Page[Resource])
async def list_region(
    state: State, options: Options, account_id: str, region: str
) -> Page[Resource]:
    check_path(state, region)
    filters = options.filters([account_id], [region], ())
    return await _page(state.queries, filters, options.page)


@router.get("/resources/{account_id:account}/{region}/{arn:arn}", response_model=Resource)
async def get_in_scope(
    state: State, account_id: str, region: str, arn: str, include_deleted: bool = False
) -> Resource:
    check_path(state, region)
    row = await find_by_arn(state, arn, include_deleted)
    # the ARN must belong to the path's account and region (global resources: 'global')
    if row is None or (row["account_id"], row["region"]) != (account_id, region):
        raise NotFoundError("resource not found")
    return to_resource(row)


@router.get(
    "/resources/{account_id:account}/{region}/{resource_type}", response_model=Page[Resource]
)
async def list_type(
    state: State, options: Options, account_id: str, region: str, resource_type: str
) -> Page[Resource]:
    check_path(state, region, resource_type)
    filters = options.filters([account_id], [region], [resource_type])
    return await _page(state.queries, filters, options.page)
