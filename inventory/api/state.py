"""Per-application state shared by the routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from inventory.api.queries import ResourceQueries
from inventory.api.refresh import RefreshService
from inventory.bootstrap import Runtime
from inventory.config import ApiSettings

if TYPE_CHECKING:
    from inventory.api.auth import Authenticator


@dataclass
class ApiState:
    settings: ApiSettings
    runtime: Runtime
    queries: ResourceQueries
    refresh: RefreshService
    auth: Authenticator


def get_state(request: Request) -> ApiState:
    state: ApiState = request.app.state.api
    return state
