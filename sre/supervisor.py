"""Service supervisor for managing microservices lifecycle and audit logging."""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
CHANGES_LOG = DATA_DIR / "changes.jsonl"
STATE_FILE = DATA_DIR / "supervisor_state.json"
DEPS_FILE = CONFIG_DIR / "dependencies.yaml"


@dataclass
class ServiceInfo:
    name: str
    port: int
    pid: int | None = None
    start_time: str | None = None
    version: str = "1.0.0"
    status: str = "stopped"  # stopped, running, unhealthy, crashed


class Supervisor:
    """Tracks each service PID, start time, version, and status.

    Exposes start, stop, restart(service), and status().
    Writes every lifecycle event to data/changes.jsonl.
    Validates service names against the registry before any action.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        changes_log: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.data_dir = self.repo_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.changes_log = Path(changes_log or (self.data_dir / "changes.jsonl"))
        self.state_file = Path(state_file or (self.data_dir / "supervisor_state.json"))
        self.registry = self._load_registry()
        self.state = self._load_state()

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        deps_path = self.repo_root / "config" / "dependencies.yaml"
        if deps_path.is_file():
            with open(deps_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                return data.get("services", {})
        return {
            "gateway": {"port": 8000, "calls": ["orders"]},
            "orders": {"port": 8001, "calls": ["payments", "inventory"]},
            "payments": {"port": 8002, "calls": []},
            "inventory": {"port": 8003, "calls": []},
        }

    def _load_state(self) -> dict[str, ServiceInfo]:
        if self.state_file.is_file():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                state = {}
                for name, d in raw.items():
                    if name in self.registry:
                        state[name] = ServiceInfo(**d)
                return state
            except (json.JSONDecodeError, OSError):
                pass

        # Default empty state matching registry
        state = {}
        for name, cfg in self.registry.items():
            state[name] = ServiceInfo(
                name=name,
                port=cfg.get("port", 8000),
                version=self._read_version(name),
            )
        return state

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        raw = {k: asdict(v) for k, v in self.state.items()}
        self.state_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    def _read_version(self, service: str) -> str:
        ver_file = self.repo_root / "services" / service / "VERSION"
        if ver_file.is_file():
            try:
                return ver_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        return "1.0.0"

    def log_change(
        self,
        action: str,
        service: str,
        details: dict[str, Any] | None = None,
        author: str = "supervisor",
    ) -> dict[str, Any]:
        """Record an event to data/changes.jsonl with timestamp and evidence ID."""
        self.changes_log.parent.mkdir(parents=True, exist_ok=True)
        now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        # Read existing line count to create stable sequential change ID
        count = 1
        if self.changes_log.is_file():
            try:
                with open(self.changes_log, "r", encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip()) + 1
            except OSError:
                count = 1

        entry = {
            "id": f"CHG-{count}",
            "ts": now_ts,
            "action": action,
            "service": service,
            "author": author,
            "details": details or {},
        }
        with open(self.changes_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def validate_service(self, service: str) -> None:
        if service not in self.registry:
            valid = ", ".join(sorted(self.registry.keys()))
            raise ValueError(f"Unknown service '{service}'. Valid services: {valid}")

    def is_pid_alive(self, pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def check_health(self, service: str, timeout: float = 1.0) -> bool:
        self.validate_service(service)
        port = self.registry[service]["port"]
        url = f"http://127.0.0.1:{port}/health"
        try:
            r = httpx.get(url, timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def start_service(self, service: str, timeout: float = 5.0) -> ServiceInfo:
        """Start a single service process under Uvicorn."""
        self.validate_service(service)
        info = self.state.get(service)
        if not info:
            info = ServiceInfo(name=service, port=self.registry[service]["port"])
            self.state[service] = info

        # If already running and alive, return
        if info.pid and self.is_pid_alive(info.pid):
            if self.check_health(service):
                info.status = "running"
                self._save_state()
                return info

        port = self.registry[service]["port"]
        version = self._read_version(service)
        app_target = f"services.{service}.main:app"

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.repo_root)

        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            app_target,
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.repo_root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        info.pid = proc.pid
        info.start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        info.version = version
        info.status = "starting"
        self._save_state()

        # Wait for health check
        t0 = time.time()
        healthy = False
        while time.time() - t0 < timeout:
            if self.check_health(service, timeout=0.5):
                healthy = True
                break
            time.sleep(0.2)

        info.status = "running" if healthy else ("unhealthy" if self.is_pid_alive(proc.pid) else "crashed")
        self._save_state()

        self.log_change(
            action="start",
            service=service,
            details={"pid": info.pid, "port": port, "version": version, "status": info.status},
        )
        return info

    def stop_service(self, service: str, sig: int = signal.SIGTERM) -> ServiceInfo:
        """Stop a running service process."""
        self.validate_service(service)
        info = self.state.get(service)
        if not info or not info.pid:
            if info:
                info.status = "stopped"
                self._save_state()
            return info or ServiceInfo(name=service, port=self.registry[service]["port"], status="stopped")

        old_pid = info.pid
        if self.is_pid_alive(old_pid):
            try:
                os.kill(old_pid, sig)
                # Wait briefly for graceful exit
                for _ in range(15):
                    if not self.is_pid_alive(old_pid):
                        break
                    time.sleep(0.1)
                # Force kill if still alive
                if self.is_pid_alive(old_pid):
                    os.kill(old_pid, signal.SIGKILL)
            except OSError:
                pass

        info.pid = None
        info.status = "stopped"
        self._save_state()

        self.log_change(
            action="stop",
            service=service,
            details={"old_pid": old_pid, "signal": int(sig)},
        )
        return info

    def restart_service(self, service: str) -> ServiceInfo:
        """Restart a service and update audit logs."""
        self.validate_service(service)
        self.stop_service(service)
        time.sleep(0.3)
        info = self.start_service(service)
        self.log_change(
            action="restart",
            service=service,
            details={"new_pid": info.pid, "version": info.version, "status": info.status},
        )
        return info

    def start_all(self) -> dict[str, ServiceInfo]:
        """Start all services in dependency order (payments/inventory -> orders -> gateway)."""
        # Leaf dependencies first
        order = ["payments", "inventory", "orders", "gateway"]
        res = {}
        for s in order:
            if s in self.registry:
                res[s] = self.start_service(s)
        return res

    def stop_all(self) -> dict[str, ServiceInfo]:
        """Stop all services in reverse order."""
        order = ["gateway", "orders", "inventory", "payments"]
        res = {}
        for s in order:
            if s in self.registry:
                res[s] = self.stop_service(s)
        return res

    def status(self) -> dict[str, dict[str, Any]]:
        """Return the current status of each service in the registry."""
        results = {}
        for name, cfg in self.registry.items():
            info = self.state.get(name)
            pid = info.pid if info else None
            alive = self.is_pid_alive(pid)
            healthy = self.check_health(name, timeout=0.5) if alive else False

            current_version = self._read_version(name)

            if not alive:
                current_status = "stopped"
                pid = None
            elif healthy:
                current_status = "running"
            else:
                current_status = "unhealthy"

            if info:
                info.pid = pid
                info.status = current_status
                info.version = current_version

            results[name] = {
                "service": name,
                "port": cfg["port"],
                "pid": pid,
                "start_time": info.start_time if info else None,
                "version": current_version,
                "status": current_status,
                "healthy": healthy,
                "calls": cfg.get("calls", []),
            }
        self._save_state()
        return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="AI-SRE Service Supervisor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show status of all services")
    subparsers.add_parser("start-all", help="Start all services")
    subparsers.add_parser("stop-all", help="Stop all services")

    p_start = subparsers.add_parser("start", help="Start a service")
    p_start.add_argument("service", help="Service name")

    p_stop = subparsers.add_parser("stop", help="Stop a service")
    p_stop.add_argument("service", help="Service name")

    p_restart = subparsers.add_parser("restart", help="Restart a service")
    p_restart.add_argument("service", help="Service name")

    args = parser.parse_args()
    sup = Supervisor()

    try:
        if args.command == "status":
            st = sup.status()
            print(f"{'SERVICE':<12} {'PORT':<6} {'PID':<8} {'STATUS':<10} {'VERSION':<8} HEALTH")
            print("-" * 55)
            for name, d in st.items():
                pid_str = str(d['pid']) if d['pid'] else "-"
                hlth = "OK" if d['healthy'] else "FAIL"
                print(f"{name:<12} {d['port']:<6} {pid_str:<8} {d['status']:<10} {d['version']:<8} {hlth}")
        elif args.command == "start":
            res = sup.start_service(args.service)
            print(f"Service {args.service} started: PID={res.pid}, status={res.status}")
        elif args.command == "stop":
            res = sup.stop_service(args.service)
            print(f"Service {args.service} stopped: status={res.status}")
        elif args.command == "restart":
            res = sup.restart_service(args.service)
            print(f"Service {args.service} restarted: PID={res.pid}, status={res.status}")
        elif args.command == "start-all":
            all_res = sup.start_all()
            for s, r in all_res.items():
                print(f"  {s:<12}: PID={r.pid}, status={r.status}")
        elif args.command == "stop-all":
            all_res = sup.stop_all()
            for s, r in all_res.items():
                print(f"  {s:<12}: status={r.status}")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
