#!/usr/bin/env python3
"""Steady traffic generator for the AI-SRE microservices application.

Sends steady simulated traffic to the API gateway so metrics and logs are never empty.
Supports configuring request rate, duration, target endpoints, and reporting stats.

Usage:
    python load.py                     # Runs indefinitely at 2.0 req/sec
    python load.py --rate 5.0          # 5 req/sec
    python load.py --count 100         # Send 100 requests and stop
    python load.py --duration 30       # Run for 30 seconds and stop
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8000/orders"
ITEMS = ["item_1", "item_2", "item_3"]
CUSTOMERS = [f"cust_{i}" for i in range(1, 21)]


@dataclass
class LoadStats:
    total_sent: int = 0
    total_success: int = 0
    total_errors: int = 0
    errors_by_status: dict[int, int] = field(default_factory=dict)
    connection_errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

    def record_success(self, latency_ms: float) -> None:
        self.total_sent += 1
        self.total_success += 1
        self.latencies_ms.append(latency_ms)

    def record_error(self, status: int | None, latency_ms: float = 0.0) -> None:
        self.total_sent += 1
        self.total_errors += 1
        if status is not None:
            self.errors_by_status[status] = self.errors_by_status.get(status, 0) + 1
        else:
            self.connection_errors += 1
        if latency_ms > 0:
            self.latencies_ms.append(latency_ms)

    def compute_percentiles(self) -> dict[str, float]:
        if not self.latencies_ms:
            return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "max": 0.0}
        s = sorted(self.latencies_ms)
        n = len(s)

        def pct(p: float) -> float:
            idx = min(int(math.ceil(p * n) - 1), n - 1)
            return round(s[max(0, idx)], 2)

        return {
            "avg": round(sum(s) / n, 2),
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": round(s[-1], 2),
        }

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        rps = (self.total_sent / elapsed) if elapsed > 0 else 0.0
        pcts = self.compute_percentiles()
        err_breakdown = ", ".join(f"{st}: {ct}" for st, ct in sorted(self.errors_by_status.items()))
        if self.connection_errors:
            err_breakdown += f", conn_err: {self.connection_errors}"
        if not err_breakdown:
            err_breakdown = "none"

        return (
            f"Elapsed: {elapsed:.1f}s | Sent: {self.total_sent} | "
            f"Success: {self.total_success} | Errors: {self.total_errors} ({err_breakdown}) | "
            f"Rate: {rps:.1f} rps | Latency p50: {pcts['p50']}ms, p95: {pcts['p95']}ms, max: {pcts['max']}ms"
        )


class LoadGenerator:
    """Sends steady synthetic order traffic to the microservices gateway."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        rate: float = 2.0,
        duration: float | None = None,
        count: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.rate = max(0.1, rate)
        self.duration = duration
        self.count = count
        self.timeout = timeout
        self.stats = LoadStats()
        self._running = False

    async def _send_one(self, client: httpx.AsyncClient) -> None:
        payload = {
            "customer_id": random.choice(CUSTOMERS),
            "item_id": random.choice(ITEMS),
            "quantity": random.randint(1, 3),
        }
        t0 = time.perf_counter()
        try:
            resp = await client.post(self.url, json=payload)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status_code == 200:
                self.stats.record_success(lat_ms)
            else:
                self.stats.record_error(resp.status_code, lat_ms)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            lat_ms = (time.perf_counter() - t0) * 1000.0
            self.stats.record_error(None, lat_ms)
        except Exception:
            lat_ms = (time.perf_counter() - t0) * 1000.0
            self.stats.record_error(None, lat_ms)

    async def run(self) -> LoadStats:
        self._running = True
        self.stats = LoadStats(start_time=time.time())
        interval = 1.0 / self.rate
        last_report = time.time()

        print(f"Starting load generator targeting {self.url} at {self.rate} req/s...")
        if self.duration:
            print(f"Target duration: {self.duration} seconds")
        if self.count:
            print(f"Target request count: {self.count}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while self._running:
                now = time.time()
                if self.duration and (now - self.stats.start_time) >= self.duration:
                    break
                if self.count and self.stats.total_sent >= self.count:
                    break

                t_start = time.perf_counter()
                await self._send_one(client)

                if time.time() - last_report >= 5.0:
                    print(f"  [Progress] {self.stats.summary()}")
                    last_report = time.time()

                sleep_time = interval - (time.perf_counter() - t_start)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        self._running = False
        print(f"\nFinal Load Summary: {self.stats.summary()}")
        return self.stats

    def stop(self) -> None:
        self._running = False


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Demo Load Generator")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Target URL (default: {DEFAULT_URL})")
    parser.add_argument("--rate", type=float, default=2.0, help="Requests per second (default: 2.0)")
    parser.add_argument("--duration", type=float, default=None, help="Run duration in seconds")
    parser.add_argument("--count", type=int, default=None, help="Stop after sending N requests")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP request timeout in seconds")
    args = parser.parse_args()

    generator = LoadGenerator(
        url=args.url,
        rate=args.rate,
        duration=args.duration,
        count=args.count,
        timeout=args.timeout,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal(*_: Any) -> None:
        print("\nStopping load generator...")
        generator.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, AttributeError):
            pass

    try:
        stats = loop.run_until_complete(generator.run())
        return 0 if stats.total_errors == 0 else 1
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
