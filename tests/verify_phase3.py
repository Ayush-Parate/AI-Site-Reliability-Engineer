#!/usr/bin/env python3
"""Live verification script for Phase 3: Observability Layer.

Starts microservices, sends traffic, injects a failure, and verifies:
1. Metric Collector (polling into data/observability.db)
2. Log Reader (get_logs with filtering and LOG-x IDs)
3. Dependency Graph (get_deps upstream/downstream)
4. Change Query (get_changes with CHG-x IDs)
5. Derived Metrics (error_rate, p95_latency, check_health)
6. Stable Evidence IDs (get_evidence for LOG-x, MET-x, CHG-x)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from load import LoadGenerator
from sre.chaos import ChaosEngine
from sre.observability import Observability
from sre.supervisor import Supervisor


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 3: Observability Layer")
    print("==================================================")

    sup = Supervisor()
    chaos = ChaosEngine(supervisor=sup)
    obs = Observability()

    try:
        # Step 1: Start all microservices
        print("\n1. Starting all microservices with supervisor.start_all()...")
        sup.start_all()
        time.sleep(1.0)
        st = sup.status()
        for name, d in st.items():
            print(f"  {name:<12}: PID={d['pid']}, status={d['status']}, healthy={d['healthy']}")
            assert d["healthy"] is True, f"Service {name} is not healthy!"

        # Step 2: Send normal load traffic
        print("\n2. Sending steady traffic with LoadGenerator...")
        import asyncio
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        # Step 3: Verify Metric Collector (poll_once into SQLite)
        print("\n3. Testing Metric Collector (poll_once)...")
        poll_res = obs.poll_once()
        for svc, info in poll_res.items():
            print(f"  {svc:<12}: health={info['health']}, samples={info['samples']}")
            assert info["health"] == "ok"
            assert info["samples"] > 0

        # Query metrics from SQLite
        print("\n4. Querying metrics from SQLite (get_metrics)...")
        gw_metrics = obs.get_metrics(service="gateway", metric="http_requests_total", window=60)
        assert len(gw_metrics["samples"]) > 0, "No http_requests_total samples found!"
        first_met = gw_metrics["samples"][0]
        print(f"  Retrieved {len(gw_metrics['samples'])} samples. Latest: [{first_met['id']}] value={first_met['value']}")
        assert first_met["id"].startswith("MET-")

        # Step 4: Verify Dependency Graph (get_deps)
        print("\n5. Verifying Dependency Graph (get_deps)...")
        deps_ord = obs.get_deps("orders")
        print(f"  orders: upstream={deps_ord['upstream']}, downstream={deps_ord['downstream']}")
        assert deps_ord["upstream"] == ["gateway"]
        assert set(deps_ord["downstream"]) == {"payments", "inventory"}

        deps_pay = obs.get_deps("payments")
        assert deps_pay["upstream"] == ["orders"]
        assert deps_pay["downstream"] == []

        # Step 5: Verify Log Reader (get_logs)
        print("\n6. Verifying Log Reader (get_logs)...")
        logs = obs.get_logs(service="gateway", limit=5)
        assert len(logs) > 0, "No gateway logs found!"
        first_log = logs[0]
        print(f"  Sample log: [{first_log['id']}] {first_log['ts']} {first_log['service']} - {first_log['msg']}")
        assert first_log["id"].startswith("LOG-")
        assert first_log["service"] == "gateway"

        # Trace ID filtering
        sample_trace = first_log["trace_id"]
        if sample_trace and sample_trace != "-":
            trace_logs = obs.get_logs(trace_id=sample_trace)
            print(f"  Trace '{sample_trace}' spans {len(trace_logs)} log entries across services: {[l['service'] for l in trace_logs]}")
            assert len(trace_logs) >= 1

        # Step 6: Inject Failure and verify Change Query & Derived Metrics
        print("\n7. Injecting Error Burst on payments (error_rate=0.8)...")
        chg_entry = chaos.inject_error_burst("payments", error_rate=0.8)
        print(f"  Chaos change entry: [{chg_entry['id']}] {chg_entry['action']}")
        assert chg_entry["id"].startswith("CHG-")

        # Send traffic to trigger errors on payments
        print("  Sending traffic while error burst is active...")
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        # Poll metrics again
        obs.poll_once()

        # Step 7: Verify Change Query (get_changes)
        print("\n8. Verifying Change Query (get_changes)...")
        changes = obs.get_changes(since=60, service="payments")
        assert len(changes) > 0
        latest_chg = changes[0]
        print(f"  Found change: [{latest_chg['id']}] {latest_chg['action']} ({latest_chg['author']})")
        assert latest_chg["id"].startswith("CHG-")
        assert latest_chg["service"] == "payments"

        # Step 8: Verify Derived Metrics (error_rate and p95_latency)
        print("\n9. Verifying Derived Metrics for payments...")
        err_res = obs.error_rate("payments", window=30)
        p95_res = obs.p95_latency("payments", window=30)
        print(f"  Error Rate : {err_res['error_rate'] * 100:.1f}% ({err_res['error_count']}/{err_res['total_requests']}) [Breached: {err_res['breached']}]")
        print(f"  Evidence IDs: {err_res['evidence_ids']}")
        assert float(err_res) > 0.05, f"Expected error rate > 5%, got {float(err_res)}"
        assert err_res["breached"] is True

        # Step 9: Verify Evidence Lookup (get_evidence)
        print("\n10. Verifying Evidence ID lookup (get_evidence)...")
        # Log evidence
        err_log_id = err_res["evidence_ids"][0]
        log_evi = obs.get_evidence(err_log_id)
        assert log_evi is not None
        assert log_evi["id"] == err_log_id
        print(f"  Lookup {err_log_id} -> {log_evi['service']} status={log_evi['status']} msg={log_evi['msg']}")

        # Metric evidence
        met_evi = obs.get_evidence(first_met["id"])
        assert met_evi is not None
        assert met_evi["id"] == first_met["id"]
        print(f"  Lookup {first_met['id']} -> {met_evi['metric']} value={met_evi['value']}")

        # Change evidence
        chg_evi = obs.get_evidence(latest_chg["id"])
        assert chg_evi is not None
        assert chg_evi["id"] == latest_chg["id"]
        print(f"  Lookup {latest_chg['id']} -> {chg_evi['action']} details={chg_evi['details']}")

        # Step 10: Clear chaos & verify recovery
        print("\n11. Clearing chaos and verifying recovery...")
        chaos.clear_all()
        time.sleep(0.5)
        hlth = obs.check_health()
        print(f"  All services health check: {hlth['healthy']}")
        assert hlth["healthy"] is True

        print("\n==================================================")
        print(" Phase 3 Observability Verification PASSED! ")
        print("==================================================")
        return 0

    finally:
        print("\nStopping all services...")
        sup.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
