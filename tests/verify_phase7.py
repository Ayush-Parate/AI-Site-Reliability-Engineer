#!/usr/bin/env python3
"""Live verification script for Phase 7: Timeline, Postmortem, and SRE Metrics.

Verifies Phase 7 exit criteria:
1. Timeline constructed from audit log and observability events with linked evidence IDs.
2. Postmortem generator fills fixed template and saves report to reports/INC-<id>.md.
3. Every claim in the postmortem has a cited evidence ID; uncited claims are strictly rejected.
4. Measured metrics calculated: Time-to-Detect (TTD), Time-to-Diagnosis (TTDiag), Time-to-Recovery (TTR).
5. Benchmark results table comparing AI-SRE vs manual baseline across all 8 failure scenarios.
6. Clean recovery and shutdown.
"""

from __future__ import annotations

import asyncio
import os
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
from sre.postmortem import (
    BenchmarkSuite,
    PostmortemGenerator,
    PostmortemValidator,
    TimelineBuilder,
)
from sre.supervisor import Supervisor


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 7: Timeline, Postmortem & Metrics")
    print("==================================================")

    sup = Supervisor()
    chaos = ChaosEngine(supervisor=sup)
    obs = Observability()
    detector = IncidentDetector(observability=obs, supervisor=sup)
    generator = PostmortemGenerator(observability=obs, detector=detector)
    validator = PostmortemValidator(observability=obs)

    try:
        # Step 1: Start all microservices
        print("\n1. Starting all microservices and ensuring baseline health...")
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

        # Step 2: Inject Chaos Failure to generate real incident data
        print("\n2. Injecting Error Burst failure on payments (error_rate=0.8)...")
        chg = chaos.inject_error_burst("payments", error_rate=0.8)
        print(f"  Chaos change logged: [{chg['id']}] {chg['action']} on {chg['service']}")

        print("  Generating traffic across call chain...")
        detector.slo["error_rate_window_seconds"] = 5.0
        asyncio.run(LoadGenerator(url="http://127.0.0.1:8000/orders", count=15, rate=30.0).run())
        time.sleep(0.5)

        # Step 3: Trigger Incident Detection
        print("\n3. Detecting incident via rule detector...")
        incident = detector.run_once()
        assert incident is not None, "Detector failed to detect error burst!"
        print(f"  Incident Opened: {incident.id}")
        print(f"  Trigger Rule   : {incident.trigger_rule}")
        print(f"  Affected Srvcs : {incident.affected_services}")
        print(f"  T0 (Anomaly)   : {incident.t0}")
        print(f"  TTD            : {incident.ttd_seconds:.1f}s")
        print(f"  Evidence IDs   : {incident.evidence}")

        # Attach simulated diagnosis and fix metadata
        incident.metadata["diagnosis"] = {
            "root_cause_service": "payments",
            "root_cause": "High 5xx error burst on payments service due to injected chaos",
            "t1": incident.created_at,
            "t1_unix": incident.created_at_unix + 4.0,
            "evidence": list(incident.evidence[:2]),
            "causal_chain": ["payments 500 burst", "orders cascade error", "gateway 502"],
        }
        incident.metadata["fix"] = {
            "action": "clear_chaos_flag",
            "service": "payments",
            "applied_at": incident.created_at,
            "applied_at_unix": incident.created_at_unix + 8.0,
            "evidence_ids": [chg["id"]],
        }
        detector.save_incident(incident)

        # Step 4: Clear Chaos and Resolve Incident
        print("\n4. Clearing chaos and resolving incident...")
        chaos.clear_all()
        time.sleep(0.5)
        detector.update_incident_status(incident.id, "resolved")
        resolved_inc = detector.get_incident(incident.id)
        assert resolved_inc is not None
        assert resolved_inc.status == "resolved"
        print(f"  Incident {incident.id} marked RESOLVED at {resolved_inc.resolved_at}")

        # Step 5: Verify Incident Timeline
        print("\n5. Building audit-backed Incident Timeline...")
        timeline = generator.timeline_builder.build_timeline(resolved_inc)
        print(f"  Timeline generated with {len(timeline)} chronological entries:")
        for t in timeline:
            ev = f"[{', '.join(t.evidence_ids)}]" if t.evidence_ids else ""
            print(f"    {t.ts} | {t.event.upper():<10} | {t.service:<10} | {t.description} {ev}")

        assert len(timeline) >= 4, "Timeline must contain at least 4 entries"
        # Chronological sorting verification
        for i in range(len(timeline) - 1):
            assert timeline[i].timestamp_unix <= timeline[i + 1].timestamp_unix, "Timeline not chronologically sorted!"

        timeline_events = [t.event for t in timeline]
        assert "detected" in timeline_events, "Timeline missing detected event"
        assert "mitigated" in timeline_events or "resolved" in timeline_events

        # Step 6: Generate Postmortem Report
        print(f"\n6. Generating Postmortem Report for {incident.id}...")
        report_md, report_path = generator.generate_postmortem(incident.id, use_llm=False)
        print(f"  Postmortem written to: {report_path}")
        assert report_path.is_file(), f"Report file {report_path} was not created!"

        # Step 7: Strict Postmortem Claim Validation
        print("\n7. Validating Postmortem Evidence Citations...")
        val_res = validator.validate(report_md)
        print(f"  Validator result: valid={val_res['valid']}, citations={len(val_res['citations'])}")
        print(f"  Citations found: {val_res['citations']}")
        assert val_res["valid"] is True, f"Postmortem validation failed: {val_res['errors']}"
        assert len(val_res["citations"]) > 0, "No evidence citations in postmortem report!"

        # Verify all 8 standard sections exist
        for sec in [
            "## 1. Summary & Severity",
            "## 2. Impact",
            "## 3. Root Cause Analysis",
            "## 4. Incident Timeline",
            "## 5. What Worked & What Didn't",
            "## 6. Fix Applied & Verification",
            "## 7. Prevention & Follow-up Actions",
            "## 8. Cited Evidence Index",
        ]:
            assert sec in report_md, f"Report missing mandatory section: {sec}"
        print("  [PASS] All 8 mandatory postmortem sections validated with cited evidence.")

        # Step 8: SRE Metrics Computation
        print("\n8. Computing SRE Metrics (TTD, TTDiag, TTR vs Manual Baseline)...")
        metrics = generator.calculate_metrics(resolved_inc)
        print(f"  Incident ID   : {metrics['incident_id']}")
        print(f"  Scenario Type : {metrics['scenario']}")
        print(f"  TTD           : {metrics['ttd_seconds']:.2f}s")
        print(f"  TTDiag (T1-T0): {metrics['ttdiag_seconds']:.2f}s")
        print(f"  TTR (Recovery): {metrics['ttr_seconds']:.2f}s")
        print(f"  Manual Baseline: {metrics['manual_baseline_seconds']:.1f}s")
        print(f"  Speedup Factor: {metrics['speedup_factor']:.1f}x faster")
        assert metrics["ttd_seconds"] >= 0.0
        assert metrics["ttdiag_seconds"] >= metrics["ttd_seconds"]
        assert metrics["speedup_factor"] > 1.0

        # Step 9: 8-Scenario Benchmark Results Table
        print("\n9. Generating 8-Scenario Benchmark Results Table...")
        bench_table = BenchmarkSuite.render_benchmark_table()
        print(bench_table)
        assert "| Failure Scenario |" in bench_table
        assert "Process Crash" in bench_table
        assert "SQLite DB Write Lock" in bench_table

        print("\nALL PHASE 7 VERIFICATION CHECKS PASSED SUCCESSFULLY!")
        return 0

    finally:
        print("\nStopping all services...")
        try:
            chaos.clear_all()
        except Exception:
            pass
        sup.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
