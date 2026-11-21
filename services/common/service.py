"""Factory for creating microservice FastAPI applications with standard endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Response
from pydantic import BaseModel, Field

from services.common.chaos import ChaosState
from services.common.logging import setup_logger
from services.common.metrics import ServiceMetrics
from services.common.middleware import TraceAndChaosMiddleware


def get_service_version(service_name: str) -> str:
    """Read the service version from environment variable or services/<service>/VERSION file."""
    # 1. Environment variable override
    env_key = f"{service_name.upper()}_VERSION"
    if os.getenv(env_key):
        return os.getenv(env_key, "").strip()
    if os.getenv("SERVICE_VERSION"):
        return os.getenv("SERVICE_VERSION", "").strip()

    # 2. VERSION file
    repo_root = Path(__file__).resolve().parent.parent.parent
    version_file = repo_root / "services" / service_name / "VERSION"
    if version_file.is_file():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    return "1.0.0"


class ChaosUpdateRequest(BaseModel):
    latency_ms: float | None = Field(default=None, description="Injected latency in milliseconds", ge=0.0)
    error_rate: float | None = Field(default=None, description="Error rate between 0.0 and 1.0", ge=0.0, le=1.0)
    leak_mb: int | None = Field(default=None, description="Memory leak in MB", ge=0)
    cpu_burn: bool | None = Field(default=None, description="Enable or disable CPU burn loop")
    unhealthy: bool | None = Field(default=None, description="Force /health to report 503 unhealthy")


def create_service_app(
    service_name: str,
    port: int,
    description: str = "",
) -> tuple[FastAPI, ServiceMetrics, ChaosState]:
    """Create a FastAPI application configured with health, metrics, chaos, and middleware."""
    app = FastAPI(
        title=f"AI-SRE {service_name.capitalize()} Service",
        description=description or f"Microservice {service_name} running on port {port}",
        version=get_service_version(service_name),
    )

    logger = setup_logger(service_name)
    metrics = ServiceMetrics(service_name)
    chaos = ChaosState(service_name)

    # Attach shared state to app.state
    app.state.service_name = service_name
    app.state.service_port = port
    app.state.logger = logger
    app.state.metrics = metrics
    app.state.chaos = chaos

    # Register middleware
    app.add_middleware(
        TraceAndChaosMiddleware,
        service_name=service_name,
        logger=logger,
        metrics=metrics,
        chaos=chaos,
    )

    # Standard endpoints required on EVERY service
    @app.get("/health", summary="Service health check")
    async def health() -> dict[str, Any]:
        version = get_service_version(service_name)
        return {
            "status": "ok",
            "service": service_name,
            "version": version,
            "port": port,
        }

    @app.get("/metrics", summary="Prometheus metrics")
    async def prometheus_metrics() -> Response:
        content = metrics.generate_metrics()
        return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/chaos", summary="Get active chaos injection settings")
    async def get_chaos() -> dict[str, Any]:
        return chaos.to_dict()

    @app.post("/chaos", summary="Update chaos injection settings")
    async def set_chaos(req: ChaosUpdateRequest) -> dict[str, Any]:
        res = chaos.update(
            latency_ms=req.latency_ms,
            error_rate=req.error_rate,
            leak_mb=req.leak_mb,
            cpu_burn=req.cpu_burn,
            unhealthy=req.unhealthy,
        )
        logger.info(f"Chaos state updated: {res}")
        return res

    @app.post("/chaos/reset", summary="Reset chaos to healthy state")
    @app.delete("/chaos", summary="Reset chaos to healthy state")
    async def reset_chaos() -> dict[str, Any]:
        res = chaos.reset()
        logger.info(f"Chaos state reset to normal: {res}")
        return res

    return app, metrics, chaos
