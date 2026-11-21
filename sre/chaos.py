"""Chaos engineering CLI and failure injection engine for AI-SRE."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.supervisor import Supervisor

DATA_DIR = REPO_ROOT / "data"
LOCK_PID_FILE = DATA_DIR / "db_lock.pid"
INVENTORY_DB = DATA_DIR / "inventory.db"


class ChaosEngine:
    """Implements the 8 failure injectors for microservices:
    1. Crash: kill -9 on PID
    2. Latency: POST /chaos latency_ms
    3. Error burst: POST /chaos error_rate
    4. Memory leak: POST /chaos leak_mb
    5. CPU hog: POST /chaos cpu_burn
    6. Bad deploy: Switch service to v2 (buggy) and restart
    7. Dependency down: Kill downstream service causing cascade
    8. DB lock: Hold SQLite write lock (BEGIN EXCLUSIVE)
    Plus: clear/reset and hidden random chaos selector.
    """

    def __init__(self, supervisor: Supervisor | None = None) -> None:
        self.supervisor = supervisor or Supervisor()
        self.registry = self.supervisor.registry
        self.repo_root = self.supervisor.repo_root

    def _get_service_port(self, service: str) -> int:
        self.supervisor.validate_service(service)
        return self.registry[service]["port"]

    def _send_chaos_http(self, service: str, payload: dict[str, Any]) -> dict[str, Any]:
        port = self._get_service_port(service)
        url = f"http://127.0.0.1:{port}/chaos"
        try:
            resp = httpx.post(url, json=payload, timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
            return {"error": f"Service returned HTTP {resp.status_code}", "body": resp.text}
        except Exception as exc:
            return {"error": f"Failed to contact {service} at {url}: {exc}"}

    def _reset_chaos_http(self, service: str) -> dict[str, Any]:
        port = self._get_service_port(service)
        url = f"http://127.0.0.1:{port}/chaos/reset"
        try:
            resp = httpx.post(url, timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
            return {"error": f"Service returned HTTP {resp.status_code}", "body": resp.text}
        except Exception as exc:
            return {"error": f"Failed to contact {service} at {url}: {exc}"}

    # 1. Crash
    def inject_crash(self, service: str = "payments") -> dict[str, Any]:
        """Crash the target service immediately with SIGKILL."""
        self.supervisor.validate_service(service)
        st = self.supervisor.status().get(service, {})
        pid = st.get("pid")
        if not pid or not self.supervisor.is_pid_alive(pid):
            # Try starting it first so we can crash it realistically
            info = self.supervisor.start_service(service)
            pid = info.pid

        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        # Update supervisor state
        info = self.supervisor.state.get(service)
        if info:
            info.status = "crashed"
            info.pid = None
            self.supervisor._save_state()

        return self.supervisor.log_change(
            action="chaos_crash",
            service=service,
            author="chaos",
            details={"killed_pid": pid, "signal": "SIGKILL", "symptom": "health check failure & upstream 5xx"},
        )

    # 2. Latency
    def inject_latency(self, service: str = "payments", latency_ms: float = 2500.0) -> dict[str, Any]:
        """Inject high processing latency causing upstream timeouts / p95 spikes."""
        self.supervisor.validate_service(service)
        res = self._send_chaos_http(service, {"latency_ms": latency_ms})
        return self.supervisor.log_change(
            action="chaos_latency",
            service=service,
            author="chaos",
            details={"latency_ms": latency_ms, "response": res, "symptom": "p95 latency spike & upstream timeouts"},
        )

    # 3. Error Burst
    def inject_error_burst(self, service: str = "payments", error_rate: float = 0.5) -> dict[str, Any]:
        """Inject random 5xx error burst on service requests."""
        self.supervisor.validate_service(service)
        res = self._send_chaos_http(service, {"error_rate": error_rate})
        return self.supervisor.log_change(
            action="chaos_error_burst",
            service=service,
            author="chaos",
            details={"error_rate": error_rate, "response": res, "symptom": "error rate threshold breach (>5%)"},
        )

    # 4. Memory Leak
    def inject_memory_leak(self, service: str = "orders", leak_mb: int = 150) -> dict[str, Any]:
        """Allocate heap memory to simulate a severe memory leak."""
        self.supervisor.validate_service(service)
        res = self._send_chaos_http(service, {"leak_mb": leak_mb})
        return self.supervisor.log_change(
            action="chaos_memory_leak",
            service=service,
            author="chaos",
            details={"leak_mb": leak_mb, "response": res, "symptom": "rapid RSS memory growth trend"},
        )

    # 5. CPU Hog
    def inject_cpu_hog(self, service: str = "gateway") -> dict[str, Any]:
        """Spawn busy-loop calculation threads consuming high CPU."""
        self.supervisor.validate_service(service)
        res = self._send_chaos_http(service, {"cpu_burn": True})
        return self.supervisor.log_change(
            action="chaos_cpu_hog",
            service=service,
            author="chaos",
            details={"cpu_burn": True, "response": res, "symptom": "high CPU utilization & slow request handling"},
        )

    # 6. Bad Deploy
    def inject_bad_deploy(self, service: str = "payments") -> dict[str, Any]:
        """Switch service to buggy v2 (e.g. payment connector failure) and restart."""
        self.supervisor.validate_service(service)
        ver_file = self.repo_root / "services" / service / "VERSION"
        prev_version = self.supervisor._read_version(service)
        ver_file.write_text("2.0.0\n", encoding="utf-8")

        # Restart service under v2
        restarted = self.supervisor.restart_service(service)

        return self.supervisor.log_change(
            action="bad_deploy",
            service=service,
            author="deploy_pipeline",
            details={
                "from_version": prev_version,
                "to_version": "2.0.0",
                "new_pid": restarted.pid,
                "symptom": "immediate 500 errors starting right after deploy",
            },
        )

    # 7. Dependency Down
    def inject_dependency_down(self, downstream_service: str = "inventory") -> dict[str, Any]:
        """Stop a downstream dependency to trigger cascading failures in upstream callers."""
        self.supervisor.validate_service(downstream_service)
        st = self.supervisor.stop_service(downstream_service)
        return self.supervisor.log_change(
            action="chaos_dependency_down",
            service=downstream_service,
            author="chaos",
            details={"stopped_service": downstream_service, "symptom": "cascade to upstream orders/gateway callers"},
        )

    # 8. DB Lock
    def inject_db_lock(self, duration_seconds: float = 30.0) -> dict[str, Any]:
        """Acquire and hold an exclusive SQLite write lock on data/inventory.db."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(INVENTORY_DB)

        # Release any existing lock process first
        self.release_db_lock()

        # Launch an independent Python process that holds the lock
        code = f"""
import sqlite3, time, sys
conn = sqlite3.connect('{db_path}', timeout=0.1)
conn.execute('BEGIN EXCLUSIVE;')
time.sleep({duration_seconds})
conn.close()
"""
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        LOCK_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

        return self.supervisor.log_change(
            action="chaos_db_lock",
            service="inventory",
            author="chaos",
            details={
                "lock_pid": proc.pid,
                "db": db_path,
                "duration_seconds": duration_seconds,
                "symptom": "sqlite3.OperationalError: database is locked & inventory timeouts",
            },
        )

    def release_db_lock(self) -> bool:
        """Release any active SQLite write lock held by the background process."""
        if LOCK_PID_FILE.is_file():
            try:
                pid = int(LOCK_PID_FILE.read_text(encoding="utf-8").strip())
                if self.supervisor.is_pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
                LOCK_PID_FILE.unlink(missing_ok=True)
                return True
            except (ValueError, OSError):
                LOCK_PID_FILE.unlink(missing_ok=True)
        return False

    # Clear / Reset
    def clear_all(self) -> dict[str, Any]:
        """Reset all chaos settings, restore versions to 1.0.0, release locks, and ensure all services run."""
        results = {}

        # 1. Release DB lock
        db_released = self.release_db_lock()
        results["db_lock_released"] = db_released

        # 2. Reset chaos HTTP & restore versions
        for svc in self.registry:
            # Restore VERSION to 1.0.0 if changed
            ver_file = self.repo_root / "services" / svc / "VERSION"
            if ver_file.is_file():
                current_ver = ver_file.read_text(encoding="utf-8").strip()
                if current_ver != "1.0.0":
                    ver_file.write_text("1.0.0\n", encoding="utf-8")
                    self.supervisor.restart_service(svc)

            # Reset HTTP chaos
            resp = self._reset_chaos_http(svc)
            results[svc] = resp

            # If stopped or crashed, bring it back up
            st = self.supervisor.status().get(svc, {})
            if st.get("status") in ("stopped", "crashed", "unhealthy"):
                self.supervisor.start_service(svc)

        change_entry = self.supervisor.log_change(
            action="chaos_clear",
            service="all",
            author="operator",
            details=results,
        )
        return change_entry

    def clear_service(self, service: str) -> dict[str, Any]:
        """Clear chaos on a specific service."""
        self.supervisor.validate_service(service)
        # Restore version if needed
        ver_file = self.repo_root / "services" / service / "VERSION"
        if ver_file.is_file():
            current_ver = ver_file.read_text(encoding="utf-8").strip()
            if current_ver != "1.0.0":
                ver_file.write_text("1.0.0\n", encoding="utf-8")
                self.supervisor.restart_service(service)

        resp = self._reset_chaos_http(service)
        # Ensure running
        st = self.supervisor.status().get(service, {})
        if st.get("status") in ("stopped", "crashed", "unhealthy"):
            self.supervisor.start_service(service)

        return self.supervisor.log_change(
            action="chaos_clear",
            service=service,
            author="operator",
            details=resp,
        )

    # Random selector for live judging rehearsal
    def inject_random(self) -> dict[str, Any]:
        """Pick a random failure type from the 8 available and inject it."""
        failures = [
            ("crash", lambda: self.inject_crash(random.choice(["payments", "inventory", "orders"]))),
            ("latency", lambda: self.inject_latency(random.choice(["payments", "inventory"]), latency_ms=random.choice([1500.0, 2500.0, 3500.0]))),
            ("error_burst", lambda: self.inject_error_burst(random.choice(["payments", "orders"]), error_rate=random.choice([0.3, 0.6, 1.0]))),
            ("memory_leak", lambda: self.inject_memory_leak("orders", leak_mb=random.choice([100, 200]))),
            ("cpu_hog", lambda: self.inject_cpu_hog(random.choice(["gateway", "orders"]))),
            ("bad_deploy", lambda: self.inject_bad_deploy("payments")),
            ("dependency_down", lambda: self.inject_dependency_down(random.choice(["payments", "inventory"]))),
            ("db_lock", lambda: self.inject_db_lock(duration_seconds=30.0)),
        ]
        name, fn = random.choice(failures)
        res = fn()
        res["chosen_failure"] = name
        return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Chaos Injection CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Crash
    p_crash = subparsers.add_parser("crash", help="Crash a service with SIGKILL")
    p_crash.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service to crash")
    p_crash.add_argument("--service", default=None, help="Service to crash (default: payments)")

    # 2. Latency
    p_lat = subparsers.add_parser("latency", help="Inject latency on a service")
    p_lat.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service name")
    p_lat.add_argument("ms_pos", nargs="?", type=float, default=None, metavar="ms", help="Latency in ms")
    p_lat.add_argument("--service", default=None, help="Service name")
    p_lat.add_argument("--ms", type=float, default=2500.0, help="Latency in milliseconds")

    # 3. Error burst
    p_err = subparsers.add_parser("error-burst", aliases=["errors"], help="Inject 5xx error burst")
    p_err.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service name")
    p_err.add_argument("rate_pos", nargs="?", type=float, default=None, metavar="rate", help="Error rate (0.0 to 1.0)")
    p_err.add_argument("--service", default=None, help="Service name")
    p_err.add_argument("--rate", type=float, default=0.5, help="Error rate (0.0 to 1.0)")

    # 4. Memory leak
    p_leak = subparsers.add_parser("memory-leak", aliases=["leak"], help="Simulate a memory leak")
    p_leak.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service name")
    p_leak.add_argument("mb_pos", nargs="?", type=int, default=None, metavar="mb", help="MB to leak")
    p_leak.add_argument("--service", default=None, help="Service name")
    p_leak.add_argument("--mb", type=int, default=150, help="MB to leak")

    # 5. CPU hog
    p_cpu = subparsers.add_parser("cpu-hog", aliases=["cpu"], help="Consume high CPU with busy calculations")
    p_cpu.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service name")
    p_cpu.add_argument("--service", default=None, help="Service name")

    # 6. Bad deploy
    p_dep = subparsers.add_parser("bad-deploy", help="Deploy buggy v2 version of a service")
    p_dep.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service to deploy v2")
    p_dep.add_argument("--service", default=None, help="Service to deploy v2")

    # 7. Dependency down
    p_down = subparsers.add_parser("dependency-down", help="Stop a downstream dependency")
    p_down.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Downstream service to stop")
    p_down.add_argument("--service", default=None, help="Downstream service to stop")

    # 8. DB lock
    p_lock = subparsers.add_parser("db-lock", aliases=["dblock"], help="Hold SQLite exclusive write lock")
    p_lock.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service or duration")
    p_lock.add_argument("duration_pos", nargs="?", type=float, default=None, metavar="duration", help="Lock duration in seconds")
    p_lock.add_argument("--duration", type=float, default=30.0, help="Lock duration in seconds")

    # Clear / Reset
    p_clear = subparsers.add_parser("clear", help="Clear chaos and restore healthy baseline")
    p_clear.add_argument("service_pos", nargs="?", default=None, metavar="service", help="Service name or 'all'")
    p_clear.add_argument("--service", default=None, help="Specific service or omit for all")

    # Random selector
    subparsers.add_parser("random", help="Inject a random failure from the 8 failure injectors")

    args = parser.parse_args(argv)
    engine = ChaosEngine()

    # Normalize positional arguments
    svc = getattr(args, "service", None) or getattr(args, "service_pos", None)
    if svc == "all":
        svc = None

    try:
        if args.command == "crash":
            res = engine.inject_crash(svc or "payments")
            print(f"[CHAOS] Crashed service {svc or 'payments'}: {res}")
        elif args.command == "latency":
            ms_val = getattr(args, "ms_pos", None) or getattr(args, "ms", 2500.0)
            res = engine.inject_latency(svc or "payments", latency_ms=float(ms_val))
            print(f"[CHAOS] Injected {ms_val}ms latency on {svc or 'payments'}: {res}")
        elif args.command in ("error-burst", "errors"):
            rate_val = getattr(args, "rate_pos", None) or getattr(args, "rate", 0.5)
            res = engine.inject_error_burst(svc or "payments", error_rate=float(rate_val))
            print(f"[CHAOS] Injected error rate {rate_val} on {svc or 'payments'}: {res}")
        elif args.command in ("memory-leak", "leak"):
            mb_val = getattr(args, "mb_pos", None) or getattr(args, "mb", 150)
            res = engine.inject_memory_leak(svc or "orders", leak_mb=int(mb_val))
            print(f"[CHAOS] Injected {mb_val}MB memory leak on {svc or 'orders'}: {res}")
        elif args.command in ("cpu-hog", "cpu"):
            res = engine.inject_cpu_hog(svc or "gateway")
            print(f"[CHAOS] Injected CPU hog on {svc or 'gateway'}: {res}")
        elif args.command == "bad-deploy":
            res = engine.inject_bad_deploy(svc or "payments")
            print(f"[CHAOS] Deployed buggy v2 on {svc or 'payments'}: {res}")
        elif args.command == "dependency-down":
            res = engine.inject_dependency_down(svc or "inventory")
            print(f"[CHAOS] Stopped dependency {svc or 'inventory'}: {res}")
        elif args.command in ("db-lock", "dblock"):
            dur_val = getattr(args, "duration_pos", None) or getattr(args, "duration", 30.0)
            res = engine.inject_db_lock(float(dur_val))
            print(f"[CHAOS] SQLite write lock held for {dur_val}s: {res}")
        elif args.command == "clear":
            if svc:
                res = engine.clear_service(svc)
            else:
                res = engine.clear_all()
            print(f"[CHAOS] Chaos cleared: {res}")
        elif args.command == "random":
            res = engine.inject_random()
            print(f"[CHAOS RANDOM] Injected {res.get('chosen_failure')}: {res}")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
