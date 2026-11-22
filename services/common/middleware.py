"""Middleware handling trace ID propagation, request timing, Prometheus metrics, and chaos."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from services.common.chaos import ChaosState
from services.common.logging import current_trace_id, set_current_trace_id
from services.common.metrics import ServiceMetrics


class TraceAndChaosMiddleware(BaseHTTPMiddleware):
    """Integrates trace ID propagation, chaos injection, metrics, and structured access logging."""

    def __init__(
        self,
        app: Any,
        service_name: str,
        logger: logging.Logger,
        metrics: ServiceMetrics,
        chaos: ChaosState,
    ) -> None:
        super().__init__(app)
        self.service_name = service_name
        self.logger = logger
        self.metrics = metrics
        self.chaos = chaos

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 1. Trace ID extraction or generation
        raw_trace_id = (
            request.headers.get("x-trace-id")
            or request.headers.get("traceparent")
            or uuid.uuid4().hex[:16]
        )
        # Normalize trace ID (clean hex string)
        trace_id = raw_trace_id.split("-")[-1] if "-" in raw_trace_id and len(raw_trace_id) > 32 else raw_trace_id
        set_current_trace_id(trace_id)

        path = request.url.path
        method = request.method

        # 2. Endpoints exempt from chaos (metrics, chaos control)
        is_exempt = path.startswith("/metrics") or path.startswith("/chaos") or path.startswith("/docs") or path.startswith("/openapi.json")

        t0 = time.perf_counter()

        # 3. Health check specific chaos handling
        if path == "/health" and self.chaos.unhealthy:
            duration_s = time.perf_counter() - t0
            latency_ms = duration_s * 1000.0
            self.metrics.record_request(method, path, 503, duration_s)
            self._log_request(method, path, 503, latency_ms, trace_id, "Health check failed (chaos injected)")
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "service": self.service_name, "chaos": True},
                headers={"X-Trace-Id": trace_id},
            )

        # 4. General chaos injection (latency and error rate) for non-exempt endpoints
        if not is_exempt:
            if self.chaos.latency_ms > 0:
                await asyncio.sleep(self.chaos.latency_ms / 1000.0)

            if self.chaos.error_rate > 0 and random.random() < self.chaos.error_rate:
                duration_s = time.perf_counter() - t0
                latency_ms = duration_s * 1000.0
                self.metrics.record_request(method, path, 500, duration_s)
                self._log_request(method, path, 500, latency_ms, trace_id, "Chaos injected error burst")
                return JSONResponse(
                    status_code=500,
                    content={"error": "Chaos injected error", "service": self.service_name, "chaos": True},
                    headers={"X-Trace-Id": trace_id},
                )

        # 5. Process downstream request
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_s = time.perf_counter() - t0
            latency_ms = duration_s * 1000.0
            self.metrics.record_request(method, path, 500, duration_s)
            self._log_request(method, path, 500, latency_ms, trace_id, f"Unhandled exception: {exc}")
            raise

        duration_s = time.perf_counter() - t0
        latency_ms = duration_s * 1000.0

        # 6. Record metrics and attach trace ID to outgoing response
        self.metrics.record_request(method, path, response.status_code, duration_s)
        response.headers["X-Trace-Id"] = trace_id

        # 7. Write structured access log
        msg = f"{method} {path} -> {response.status_code} ({latency_ms:.1f}ms)"
        self._log_request(method, path, response.status_code, latency_ms, trace_id, msg)

        return response

    def _log_request(
        self,
        method: str,
        path: str,
        status: int,
        latency_ms: float,
        trace_id: str,
        msg: str,
    ) -> None:
        """Write structured access log adhering to Phase 1 JSON schema.

        Filters out routine successful polling noise (e.g. GET /health 200, GET /metrics 200)
        so only meaningful operational, business, and error logs are retained.
        """
        # Save only important logs: skip routine successful telemetry/docs polling
        if status < 400 and path in ("/health", "/metrics", "/docs", "/openapi.json", "/favicon.ico"):
            return

        level = logging.INFO
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING

        self.logger.log(
            level,
            msg,
            extra={
                "trace_id": trace_id,
                "latency_ms": latency_ms,
                "status": status,
                "extra_data": {
                    "method": method,
                    "path": path,
                },
            },
        )
