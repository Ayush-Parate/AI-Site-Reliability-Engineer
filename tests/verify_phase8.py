#!/usr/bin/env python3
"""Live verification script for Phase 8: Dashboard, Testing and Demo Preparation.

Verifies Phase 8 exit criteria:
1. Starts microservices + dashboard (port 8080).
2. Live service health telemetry on GET /api/status.
3. Chaos injection via dashboard API (POST /api/chaos/inject).
4. Incident lifecycle and live timeline with evidence links (GET /api/incidents/{id}/timeline).
5. Evidence viewer lookup via GET /api/evidence/{id}.
6. Human approval flow: pending card, one-click Approve (POST /api/approvals/{id}/approve).
7. SRE metrics panel & 8-scenario benchmark table via GET /api/metrics.
8. Postmortem report viewer via GET /api/reports & GET /api/reports/{id}.
9. Chaos clear via dashboard API (POST /api/chaos/clear).
10. Clean shutdown.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from load import LoadGenerator
from sre.chaos import ChaosEngine
from sre.detector import IncidentDetector
from sre.fixer import Approval, Fixer
from sre.observability import Observability
from sre.postmortem import PostmortemGenerator
from sre.supervisor import Supervisor

DASHBOARD_URL = "http://127.0.0.1:8080"


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 8: SRE Operational Dashboard")
    print("==================================================")

    sup = Supervisor()
    chaos = ChaosEngine(supervisor=sup)
    obs = Observability()
    detector = IncidentDetector(observability=obs, supervisor=sup)
    fixer = Fixer(supervisor=sup, chaos=chaos, observability=obs, detector=detector)
    pm = PostmortemGenerator(observability=obs, detector=detector)

    dash_proc = None

    try:
        # Step 1: Start all microservices
        print("\n1. Starting microservices with supervisor.start_all()...")
        try:
            chaos.clear_all()
        except Exception:
            pass
        active = detector.get_active_incident()
        if active:
            detector.update_incident_status(active.id, "resolved")

        sup.start_all()
        time.sleep(1.0)
        st = sup.status()
        for name, d in st.items():
            print(f"  {name:<12}: PID={d['pid']}, status={d['status']}, healthy={d['healthy']}")
            assert d["healthy"] is True, f"Service {name} is not healthy!"

        # Step 2: Start Dashboard server process
        print("\n2. Launching SRE Dashboard server on http://localhost:8080...")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        cmd_dash = [sys.executable, "-m", "uvicorn", "sre.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
        dash_proc = subprocess.Popen(cmd_dash, cwd=str(REPO_ROOT), env=env)

        # Wait for dashboard health
        dash_up = False
        for _ in range(25):
            try:
                r = httpx.get(f"{DASHBOARD_URL}/health", timeout=1.0)
                if r.status_code == 200:
                    dash_up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        assert dash_up is True, "Dashboard server failed to start on port 8080!"
        print("  [PASS] Dashboard is live on http://localhost:8080/ (health 200 OK)")

        # Step 3: Verify GET / and GET /api/status
        print("\n3. Testing GET / and GET /api/status...")
        r_index = httpx.get(DASHBOARD_URL)
        assert r_index.status_code == 200
        assert "AI Site Reliability Engineer" in r_index.text
        print("  [PASS] GET / renders single-page SRE application.")

        r_status = httpx.get(f"{DASHBOARD_URL}/api/status")
        assert r_status.status_code == 200
        status_data = r_status.json()
        assert len(status_data["services"]) == 4
        print(f"  Live Telemetry: {len(status_data['services'])} microservices reporting health.")

        # Step 4: Inject Chaos via Dashboard API
        print("\n4. Testing POST /api/chaos/inject via dashboard...")
        r_inj = httpx.post(
            f"{DASHBOARD_URL}/api/chaos/inject",
            json={"failure": "error_burst", "service": "payments", "error_rate": 0.8},
        )
        assert r_inj.status_code == 200
        print(f"  Result: {r_inj.json()}")

        print("  Generating traffic across call graph...")
        detector.slo["error_rate_window_seconds"] = 5.0
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        # Step 5: Detect Incident and verify active incident in dashboard
        print("\n5. Running detector and verifying active incident in dashboard...")
        inc = detector.run_once()
        assert inc is not None, "Detector failed to detect error burst!"
        print(f"  Active Incident: {inc.id} (status: {inc.status}, rule: {inc.trigger_rule})")

        r_inc = httpx.get(f"{DASHBOARD_URL}/api/incidents")
        assert r_inc.status_code == 200
        inc_data = r_inc.json()
        assert inc_data["active"] is not None
        assert inc_data["active"]["id"] == inc.id
        print("  [PASS] Active incident correctly surfaced in dashboard API.")

        # Step 6: Verify Timeline and Evidence Viewer APIs
        print("\n6. Testing GET /api/incidents/{id}/timeline & GET /api/evidence/{id}...")
        r_tl = httpx.get(f"{DASHBOARD_URL}/api/incidents/{inc.id}/timeline")
        assert r_tl.status_code == 200
        tl_data = r_tl.json()
        print(f"  Timeline events: {len(tl_data['timeline'])} entries")
        assert len(tl_data["timeline"]) >= 2

        first_ev_id = inc.evidence[0]
        print(f"  Querying evidence record for citation: {first_ev_id}...")
        r_ev = httpx.get(f"{DASHBOARD_URL}/api/evidence/{first_ev_id}")
        assert r_ev.status_code == 200
        ev_data = r_ev.json()
        print(f"  Evidence payload: {ev_data.get('id')} ({ev_data.get('type')}) on {ev_data.get('service')}")
        assert ev_data["id"] == first_ev_id
        print("  [PASS] Evidence drawer API returns verified telemetry record.")

        # Step 7: Verify Human Approval Flow (Ask-First Tier)
        print("\n7. Testing Human Approval Flow (Ask-First Tier)...")
        # Create an approval record for rollback
        appr = fixer._create_approval(
            incident_id=inc.id,
            action="rollback",
            args={"service": "payments", "to_version": "1.0.0"},
            reason="Breaking deploy detected, proposing safe rollback.",
        )
        print(f"  Created Approval: {appr.id} ({appr.action} on {appr.args.get('service')})")

        # Check approval in dashboard API
        r_appr_list = httpx.get(f"{DASHBOARD_URL}/api/approvals")
        assert r_appr_list.status_code == 200
        appr_list = r_appr_list.json()
        pending_ids = [a["id"] for a in appr_list["pending"]]
        assert appr.id in pending_ids
        print(f"  Pending approvals list includes: {appr.id}")

        # Approve action via dashboard POST /api/approvals/{id}/approve
        print(f"  Operator clicking Approve for {appr.id}...")
        r_decide = httpx.post(
            f"{DASHBOARD_URL}/api/approvals/{appr.id}/approve",
            json={"decided_by": "lead_sre_operator", "reason": "Authorized via web dashboard"},
        )
        assert r_decide.status_code == 200
        dec_data = r_decide.json()
        assert dec_data["approval"]["status"] == "approved"
        print(f"  Approval executed: status={dec_data['approval']['status']}, decided_by={dec_data['approval']['decided_by']}")
        print("  [PASS] Human approval flow verified through dashboard API.")

        # Step 8: SRE Metrics Panel & 8-Scenario Benchmark Table API
        print("\n8. Testing GET /api/metrics (SRE Benchmarks & Speedup)...")
        r_met = httpx.get(f"{DASHBOARD_URL}/api/metrics")
        assert r_met.status_code == 200
        met_data = r_met.json()
        assert len(met_data["benchmarks"]) == 8
        print(f"  8 SRE Benchmarks loaded. Average speedup vs manual: {met_data['benchmarks'][0]['speedup']}x - {met_data['benchmarks'][-1]['speedup']}x")
        print("  [PASS] Benchmark metrics API active.")

        # Step 9: Postmortem Report Viewer API
        print("\n9. Testing GET /api/reports and GET /api/reports/{id}...")
        # Generate postmortem for current incident
        detector.update_incident_status(inc.id, "resolved")
        pm.generate_postmortem(inc.id, use_llm=False)

        r_reps = httpx.get(f"{DASHBOARD_URL}/api/reports")
        assert r_reps.status_code == 200
        reports_list = r_reps.json()["reports"]
        assert len(reports_list) > 0
        print(f"  Reports available: {[r['incident_id'] for r in reports_list]}")

        r_rep_single = httpx.get(f"{DASHBOARD_URL}/api/reports/{inc.id}")
        assert r_rep_single.status_code == 200
        rep_data = r_rep_single.json()
        assert "## 3. Root Cause Analysis" in rep_data["markdown"]
        print("  [PASS] Postmortem viewer API serves formatted Markdown report.")

        # Step 10: Reset Chaos via Dashboard API
        print("\n10. Testing POST /api/chaos/clear via dashboard...")
        r_clear = httpx.post(f"{DASHBOARD_URL}/api/chaos/clear")
        assert r_clear.status_code == 200
        print("  [PASS] Chaos reset completed via dashboard API.")

        print("\nALL PHASE 8 VERIFICATION CHECKS PASSED SUCCESSFULLY!")
        return 0

    finally:
        print("\nStopping dashboard and microservices...")
        if dash_proc:
            dash_proc.terminate()
            try:
                dash_proc.wait(timeout=2.0)
            except Exception:
                dash_proc.kill()
        try:
            chaos.clear_all()
        except Exception:
            pass
        sup.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
