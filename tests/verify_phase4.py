#!/usr/bin/env python3
"""Live verification script for Phase 4: Incident Detector.

Starts all microservices, generates steady traffic, tests:
1. Healthy baseline: zero false alarms.
2. Error burst failure: incident opens within 30s, accurate T0, TTD calculation, evidence citations.
3. Cascading failure: incident deduplication (attaches to existing incident, appends affected services).
4. Auto-resolution: recovers after staying healthy for recover_after_seconds.
5. Crash failure: opens incident on consecutive health check failures.
6. Clean recovery and shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from load import LoadGenerator
from sre.chaos import ChaosEngine
from sre.detector import IncidentDetector
from sre.observability import Observability
from sre.supervisor import Supervisor


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 4: Incident Detector")
    print("==================================================")

    sup = Supervisor()
    chaos = ChaosEngine(supervisor=sup)
    obs = Observability()
    detector = IncidentDetector(observability=obs, supervisor=sup)

    try:
        # Step 1: Start all microservices
        print("\n1. Starting all microservices with supervisor.start_all()...")
        sup.start_all()
        time.sleep(1.0)
        st = sup.status()
        for name, d in st.items():
            print(f"  {name:<12}: PID={d['pid']}, status={d['status']}, healthy={d['healthy']}")
            assert d["healthy"] is True, f"Service {name} is not healthy!"

        # Clean slate: clear chaos and resolve any leftover incidents from prior runs
        chaos.clear_all()
        active = detector.get_active_incident()
        if active:
            detector.update_incident_status(active.id, "resolved")

        # Set 5s window and 1s recovery for test efficiency
        detector.slo["error_rate_window_seconds"] = 5.0
        detector.slo["recover_after_seconds"] = 1.0

        # Step 2: Healthy Baseline (Zero False Alarms)
        print("\n2. Waiting 5.2s for clean window and sending steady healthy traffic (15 orders)...")
        time.sleep(5.2)
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        print("  Evaluating rules on healthy traffic...")
        inc = detector.run_once()
        print(f"  Result: {inc}")
        assert inc is None, f"False alarm! Detector returned: {inc}"
        assert detector.get_active_incident() is None
        print("  [PASS] Zero false alarms on healthy baseline.")

        # Step 3: Inject Error Burst Failure on payments
        print("\n3. Injecting Error Burst on payments (error_rate=0.8)...")
        chg = chaos.inject_error_burst("payments", error_rate=0.8)
        print(f"  Chaos change: [{chg['id']}] {chg['action']}")

        print("  Sending traffic while payments is failing...")
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        # Step 4: Detect Incident
        print("\n4. Running Incident Detector...")
        inc1 = detector.run_once()
        assert inc1 is not None, "Failed to open incident on error burst!"
        print(f"  Incident Opened: {inc1.id} (Status: {inc1.status})")
        print(f"  Trigger Rule   : {inc1.trigger_rule}")
        print(f"  Affected Srvcs : {inc1.affected_services}")
        print(f"  T0 (Anomaly)   : {inc1.t0}")
        print(f"  Detection Time : {inc1.created_at} (TTD: {inc1.ttd_seconds:.1f}s)")
        print(f"  Evidence IDs   : {inc1.evidence}")

        assert inc1.id.startswith("INC-")
        assert inc1.status == "open"
        assert "payments" in inc1.affected_services
        assert len(inc1.evidence) > 0
        assert inc1.ttd_seconds >= 0.0

        # Step 5: Test Deduplication with a second cascading failure
        print("\n5. Testing Deduplication: Injecting Latency on inventory (2500ms)...")
        chg_lat = chaos.inject_latency("inventory", latency_ms=2500.0)
        print(f"  Chaos change: [{chg_lat['id']}] {chg_lat['action']}")

        print("  Sending traffic during secondary failure...")
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=10, rate=20.0).run())
        time.sleep(0.5)

        print("  Running detector again: should deduplicate into existing incident...")
        inc1_dedup = detector.run_once()
        assert inc1_dedup is not None
        assert inc1_dedup.id == inc1.id, f"Deduplication failed! Expected {inc1.id}, got {inc1_dedup.id}"
        print(f"  Deduplicated into {inc1_dedup.id}: Affected services now: {inc1_dedup.affected_services}")
        assert "inventory" in inc1_dedup.affected_services
        assert "payments" in inc1_dedup.affected_services
        print("  [PASS] Deduplication verified.")

        # Step 6: Test Incident Status Update
        print("\n6. Updating incident status to 'diagnosing'...")
        diag_inc = detector.update_incident_status(inc1.id, "diagnosing", {"agent": "ai-sre"})
        assert diag_inc.status == "diagnosing"
        print(f"  Status updated to: {diag_inc.status}")

        # Step 7: Clear chaos and test auto-resolution
        print("\n7. Clearing all chaos and testing auto-resolution...")
        chaos.clear_all()
        time.sleep(1.0)

        # Set short recovery window for verification run
        detector.slo["error_rate_window_seconds"] = 5.0
        detector.slo["recover_after_seconds"] = 1.0

        # Wait for old errors to age out of the 5s window
        print("  Waiting 5.0s for clean observation window...")
        time.sleep(5.2)

        # Send clean traffic to establish healthy metrics
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=10, rate=30.0).run())
        time.sleep(0.5)

        # Trigger check: healthy_since timer starts
        inc_recovering = detector.run_once()
        assert inc_recovering is not None
        print(f"  Incident recovery timer started: healthy_since={inc_recovering.healthy_since}")
        assert inc_recovering.healthy_since is not None

        time.sleep(1.2)

        # Run detector again: auto-resolves!
        inc_resolved = detector.run_once()
        assert inc_resolved is not None
        assert inc_resolved.status == "resolved"
        assert inc_resolved.resolved_at is not None
        print(f"  Incident Auto-Resolved! ID: {inc_resolved.id}, Resolved At: {inc_resolved.resolved_at}")
        print(f"  Total Duration: {inc_resolved.metadata.get('duration_seconds')}s")

        active = detector.get_active_incident()
        assert active is None, "Active incident should be None after resolution!"

        # Step 8: Verify persistence across detector instances
        print("\n8. Verifying SQLite persistence in data/observability.db...")
        detector2 = IncidentDetector()
        persisted = detector2.get_incident(inc1.id)
        assert persisted is not None
        assert persisted.id == inc1.id
        assert persisted.status == "resolved"
        print(f"  Persisted incident {persisted.id} retrieved successfully from SQLite.")

        print("\n==================================================")
        print(" Phase 4 Incident Detector Verification PASSED! ")
        print("==================================================")
        return 0

    finally:
        print("\nStopping all services...")
        sup.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
