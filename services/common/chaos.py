"""Chaos injection state and management for microservices."""

from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("sre.chaos")


class ChaosState:
    """Manages dynamic chaos state for a service."""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self.latency_ms: float = 0.0
        self.error_rate: float = 0.0
        self.leak_mb_total: int = 0
        self.cpu_burn: bool = False
        self.unhealthy: bool = False

        self._lock = threading.Lock()
        self._leak_chunks: list[bytearray] = []
        self._cpu_thread: threading.Thread | None = None
        self._stop_cpu_event = threading.Event()

    def update(
        self,
        latency_ms: float | None = None,
        error_rate: float | None = None,
        leak_mb: int | None = None,
        cpu_burn: bool | None = None,
        unhealthy: bool | None = None,
    ) -> dict[str, Any]:
        """Update chaos settings and activate requested failure injectors."""
        with self._lock:
            if latency_ms is not None:
                self.latency_ms = max(0.0, float(latency_ms))

            if error_rate is not None:
                self.error_rate = max(0.0, min(1.0, float(error_rate)))

            if unhealthy is not None:
                self.unhealthy = bool(unhealthy)

            if leak_mb is not None and leak_mb > 0:
                mb = int(leak_mb)
                try:
                    # Allocate memory chunk to increase RSS
                    chunk = bytearray(mb * 1024 * 1024)
                    # Touch every 4KB page so memory is actually faulted in
                    for i in range(0, len(chunk), 4096):
                        chunk[i] = 1
                    self._leak_chunks.append(chunk)
                    self.leak_mb_total += mb
                except MemoryError:
                    logger.error(f"Failed to allocate {mb}MB memory chunk for chaos leak")

            if cpu_burn is not None:
                if cpu_burn and not self.cpu_burn:
                    self._start_cpu_burn()
                elif not cpu_burn and self.cpu_burn:
                    self._stop_cpu_burn()

        return self.to_dict()

    def _start_cpu_burn(self) -> None:
        self.cpu_burn = True
        self._stop_cpu_event.clear()

        def _worker() -> None:
            while not self._stop_cpu_event.is_set():
                # Perform continuous busy calculations
                _ = sum(i * i for i in range(50000))

        self._cpu_thread = threading.Thread(
            target=_worker,
            name=f"cpu_burn_{self.service_name}",
            daemon=True,
        )
        self._cpu_thread.start()

    def _stop_cpu_burn(self) -> None:
        self.cpu_burn = False
        self._stop_cpu_event.set()
        if self._cpu_thread and self._cpu_thread.is_alive():
            self._cpu_thread.join(timeout=1.0)
        self._cpu_thread = None

    def reset(self) -> dict[str, Any]:
        """Reset all chaos settings to baseline healthy operation."""
        with self._lock:
            self.latency_ms = 0.0
            self.error_rate = 0.0
            self.unhealthy = False
            self._stop_cpu_burn()
            self._leak_chunks.clear()
            self.leak_mb_total = 0
            gc.collect()

        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        """Return the current chaos configuration."""
        return {
            "service": self.service_name,
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "leak_mb_total": self.leak_mb_total,
            "cpu_burn": self.cpu_burn,
            "unhealthy": self.unhealthy,
        }
