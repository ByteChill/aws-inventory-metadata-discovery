"""Who am I, and API key management (spec 10)."""

from __future__ import annotations

from typing import Annotated

from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, Response
from sqlalchemy.engine import RowMapping

from inventory.api.auth import CurrentPrincipal, Principal, new_api_key, require
from inventory.api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyOut, MeOut
from inventory.api.state import ApiState, get_state
from inventory.db import ApiKeyRepository
from inventory.errors import ForbiddenError, NotFoundError

router = APIRouter(prefix="/v1", tags=["auth"])
State = Annotated[ApiState, Depends(get_state)]
keys = ApiKeyRepository()


async def key_admin(
    state: State, principal: Annotated[Principal, Depends(require("api_keys:manage"))]
) -> Principal:
    # with authentication disabled every caller is an admin: a key created then would stay a
    # valid admin credential after enforcement is switched on
    if not state.auth.enabled:
        raise ForbiddenError("API keys can only be managed when authentication is enforced")
    return principal


Admin = Annotated[Principal, Depends(key_admin)]


def to_key(row: RowMapping) -> ApiKeyOut:
    return ApiKeyOut.model_validate({k: v for k, v in row.items() if k != "key_hash"})


@router.get("/me", response_model=MeOut)
async def me(state: State, principal: CurrentPrincipal) -> MeOut:
    return MeOut(
        subject=principal.subject,
        kind=principal.kind,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
        auth_mode=state.settings.auth_mode,
    )


@router.get("/api-keys", response_model=ApiKeyList)
async def list_keys(state: State, _: Admin) -> ApiKeyList:
    def load() -> list[RowMapping]:
        with state.runtime.db.connect() as conn:
            return keys.list(conn)

    return ApiKeyList(items=[to_key(r) for r in await run_sync(load)])


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(state: State, body: ApiKeyCreate, admin: Admin) -> ApiKeyCreated:
    key_id, secret, key_hash = new_api_key()

    def create() -> RowMapping:
        with state.runtime.db.begin() as conn:
            return keys.create(
                conn,
                key_id=key_id,
                name=body.name,
                key_hash=key_hash,
                roles=sorted(set(body.roles)),
                created_by=admin.subject,
                expires_at=body.expires_at,
            )

    return ApiKeyCreated(key=to_key(await run_sync(create)), secret=secret)


@router.delete("/api-keys/{key_id}", status_code=204, response_class=Response)
async def revoke_key(state: State, key_id: str, _: Admin) -> Response:
    def revoke() -> bool:
        with state.runtime.db.begin() as conn:
            return keys.revoke(conn, key_id)

    if not await run_sync(revoke):
        raise NotFoundError(f"active API key {key_id} not found")
    return Response(status_code=204)
