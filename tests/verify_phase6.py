#!/usr/bin/env python3
"""Live verification script for Phase 6: Fix Pipeline and Approval Flow.

Verifies exit criteria:
1. Crash: auto-restart works and is verified.
2. Bad deploy: restart fails circuit / rollback ask-first; human approves; recovers.
3. Prompt-injection: drop_db / delete_data blocked by code and logged.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.chaos import ChaosEngine
from sre.detector import IncidentDetector
from sre.fixer import Fixer
from sre.observability import Observability
from sre.supervisor import Supervisor


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 6: Fix Pipeline & Approvals")
    print("==================================================")

    sup = Supervisor()
    chaos = ChaosEngine(supervisor=sup)
    obs = Observability()
    detector = IncidentDetector(observability=obs, supervisor=sup)
    fixer = Fixer(
        supervisor=sup,
        chaos=chaos,
        observability=obs,
        detector=detector,
        verify_timeout=20.0,
        approval_timeout=2.0,
    )

    try:
        print("\n1. Starting all microservices...")
        # Ensure clean ports from prior runs
        try:
            chaos.clear_all()
        except Exception:
            pass
        active = detector.get_active_incident()
        if active:
            detector.update_incident_status(active.id, "resolved")

        sup.start_all()
        time.sleep(1.2)
        for name, d in sup.status().items():
            print(f"  {name:<12}: status={d['status']}, healthy={d['healthy']}")
            assert d["healthy"] is True, f"{name} not healthy"

        print("\n2. Prompt-injection / never-do blocks...")
        blocked = fixer.apply_fix(
            {"action": "drop_db", "args": {"service": "inventory"}},
        )
        assert blocked.status == "blocked"
        assert blocked.tier == "never_do"
        blocked2 = fixer.apply_fix(
            {"action": "delete_data", "args": {"service": "inventory"}},
        )
        assert blocked2.status == "blocked"
        changes = (REPO_ROOT / "data" / "changes.jsonl").read_text(encoding="utf-8")
        assert "fix_blocked" in changes
        print("  [PASS] drop_db / delete_data blocked and logged.")

        print("\n3. Crash → auto-restart + verify...")
        chg = chaos.inject_crash("payments")
        print(f"  Injected: [{chg['id']}] {chg['action']}")
        time.sleep(0.5)
        st = sup.status().get("payments", {})
        assert st.get("healthy") is False
        print(f"  payments healthy={st.get('healthy')} status={st.get('status')}")

        # Open / attach an incident for the fix pipeline
        obs.poll_once()
        detector.slo["health_check_failures"] = 2
        incident = detector.run_once() or detector.get_active_incident()
        if incident is None:
            # Synthesize minimal incident if detector window is slow
            from sre.detector import Incident
            from sre.observability import format_iso

            now = time.time()
            incident = Incident(
                id="INC-P6-CRASH",
                status="diagnosing",
                trigger_rule="consecutive_health_failures",
                t0=format_iso(now - 1),
                t0_unix=now - 1,
                created_at=format_iso(now),
                created_at_unix=now,
                ttd_seconds=1.0,
                affected_services=["payments"],
            )
            detector.save_incident(incident)
        else:
            detector.update_incident_status(incident.id, "diagnosing")

        result = fixer.apply_fix(
            {"action": "restart_service", "args": {"service": "payments"}},
            incident=incident,
        )
        print(f"  Fix status={result.status} message={result.message}")
        assert result.status == "verified", f"Expected verified, got {result.to_dict()}"
        assert result.tier == "auto_fix"
        assert result.verify_result and result.verify_result.get("recovered")
        st = sup.status().get("payments", {})
        assert st.get("healthy") is True
        refreshed = detector.get_incident(incident.id)
        assert refreshed is not None and refreshed.status == "resolved"
        print("  [PASS] Crash auto-restart verified; incident resolved.")

        print("\n4. Bad deploy → rollback ask-first → approve → recover...")
        # Resolve any leftover active incident
        active = detector.get_active_incident()
        if active and active.status != "resolved":
            detector.update_incident_status(active.id, "resolved")

        deploy = chaos.inject_bad_deploy("payments")
        print(f"  Bad deploy: [{deploy['id']}] {deploy.get('details')}")
        time.sleep(1.0)
        ver = (REPO_ROOT / "services" / "payments" / "VERSION").read_text().strip()
        assert ver == "2.0.0"

        # Record incident for rollback path
        from sre.detector import Incident
        from sre.observability import format_iso

        now = time.time()
        bad_inc = Incident(
            id="INC-P6-BADDEPLOY",
            status="diagnosing",
            trigger_rule="error_rate_threshold",
            t0=format_iso(now - 2),
            t0_unix=now - 2,
            created_at=format_iso(now),
            created_at_unix=now,
            ttd_seconds=2.0,
            affected_services=["payments"],
            metadata={"diagnosis": {"root_cause_service": "payments", "proposed_fix": {
                "action": "rollback",
                "args": {"service": "payments", "to_version": "1.0.0"},
            }}},
        )
        detector.save_incident(bad_inc)

        # Ask-first rollback (do not burn circuit breaker with extra restarts first)
        pending = fixer.apply_fix(
            {"action": "rollback", "args": {"service": "payments", "to_version": "1.0.0"}},
            incident=bad_inc,
        )
        assert pending.status == "pending_approval", pending.to_dict()
        assert pending.approval_id
        print(f"  Pending approval: {pending.approval_id}")

        result = fixer.execute_approved(pending.approval_id, decided_by="judge")
        print(f"  Rollback result status={result.status}")
        assert result.status == "verified", result.to_dict()
        ver = (REPO_ROOT / "services" / "payments" / "VERSION").read_text().strip()
        assert ver == "1.0.0"
        st = sup.status().get("payments", {})
        assert st.get("healthy") is True
        print("  [PASS] Human-approved rollback recovered payments to v1.")

        print("\n5. Circuit breaker proposes rollback after repeated restarts...")
        # Use a fresh fixer window by recording synthetic restart counts
        fixer._restart_times["orders"] = [time.time(), time.time()]
        assert fixer.circuit_open("orders") is True
        # Ensure orders is running for the blocked attempt
        if not sup.status().get("orders", {}).get("healthy"):
            sup.start_service("orders")
        cb = fixer.apply_fix(
            {"action": "restart_service", "args": {"service": "orders"}},
            incident=None,
        )
        assert cb.status == "escalated"
        assert cb.approval_id
        appr = fixer.get_approval(cb.approval_id)
        assert appr is not None and appr.action == "rollback"
        print(f"  Circuit breaker -> approval {cb.approval_id} for rollback")
        # Cancel leftover pending so it does not linger
        fixer.deny(cb.approval_id, decided_by="verify", reason="cleanup")
        print("  [PASS] Circuit breaker tripped and proposed rollback.")

        print("\n6. Cleanup...")
        chaos.clear_all()
        time.sleep(0.5)
        for name, d in sup.status().items():
            if not d.get("healthy"):
                sup.start_service(name)
        print("  Done.")

        print("\n==================================================")
        print(" Phase 6 verification PASSED")
        print("==================================================")
        return 0

    except Exception as exc:
        print(f"\n[FAIL] {exc}")
        try:
            chaos.clear_all()
        except Exception:
            pass
        # Restore VERSION if left on v2
        ver_file = REPO_ROOT / "services" / "payments" / "VERSION"
        try:
            if ver_file.read_text().strip() != "1.0.0":
                ver_file.write_text("1.0.0\n", encoding="utf-8")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
