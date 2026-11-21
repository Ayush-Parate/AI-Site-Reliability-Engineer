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

REPO_ROOT = Path(__file__).resolve().parent

# Auto re-exec inside .venv if run with system Python outside the virtual environment
_venv_python = REPO_ROOT / ".venv" / "bin" / "python"
if sys.prefix == getattr(sys, "base_prefix", sys.prefix) and _venv_python.exists() and sys.executable != str(_venv_python):
    os.execv(str(_venv_python), [str(_venv_python)] + sys.argv)

from sre import __version__
from sre.llm import LLM, LLMConfigError, PROVIDERS

SERVICES = [
    ("inventory", 8003, "services.inventory.main:app"),
    ("payments", 8002, "services.payments.main:app"),
    ("orders", 8001, "services.orders.main:app"),
    ("gateway", 8000, "services.gateway.main:app"),
]


def _find_dashboard_port() -> int:
    env_port = os.getenv("DASHBOARD_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    import socket
    for p in (8080, 9000, 8090, 8088):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    return 8080


def _print_status() -> None:
    print(f"AI-SRE v{__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    llm = LLM()
    cfg = PROVIDERS.get("ollama")
    if cfg:
        state = "configured" if llm.is_configured("ollama") else "unconfigured"
        print(f"LLM Provider: ollama (model={cfg.model}) [{state}] at {cfg.base_url}")
    print()


def _run_llm_test() -> int:
    print("Testing Ollama (qwen2.5)...\n")
    results = LLM().test()
    res = results.get("ollama", {})
    if res.get("ok"):
        print(f"  [PASS] ollama: {res['text']!r} (model={res.get('model')})")
        print("\nOllama is working and ready.")
        return 0
    else:
        print(f"  [FAIL] ollama: {res.get('error', 'unknown error')}")
        print("\nOllama did not return text. Make sure Ollama is running ('ollama serve') and qwen2.5 is pulled ('ollama pull qwen2.5').")
        return 1


def _run_services(with_load: bool = True) -> int:
    print(f"==================================================")
    print(f" Starting AI-SRE Microservices Demo App")
    print(f"==================================================")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    python_bin = sys.executable

    processes: list[subprocess.Popen] = []
    cleaned_up = False

    def _cleanup(*_: Any) -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        print("\nStopping all services...", flush=True)
        for p in list(processes):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(0.4)
        for p in list(processes):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("All processes stopped.", flush=True)
        os._exit(0)

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
                "--reload",
                "--reload-dir",
                "services",
                "--reload-dir",
                "config",
                "--log-level",
                "info",
            ]
            print(f"  -> Starting {name:<10} on http://localhost:{port} (auto-reload enabled)")
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, start_new_session=True)
            processes.append(proc)
            time.sleep(0.4)

        print("\nAll 4 services started successfully:")
        print("  - Gateway:   http://localhost:8000 (Health: /health, Metrics: /metrics, Chaos: /chaos)")
        print("  - Orders:    http://localhost:8001")
        print("  - Payments:  http://localhost:8002")
        print("  - Inventory: http://localhost:8003")
        print("  - Logs:      logs/<service>.log")
        print("  - DB:        data/observability.db")

        # Start dashboard
        dash_port = _find_dashboard_port()
        if dash_port != 8080 and not os.getenv("DASHBOARD_PORT"):
            print(f"  - Dashboard: http://localhost:{dash_port} (Port 8080 is in use by another app, fell back to {dash_port})")
        else:
            print(f"  - Dashboard: http://localhost:{dash_port}")
        env["DASHBOARD_PORT"] = str(dash_port)
        cmd_dash = [
            python_bin,
            "-m",
            "uvicorn",
            "sre.dashboard.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(dash_port),
            "--reload",
            "--reload-dir",
            "sre",
            "--reload-dir",
            "config",
            "--log-level",
            "warning",
        ]
        proc_dash = subprocess.Popen(cmd_dash, cwd=str(REPO_ROOT), env=env, start_new_session=True)
        processes.append(proc_dash)

        # Start observability metric collector (every 1.5s into SQLite)
        print("\nStarting observability metric collector (1.5s interval)...")
        cmd_col = [python_bin, "sre/observability.py", "collect", "--interval", "1.5"]
        proc_col = subprocess.Popen(cmd_col, cwd=str(REPO_ROOT), env=env, start_new_session=True)
        processes.append(proc_col)

        # Start rule-based incident detector (watches metrics & health every 1s)
        print("Starting rule-based incident detector with autonomous Agent (1s interval)...")
        cmd_det = [python_bin, "sre/detector.py", "watch", "--interval", "1.0", "--auto-remediate"]
        proc_det = subprocess.Popen(cmd_det, cwd=str(REPO_ROOT), env=env, start_new_session=True)
        processes.append(proc_det)

        # Optional: Start Telegram Incident & Approval Bot if token is configured
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if tg_token and not tg_token.startswith("your_"):
            print("Starting Telegram Incident & Approval Bot...")
            cmd_tg = [python_bin, "sre/telegram_bot.py", "--api-url", f"http://localhost:{dash_port}"]
            proc_tg = subprocess.Popen(cmd_tg, cwd=str(REPO_ROOT), env=env, start_new_session=True)
            processes.append(proc_tg)

        if with_load:
            print("\nStarting steady traffic generator (2.0 req/s to gateway)...")
            cmd_load = [python_bin, "load.py", "--rate", "2.0"]
            proc_load = subprocess.Popen(cmd_load, cwd=str(REPO_ROOT), env=env, start_new_session=True)
            processes.append(proc_load)

        # Watch auxiliary python & config files and auto-restart monitors on change
        def _watch_aux() -> None:
            import threading
            watched = [
                REPO_ROOT / "sre" / "detector.py",
                REPO_ROOT / "sre" / "observability.py",
                REPO_ROOT / "config" / "slo.yaml",
                REPO_ROOT / "config" / "dependencies.yaml",
            ]
            mtimes = {f: f.stat().st_mtime for f in watched if f.is_file()}
            nonlocal proc_col, proc_det
            while True:
                time.sleep(1.0)
                changed = False
                for f in watched:
                    if f.is_file():
                        try:
                            mt = f.stat().st_mtime
                            if mt != mtimes.get(f):
                                mtimes[f] = mt
                                changed = True
                        except OSError:
                            pass
                if changed:
                    print("\n[AUTO-RELOAD] Detected change in detector/observability/config. Restarting background monitors...")
                    try:
                        proc_col.terminate()
                        proc_det.terminate()
                        proc_col.wait(timeout=1.0)
                        proc_det.wait(timeout=1.0)
                    except Exception:
                        pass
                    if proc_col in processes:
                        processes.remove(proc_col)
                    if proc_det in processes:
                        processes.remove(proc_det)
                    proc_col = subprocess.Popen(cmd_col, cwd=str(REPO_ROOT), env=env, start_new_session=True)
                    proc_det = subprocess.Popen(cmd_det, cwd=str(REPO_ROOT), env=env, start_new_session=True)
                    processes.append(proc_col)
                    processes.append(proc_det)

        import threading
        t_watch = threading.Thread(target=_watch_aux, daemon=True, name="aux_file_watcher")
        t_watch.start()

        print("\nSystem running with auto-reload enabled. Press Ctrl+C to stop.\n")

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
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Run only the SRE dashboard web server on port 8080.",
    )
    parser.add_argument(
        "--telegram-bot",
        action="store_true",
        help="Run only the SRE Telegram Incident & Approval Bot.",
    )
    args = parser.parse_args(argv)

    try:
        if args.test_llm:
            return _run_llm_test()
        if args.status:
            _print_status()
            return 0
        if args.telegram_bot:
            from sre.telegram_bot import main as tg_main
            return tg_main()
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
        if args.dashboard:
            from sre.dashboard.app import main as dash_main
            return dash_main()
        return _run_services(with_load=not args.services)
    except LLMConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())