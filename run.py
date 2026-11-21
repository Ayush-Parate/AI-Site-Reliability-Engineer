#!/usr/bin/env python3
"""Single entry point for the AI-SRE demo.

Usage:
    python run.py                # start all 4 microservices + load generator
    python run.py --services     # start only the 4 microservices
    python run.py --status       # print environment / provider status
    python run.py --test-llm     # send a test call to each configured provider
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sre import __version__
from sre.llm import LLM, LLMConfigError, PROVIDERS

REPO_ROOT = Path(__file__).resolve().parent

SERVICES = [
    ("inventory", 8003, "services.inventory.main:app"),
    ("payments", 8002, "services.payments.main:app"),
    ("orders", 8001, "services.orders.main:app"),
    ("gateway", 8000, "services.gateway.main:app"),
]


def _print_status() -> None:
    print(f"AI-SRE v{__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    llm = LLM()
    print("LLM provider order (primary -> backup):")
    for i, name in enumerate(llm.order):
        role = "primary" if i == 0 else "backup"
        cfg = PROVIDERS[name]
        state = "configured" if llm.is_configured(name) else f"missing {cfg.api_key_env}"
        print(f"  {role:7} {name:11} model={cfg.model:28} [{state}]")
    print()


def _run_llm_test() -> int:
    print("Testing each configured provider...\n")
    results = LLM().test()
    ok_all = True
    for name, res in results.items():
        if res["ok"]:
            print(f"  [PASS] {name}: {res['text']!r} (model={res.get('model')})")
        else:
            ok_all = False
            print(f"  [FAIL] {name}: {res['error']}")
    print()
    if not any(r["ok"] for r in results.values()):
        print("No provider returned text. Set a key in .env or start Ollama.")
        return 1
    if not ok_all:
        print("Some providers failed; at least one works, which is enough to run.")
    return 0


def _run_services(with_load: bool = True) -> int:
    print(f"==================================================")
    print(f" Starting AI-SRE Microservices Demo App")
    print(f"==================================================")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    python_bin = sys.executable

    processes: list[subprocess.Popen] = []

    def _cleanup(*_: Any) -> None:
        print("\nStopping all services...")
        for p in processes:
            p.terminate()
            try:
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
        print("All processes stopped.")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _cleanup)

    try:
        for name, port, app_target in SERVICES:
            cmd = [
                python_bin,
                "-m",
                "uvicorn",
                app_target,
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
                "--log-level",
                "info",
            ]
            print(f"  -> Starting {name:<10} on http://localhost:{port}")
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env)
            processes.append(proc)
            time.sleep(0.4)

        print("\nAll 4 services started successfully:")
        print("  - Gateway:   http://localhost:8000 (Health: /health, Metrics: /metrics, Chaos: /chaos)")
        print("  - Orders:    http://localhost:8001")
        print("  - Payments:  http://localhost:8002")
        print("  - Inventory: http://localhost:8003")
        print("  - Logs:      logs/<service>.log")
        print("  - DB:        data/observability.db")

        # Start observability metric collector (every 5 seconds into SQLite)
        print("\nStarting observability metric collector (5s interval)...")
        cmd_col = [python_bin, "sre/observability.py", "collect", "--interval", "5.0"]
        proc_col = subprocess.Popen(cmd_col, cwd=str(REPO_ROOT), env=env)
        processes.append(proc_col)

        if with_load:
            print("\nStarting steady traffic generator (2.0 req/s to gateway)...")
            cmd_load = [python_bin, "load.py", "--rate", "2.0"]
            proc_load = subprocess.Popen(cmd_load, cwd=str(REPO_ROOT), env=env)
            processes.append(proc_load)

        print("\nSystem running. Press Ctrl+C to stop.\n")

        # Keep parent alive while sub-processes run
        while True:
            for p in processes:
                code = p.poll()
                if code is not None and code != 0:
                    print(f"Process {p.pid} exited with code {code}")
                    return code
            time.sleep(1.0)

    except KeyboardInterrupt:
        _cleanup()
        return 0
    except Exception as exc:
        print(f"Error while running services: {exc}", file=sys.stderr)
        _cleanup()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Site Reliability Engineer")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print environment / LLM provider configuration status.",
    )
    parser.add_argument(
        "--test-llm",
        action="store_true",
        help="Send a test call to each configured LLM provider and exit.",
    )
    parser.add_argument(
        "--services",
        action="store_true",
        help="Run only the 4 microservices without the load generator.",
    )
    parser.add_argument(
        "--obs",
        action="store_true",
        help="Show observability status (metrics, logs, health, dependencies).",
    )
    args = parser.parse_args(argv)

    try:
        if args.test_llm:
            return _run_llm_test()
        if args.status:
            _print_status()
            return 0
        if args.obs:
            from sre.observability import get_observability
            obs = get_observability()
            hlth = obs.check_health()
            print("--- Service Health ---")
            for s, d in hlth.get("services", {}).items():
                print(f"  {s:<12}: healthy={d['healthy']}, status={d['status']}")
            deps = obs.get_deps()
            print("\n--- Dependencies ---")
            for s, d in deps.get("services", {}).items():
                print(f"  {s:<12}: upstream={d['upstream']}, downstream={d['downstream']}")
            logs = obs.get_logs(limit=5)
            print(f"\n--- Recent Logs ({len(logs)} shown) ---")
            for l in logs:
                print(f"  [{l['id']}] {l['ts']} [{l['level']}] {l['service']}: {l['msg']}")
            return 0
        return _run_services(with_load=not args.services)
    except LLMConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())