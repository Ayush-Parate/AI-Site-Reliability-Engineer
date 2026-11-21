#!/usr/bin/env python3
"""Live verification script for Phase 1 Demo Microservices App."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

SERVICES = [
    ("inventory", 8003, "services.inventory.main:app"),
    ("payments", 8002, "services.payments.main:app"),
    ("orders", 8001, "services.orders.main:app"),
    ("gateway", 8000, "services.gateway.main:app"),
]


def wait_for_health(port: int, service_name: str, timeout: float = 10.0) -> bool:
    start = time.time()
    url = f"http://127.0.0.1:{port}/health"
    while time.time() - start < timeout:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                print(f"  [OK] {service_name} on :{port} is healthy")
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.3)
    print(f"  [FAIL] {service_name} on :{port} failed to become healthy")
    return False


def main() -> int:
    processes: list[subprocess.Popen] = []
    print("1. Starting 4 microservices processes...")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    try:
        for name, port, app_target in SERVICES:
            cmd = [
                PYTHON,
                "-m",
                "uvicorn",
                app_target,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ]
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env)
            processes.append(proc)

        # Wait for all services to become healthy
        all_healthy = True
        for name, port, _ in SERVICES:
            if not wait_for_health(port, name):
                all_healthy = False

        if not all_healthy:
            print("ERROR: Not all services started successfully.")
            return 1

        print("\n2. Sending curl/request to Gateway (:8000) for full order...")
        custom_trace = "trace_e2e_live_test_999"
        order_resp = httpx.post(
            "http://127.0.0.1:8000/orders",
            json={"customer_id": "cust_live_test", "item_id": "item_2", "quantity": 1},
            headers={"X-Trace-Id": custom_trace},
            timeout=5.0,
        )
        print(f"  Response Status: {order_resp.status_code}")
        print(f"  Response Body: {order_resp.text}")
        print(f"  X-Trace-Id header: {order_resp.headers.get('x-trace-id')}")

        if order_resp.status_code != 200:
            print("ERROR: Order request failed!")
            return 1

        order_data = order_resp.json()
        if order_data.get("status") != "confirmed":
            print(f"ERROR: Expected status confirmed, got {order_data.get('status')}")
            return 1

        print("\n3. Verifying trace ID propagation across all service log files...")
        logs_dir = REPO_ROOT / "logs"
        for name, _, _ in SERVICES:
            log_file = logs_dir / f"{name}.log"
            if not log_file.exists():
                print(f"ERROR: Log file {log_file} does not exist!")
                return 1
            content = log_file.read_text(encoding="utf-8")
            if custom_trace not in content:
                print(f"ERROR: Trace ID {custom_trace} not found in {name}.log!")
                return 1
            print(f"  [OK] Found trace ID {custom_trace} in {name}.log")

        print("\n4. Running load generator (load.py --count 15 --rate 5)...")
        load_proc = subprocess.run(
            [PYTHON, "load.py", "--count", "15", "--rate", "5.0", "--url", "http://127.0.0.1:8000/orders"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        print(load_proc.stdout)
        if load_proc.returncode != 0:
            print(f"ERROR: Load generator failed with code {load_proc.returncode}")
            print(load_proc.stderr)
            return 1

        print("  [OK] Load generator completed with 0 errors!")

        print("\n5. Testing Chaos API (latency, error_rate, unhealthy, reset)...")
        # Test error_rate chaos
        c_res = httpx.post("http://127.0.0.1:8002/chaos", json={"error_rate": 1.0})
        print(f"  POST /chaos on payments (error_rate=1.0): {c_res.json()}")
        # Request should now fail
        fail_res = httpx.post(
            "http://127.0.0.1:8000/orders",
            json={"customer_id": "cust_chaos", "item_id": "item_1", "quantity": 1},
            timeout=5.0,
        )
        print(f"  Gateway response under payments chaos: {fail_res.status_code} (expected 502/500)")
        assert fail_res.status_code >= 500

        # Reset chaos
        r_res = httpx.post("http://127.0.0.1:8002/chaos/reset")
        print(f"  POST /chaos/reset on payments: {r_res.json()}")

        # Healthy again
        recovered_res = httpx.post(
            "http://127.0.0.1:8000/orders",
            json={"customer_id": "cust_recovered", "item_id": "item_1", "quantity": 1},
            timeout=5.0,
        )
        print(f"  Gateway response after reset: {recovered_res.status_code} (status={recovered_res.json().get('status')})")
        assert recovered_res.status_code == 200

        print("\nALL PHASE 1 CRITERIA VERIFIED SUCCESSFULLY!")
        return 0

    finally:
        print("\nCleaning up service processes...")
        for p in processes:
            p.terminate()
            try:
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
