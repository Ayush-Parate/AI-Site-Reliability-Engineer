#!/usr/bin/env python3
"""Live verification script for Phase 2: Supervisor and Chaos Injection."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.chaos import ChaosEngine
from sre.supervisor import Supervisor


def main() -> int:
    print("==================================================")
    print(" Verifying Phase 2: Supervisor & Chaos Injection")
    print("==================================================")

    sup = Supervisor()
    engine = ChaosEngine(supervisor=sup)

    try:
        # Step 1: Start all services
        print("\n1. Starting all microservices with supervisor.start_all()...")
        sup.start_all()
        time.sleep(1.0)
        st = sup.status()
        for name, d in st.items():
            print(f"  {name:<12}: PID={d['pid']}, status={d['status']}, healthy={d['healthy']}")
            assert d["healthy"] is True, f"Service {name} is not healthy!"

        # Step 2: Test Failure Injectors
        print("\n2. Testing Failure 1: Latency injection...")
        res_lat = engine.inject_latency("payments", latency_ms=1500.0)
        print(f"  Result: {res_lat}")
        assert res_lat["action"] == "chaos_latency"

        print("\n3. Testing Failure 2: Error burst injection...")
        res_err = engine.inject_error_burst("orders", error_rate=0.4)
        print(f"  Result: {res_err}")
        assert res_err["action"] == "chaos_error_burst"

        print("\n4. Testing Failure 3: Memory leak injection...")
        res_leak = engine.inject_memory_leak("orders", leak_mb=50)
        print(f"  Result: {res_leak}")
        assert res_leak["action"] == "chaos_memory_leak"

        print("\n5. Testing Failure 4: CPU hog injection...")
        res_cpu = engine.inject_cpu_hog("gateway")
        print(f"  Result: {res_cpu}")
        assert res_cpu["action"] == "chaos_cpu_hog"

        print("\n6. Testing Failure 5: DB lock injection...")
        res_lock = engine.inject_db_lock(duration_seconds=5.0)
        print(f"  Result: {res_lock}")
        assert res_lock["action"] == "chaos_db_lock"
        time.sleep(0.5)
        released = engine.release_db_lock()
        print(f"  DB Lock released: {released}")

        print("\n7. Testing Failure 6: Crash service (kill -9)...")
        res_crash = engine.inject_crash("payments")
        print(f"  Result: {res_crash}")
        assert res_crash["action"] == "chaos_crash"
        st = sup.status()
        assert st["payments"]["healthy"] is False

        print("\n8. Testing Failure 7: Bad deploy (v2)...")
        res_deploy = engine.inject_bad_deploy("payments")
        print(f"  Result: {res_deploy}")
        assert res_deploy["action"] == "bad_deploy"
        assert sup._read_version("payments") == "2.0.0"

        print("\n9. Testing Failure 8: Dependency down...")
        res_dep = engine.inject_dependency_down("inventory")
        print(f"  Result: {res_dep}")
        assert res_dep["action"] == "chaos_dependency_down"
        assert sup.status()["inventory"]["status"] == "stopped"

        # Step 3: Clear all chaos
        print("\n10. Clearing all chaos and restoring baseline...")
        res_clear = engine.clear_all()
        print(f"  Result: {res_clear}")
        time.sleep(1.0)
        st_after = sup.status()
        for name, d in st_after.items():
            print(f"  Recovered {name:<10}: status={d['status']}, version={d['version']}, healthy={d['healthy']}")
            assert d["healthy"] is True
            assert d["version"] == "1.0.0"

        # Step 4: Random chaos selector
        print("\n11. Testing hidden random chaos selector...")
        res_rand = engine.inject_random()
        print(f"  Random failure chosen: {res_rand.get('chosen_failure')} -> {res_rand}")
        assert "chosen_failure" in res_rand

        # Clear again
        engine.clear_all()

        # Step 5: Verify changes.jsonl
        print("\n12. Verifying audit change log (data/changes.jsonl)...")
        changes_file = REPO_ROOT / "data" / "changes.jsonl"
        assert changes_file.is_file(), "data/changes.jsonl does not exist!"
        lines = changes_file.read_text(encoding="utf-8").strip().splitlines()
        print(f"  Total change records logged: {len(lines)}")
        for line in lines[-5:]:
            parsed = json.loads(line)
            print(f"    [{parsed['id']}] {parsed['ts']} | {parsed['action']} on {parsed['service']} by {parsed['author']}")

        print("\nALL PHASE 2 VERIFICATION CHECKS PASSED SUCCESSFULLY!")
        return 0

    finally:
        print("\nStopping all services...")
        sup.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
