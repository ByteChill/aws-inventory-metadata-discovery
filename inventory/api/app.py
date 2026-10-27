"""FastAPI application factory.

Run with ``uvicorn --factory inventory.api.app:create_app --host 0.0.0.0 --port 8080``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.responses import Response
from starlette.types import Scope

from inventory import __version__
from inventory.api import resources, scans
from inventory.api.errors import install_exception_handlers
from inventory.api.middleware import RequestContextMiddleware
from inventory.api.queries import ResourceQueries
from inventory.api.refresh import RefreshService
from inventory.api.state import ApiState
from inventory.bootstrap import Runtime, build_runtime
from inventory.config import ApiSettings, get_api_settings, get_common_settings
from inventory.db import connect_args, install_session_settings
from inventory.observability import enable_metric_api, setup_logging

SERVICE_NAME = "inventory-api"
UI_DIR = Path(__file__).parent / "ui"
# Resource names, tags and data come from scanned accounts: the page renders them as text,
# and the CSP blocks inline scripts as a second line of defence.
UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # revalidate (cheap, ETag) so a deploy never leaves an old app.js running against a new API
    "Cache-Control": "no-cache",
}
log = logging.getLogger(__name__)


class UiFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(UI_HEADERS)
        return response


def create_app(
    settings: ApiSettings | None = None,
    runtime: Runtime | None = None,
    async_engine: AsyncEngine | None = None,
) -> FastAPI:
    """``runtime`` / ``async_engine`` are injected by tests; production builds them."""
    api_settings = settings or get_api_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime
        if rt is None:
            common = get_common_settings()
            setup_logging(SERVICE_NAME, common.log_level, common.log_sample_rate)
            rt = await run_sync(
                lambda: build_runtime(
                    SERVICE_NAME,
                    statement_timeout_ms=api_settings.statement_timeout_ms,
                    pool_size=api_settings.db_pool_size,
                    max_overflow=api_settings.db_max_overflow,
                )
            )
            if common.metrics_mode == "api":
                enable_metric_api(rt.clients.client("monitoring", common.home_region))
        engine = async_engine
        if engine is None:
            engine = create_async_engine(
                rt.db.url.render_as_string(hide_password=False),
                pool_size=api_settings.db_pool_size,
                max_overflow=api_settings.db_max_overflow,
                pool_pre_ping=True,
                connect_args=connect_args(SERVICE_NAME),
            )
            install_session_settings(
                engine.sync_engine, api_settings.statement_timeout_ms, rt.db_credentials
            )
        app.state.api = ApiState(
            api_settings,
            rt,
            ResourceQueries(engine, api_settings.search_timeout_ms),
            RefreshService(rt, api_settings),
        )
        log.info("api started", extra={"version": __version__})
        try:
            yield
        finally:
            if async_engine is None:
                await engine.dispose()
            if runtime is None:
                rt.db.dispose()

    app = FastAPI(
        title="AWS Inventory API",
        version=__version__,
        lifespan=lifespan,
        description="Query, count, scan and refresh AWS resource inventory. See docs/api.md.",
    )
    trace_id = setup_tracing(app, api_settings) if api_settings.tracing_enabled else None
    app.add_middleware(RequestContextMiddleware, trace_id=trace_id)
    install_exception_handlers(app)
    app.include_router(resources.router)
    app.include_router(scans.router)

    if api_settings.ui_enabled:
        app.mount("/ui", UiFiles(directory=UI_DIR, html=True), name="ui")

        # explicit relative redirects: Starlette's slash redirect builds an absolute URL from Host
        @app.get("/", include_in_schema=False)
        @app.get("/ui", include_in_schema=False)
        async def to_ui() -> RedirectResponse:
            return RedirectResponse("/ui/")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Any:
        try:
            await app.state.api.queries.ping()
        except Exception:
            log.warning("readiness check failed", exc_info=True)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app


def setup_tracing(app: FastAPI, settings: ApiSettings) -> Callable[[], str | None]:
    """OpenTelemetry -> ADOT sidecar -> X-Ray. Imported lazily: tracing is optional."""
    from opentelemetry import propagate, trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.propagators.aws import AwsXRayPropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        id_generator=AwsXRayIdGenerator(), resource=Resource.create({"service.name": SERVICE_NAME})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(AwsXRayPropagator())
    FastAPIInstrumentor.instrument_app(app)
    BotocoreInstrumentor().instrument()  # type: ignore[no-untyped-call]

    def current_trace_id() -> str | None:
        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return None
        raw = f"{context.trace_id:032x}"
        return f"1-{raw[:8]}-{raw[8:]}"

    return current_trace_id
