"""FastAPI application: REST API for managing endpoints and running health checks."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, status

from app.db import Database, DuplicateEndpointError
from app.monitor import check_endpoint
from app.schemas import CheckResultOut, EndpointCreate, EndpointOut, EndpointStatus

logger = logging.getLogger("api_health_monitor")

DEFAULT_DB_PATH = "health_monitor.db"


def create_app(
    db_path: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    check_interval_seconds: float | None = None,
) -> FastAPI:
    """Build the app. Arguments override environment variables (used by tests)."""
    db = Database(db_path or os.getenv("HEALTH_MONITOR_DB", DEFAULT_DB_PATH))
    if check_interval_seconds is None:
        check_interval_seconds = float(os.getenv("CHECK_INTERVAL_SECONDS", "0"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.init()
        client = http_client or httpx.AsyncClient(headers={"User-Agent": "api-health-monitor/0.1"})
        app.state.http_client = client

        scheduler = None
        if check_interval_seconds > 0:
            scheduler = asyncio.create_task(_run_periodically(check_interval_seconds))
            logger.info("Periodic checks every %ss", check_interval_seconds)
        try:
            yield
        finally:
            if scheduler:
                scheduler.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler
            if http_client is None:  # only close a client we created ourselves
                await client.aclose()

    app = FastAPI(
        title="API Health Monitor",
        description="Monitor HTTP endpoint availability and response times.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.db = db

    async def run_check(endpoint: dict[str, Any]) -> dict[str, Any]:
        result = await check_endpoint(
            app.state.http_client,
            endpoint["url"],
            method=endpoint["method"],
            timeout_seconds=endpoint["timeout_seconds"],
            expected_status=endpoint["expected_status"],
        )
        return db.save_result(endpoint["id"], result)

    async def run_all_checks() -> list[dict[str, Any]]:
        # Checks run concurrently, so total time ≈ the slowest endpoint, not the sum.
        return list(await asyncio.gather(*(run_check(e) for e in db.list_endpoints())))

    async def _run_periodically(interval: float) -> None:
        while True:
            try:
                await run_all_checks()
            except Exception:
                logger.exception("Periodic check run failed; will retry next interval")
            await asyncio.sleep(interval)

    def get_endpoint_or_404(endpoint_id: int) -> dict[str, Any]:
        endpoint = db.get_endpoint(endpoint_id)
        if endpoint is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Endpoint {endpoint_id} not found")
        return endpoint

    # --- routes ----------------------------------------------------------

    @app.get("/", tags=["service"])
    def root() -> dict[str, str]:
        return {"project": "API Health Monitor", "status": "running", "docs": "/docs"}

    @app.get("/health", tags=["service"])
    def health() -> dict[str, str]:
        """Health of this service itself."""
        return {"status": "healthy"}

    @app.post(
        "/endpoints",
        response_model=EndpointOut,
        status_code=status.HTTP_201_CREATED,
        tags=["endpoints"],
    )
    def add_endpoint(payload: EndpointCreate) -> dict[str, Any]:
        try:
            return db.add_endpoint(
                name=payload.name,
                url=str(payload.url),
                method=payload.method,
                expected_status=payload.expected_status,
                timeout_seconds=payload.timeout_seconds,
            )
        except DuplicateEndpointError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Endpoint with URL {payload.url} already exists"
            )

    @app.get("/endpoints", response_model=list[EndpointOut], tags=["endpoints"])
    def list_endpoints() -> list[dict[str, Any]]:
        return db.list_endpoints()

    @app.get("/endpoints/{endpoint_id}", response_model=EndpointOut, tags=["endpoints"])
    def get_endpoint(endpoint_id: int) -> dict[str, Any]:
        return get_endpoint_or_404(endpoint_id)

    @app.delete(
        "/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["endpoints"]
    )
    def delete_endpoint(endpoint_id: int) -> Response:
        """Delete an endpoint and its whole check history."""
        if not db.delete_endpoint(endpoint_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Endpoint {endpoint_id} not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/endpoints/{endpoint_id}/check", response_model=CheckResultOut, tags=["checks"])
    async def check_one(endpoint_id: int) -> dict[str, Any]:
        """Run a health check now and save the result."""
        return await run_check(get_endpoint_or_404(endpoint_id))

    @app.post("/checks/run", response_model=list[CheckResultOut], tags=["checks"])
    async def check_all() -> list[dict[str, Any]]:
        """Check every monitored endpoint concurrently and save the results."""
        return await run_all_checks()

    @app.get(
        "/endpoints/{endpoint_id}/checks", response_model=list[CheckResultOut], tags=["checks"]
    )
    def check_history(
        endpoint_id: int, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Saved check results, newest first."""
        get_endpoint_or_404(endpoint_id)
        return db.list_results(endpoint_id, limit)

    @app.get("/status", response_model=list[EndpointStatus], tags=["checks"])
    def status_overview() -> list[dict[str, Any]]:
        """Latest result plus uptime/latency over the last 100 checks, per endpoint."""
        overview = []
        for endpoint in db.list_endpoints():
            latest = db.list_results(endpoint["id"], limit=1)
            overview.append(
                {
                    "endpoint": endpoint,
                    "latest_check": latest[0] if latest else None,
                    **db.summarize(endpoint["id"]),
                }
            )
        return overview

    return app


app = create_app()
