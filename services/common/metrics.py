"""Prometheus metrics registry and collectors for microservices."""

from __future__ import annotations

import resource
import sys
import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Histogram, Gauge


class ServiceMetrics:
    """Encapsulates Prometheus metrics for an individual service with its own registry.
    
    Using a dedicated CollectorRegistry per service avoids duplicate registration
    collisions when multiple services run or are tested in the same process.
    """

    def __init__(self, service_name: str, registry: CollectorRegistry | None = None) -> None:
        self.service_name = service_name
        self.registry = registry or CollectorRegistry()

        self.requests_total = Counter(
            "http_requests_total",
            "Total HTTP requests handled by the service.",
            ["service", "method", "endpoint", "status"],
            registry=self.registry,
        )

        self.errors_total = Counter(
            "http_errors_total",
            "Total HTTP errors (4xx/5xx) emitted by the service.",
            ["service", "method", "endpoint", "status"],
            registry=self.registry,
        )

        self.request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency histogram in seconds.",
            ["service", "endpoint"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )

        # Process metrics for memory (RSS) and CPU
        try:
            self.process_resident_memory_bytes = Gauge(
                "process_resident_memory_bytes",
                "Resident memory size in bytes.",
                registry=self.registry,
            )
            scale = 1 if sys.platform == "darwin" else 1024
            self.process_resident_memory_bytes.set_function(
                lambda: float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale)
            )
            self.process_cpu_seconds_total = Gauge(
                "process_cpu_seconds_total",
                "Total user and system CPU time spent in seconds.",
                registry=self.registry,
            )
            self.process_cpu_seconds_total.set_function(
                lambda: float(resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime)
            )
        except Exception:
            pass

    def record_request(
        self,
        method: str,
        endpoint: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        """Record counters and histogram for a completed HTTP request."""
        status_str = str(status)
        self.requests_total.labels(
            service=self.service_name,
            method=method,
            endpoint=endpoint,
            status=status_str,
        ).inc()

        if status >= 400:
            self.errors_total.labels(
                service=self.service_name,
                method=method,
                endpoint=endpoint,
                status=status_str,
            ).inc()

        self.request_duration_seconds.labels(
            service=self.service_name,
            endpoint=endpoint,
        ).observe(duration_seconds)

    def generate_metrics(self) -> bytes:
        """Export metrics formatted for Prometheus scrapers."""
        return prometheus_client.generate_latest(self.registry)
