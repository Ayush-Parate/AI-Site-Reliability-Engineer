"""Tests for Phase 6: Fix Pipeline and Approval Flow (sre/fixer.py)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sre.detector import Incident, IncidentDetector
from sre.fixer import (
    ASK_FIRST_ACTIONS,
    AUTO_FIX_ACTIONS,
    NEVER_DO_ACTIONS,
    Fixer,
)
from sre.observability import Observability, format_iso
from sre.supervisor import ServiceInfo, Supervisor


@pytest.fixture
def fixer_env(tmp_path: Path):
    db_file = tmp_path / "obs.db"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    changes_file = tmp_path / "changes.jsonl"
    deps_file = tmp_path / "config" / "dependencies.yaml"
    deps_file.parent.mkdir(parents=True)
    slo_file = tmp_path / "config" / "slo.yaml"
    for svc in ("gateway", "orders", "payments", "inventory"):
        svc_dir = tmp_path / "services" / svc
        svc_dir.mkdir(parents=True)
        (svc_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    deps_file.write_text(
        """
services:
  gateway:
    port: 8000
    calls: [orders]
  orders:
    port: 8001
    calls: [payments, inventory]
  payments:
    port: 8002
    calls: []
  inventory:
    port: 8003
    calls: []
""",
        encoding="utf-8",
    )
    slo_file.write_text(
        """
error_rate_threshold: 0.05
latency_p95_slo_ms: 2000
recover_after_seconds: 1.0
""",
        encoding="utf-8",
    )

    obs = Observability(
        db_path=db_file,
        logs_dir=logs_dir,
        changes_path=changes_file,
        deps_path=deps_file,
        slo_path=slo_file,
        repo_root=tmp_path,
    )
    sup = Supervisor(repo_root=tmp_path, changes_log=changes_file)
    detector = IncidentDetector(
        observability=obs,
        supervisor=sup,
        slo_path=slo_file,
        deps_path=deps_file,
        db_path=db_file,
    )

    # Stub live process operations
    def fake_start(service: str, timeout: float = 8.0):
        info = ServiceInfo(
            name=service,
            port=sup.registry[service]["port"],
            pid=1000 + abs(hash(service)) % 1000,
            start_time=format_iso(),
            version=sup._read_version(service),
            status="running",
        )
        sup.state[service] = info
        return info

    def fake_stop(service: str, sig: int = 15):
        info = sup.state.get(service) or ServiceInfo(
            name=service, port=sup.registry[service]["port"]
        )
        info.pid = None
        info.status = "stopped"
        sup.state[service] = info
        return info

    def fake_restart(service: str):
        fake_stop(service)
        return fake_start(service)

    def fake_status():
        out = {}
        for name, cfg in sup.registry.items():
            info = sup.state.get(name) or ServiceInfo(name=name, port=cfg["port"])
            out[name] = {
                "service": name,
                "port": cfg["port"],
                "pid": info.pid,
                "version": info.version,
                "status": info.status,
                "healthy": info.status == "running" and info.pid is not None,
                "calls": cfg.get("calls", []),
            }
        return out

    def fake_health(service: str | None = None):
        if service:
            st = fake_status().get(service, {})
            return {
                "service": service,
                "healthy": st.get("healthy", False),
                "status": "ok" if st.get("healthy") else "down",
                "port": st.get("port"),
            }
        services = fake_status()
        return {
            "healthy": all(s.get("healthy") for s in services.values()),
            "services": {
                n: {
                    "service": n,
                    "healthy": s["healthy"],
                    "status": "ok" if s["healthy"] else "down",
                    "port": s["port"],
                }
                for n, s in services.items()
            },
        }

    sup.start_service = fake_start  # type: ignore[method-assign]
    sup.stop_service = fake_stop  # type: ignore[method-assign]
    sup.restart_service = fake_restart  # type: ignore[method-assign]
    sup.status = fake_status  # type: ignore[method-assign]
    obs.get_health = fake_health  # type: ignore[method-assign]
    obs.error_rate = MagicMock(return_value=0.0)  # type: ignore[method-assign]
    obs.p95_latency = MagicMock(return_value=50.0)  # type: ignore[method-assign]
    obs.poll_service = MagicMock(return_value={"health": "ok"})  # type: ignore[method-assign]

    chaos = MagicMock()
    chaos.clear_service = MagicMock(
        return_value={"id": "CHG-clear", "action": "chaos_clear", "service": "payments"}
    )

    fixer = Fixer(
        supervisor=sup,
        chaos=chaos,
        observability=obs,
        detector=detector,
        db_path=db_file,
        repo_root=tmp_path,
        verify_timeout=2.0,
        approval_timeout=1.0,
        circuit_max=2,
        circuit_window=600,
    )

    t0 = time.time() - 30
    incident = Incident(
        id="INC-FIX-1",
        status="diagnosing",
        trigger_rule="consecutive_health_failures",
        t0=format_iso(t0),
        t0_unix=t0,
        created_at=format_iso(),
        created_at_unix=time.time(),
        ttd_seconds=5.0,
        affected_services=["payments"],
        evidence=["MET-1"],
        metadata={},
    )
    detector.save_incident(incident)

    yield {
        "fixer": fixer,
        "sup": sup,
        "obs": obs,
        "detector": detector,
        "chaos": chaos,
        "incident": incident,
        "tmp_path": tmp_path,
        "changes_file": changes_file,
    }


def test_tier_classification():
    fixer = Fixer.__new__(Fixer)
    for action in AUTO_FIX_ACTIONS:
        assert Fixer.classify_tier(action) == "auto_fix"
    for action in ASK_FIRST_ACTIONS:
        assert Fixer.classify_tier(action) == "ask_first"
    for action in NEVER_DO_ACTIONS:
        assert Fixer.classify_tier(action) == "never_do"
    assert Fixer.classify_tier("drop_database") == "never_do"
    assert Fixer.classify_tier("rm_rf") == "never_do"
    assert Fixer.classify_tier("totally_unknown") == "unknown"


def test_never_do_blocked_and_logged(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    incident = fixer_env["incident"]
    changes_file: Path = fixer_env["changes_file"]

    result = fixer.apply_fix(
        {"action": "drop_db", "args": {"service": "inventory"}},
        incident=incident,
    )
    assert result.status == "blocked"
    assert result.tier == "never_do"
    assert result.change_ids

    log = changes_file.read_text(encoding="utf-8")
    assert "fix_blocked" in log
    assert "drop_db" in log


def test_prompt_injection_style_action_blocked(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    result = fixer.apply_fix(
        {"action": "delete_data", "args": {"service": "inventory", "path": "/tmp/x"}},
        incident=fixer_env["incident"],
    )
    assert result.status == "blocked"


def test_path_outside_app_blocked(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    result = fixer.apply_fix(
        {
            "action": "change_config",
            "args": {
                "service": "orders",
                "key": "x",
                "value": 1,
                "path": "/etc/passwd",
            },
        },
        incident=fixer_env["incident"],
        approved_by="tester",  # would be ask-first otherwise
    )
    assert result.status == "blocked"
    assert "outside" in result.message.lower()


def test_auto_restart_verified(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    detector: IncidentDetector = fixer_env["detector"]
    incident = fixer_env["incident"]

    # Simulate crashed payments
    fixer.supervisor.state["payments"] = ServiceInfo(
        name="payments", port=8002, pid=None, status="crashed", version="1.0.0"
    )

    result = fixer.apply_fix(
        {"action": "restart_service", "args": {"service": "payments"}},
        incident=incident,
    )
    assert result.status == "verified"
    assert result.tier == "auto_fix"
    assert result.snapshot is not None
    assert result.verify_result and result.verify_result["recovered"] is True
    assert result.t2_unix is not None

    refreshed = detector.get_incident(incident.id)
    assert refreshed is not None
    assert refreshed.status == "resolved"
    assert refreshed.metadata.get("t2") == result.t2


def test_clear_chaos_flag_auto(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    chaos = fixer_env["chaos"]
    fixer.supervisor.state["orders"] = ServiceInfo(
        name="orders", port=8001, pid=42, status="running", version="1.0.0"
    )
    result = fixer.apply_fix(
        {"action": "clear_chaos_flag", "args": {"service": "orders"}},
        incident=fixer_env["incident"],
    )
    assert result.status == "verified"
    chaos.clear_service.assert_called_once_with("orders")


def test_rotate_logs_auto(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    tmp_path: Path = fixer_env["tmp_path"]
    log_path = tmp_path / "logs" / "gateway.log"
    log_path.write_text("old line\n", encoding="utf-8")
    fixer.supervisor.state["gateway"] = ServiceInfo(
        name="gateway", port=8000, pid=7, status="running", version="1.0.0"
    )

    result = fixer.apply_fix(
        {"action": "rotate_logs", "args": {"service": "gateway"}},
        incident=fixer_env["incident"],
    )
    assert result.status == "verified"
    assert log_path.read_text(encoding="utf-8") == ""
    assert result.apply_result and result.apply_result.get("rotated_to")


def test_ask_first_creates_pending_approval(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    result = fixer.apply_fix(
        {
            "action": "rollback",
            "args": {"service": "payments", "to_version": "1.0.0"},
        },
        incident=fixer_env["incident"],
    )
    assert result.status == "pending_approval"
    assert result.approval_id
    pending = fixer.list_approvals(status="pending")
    assert len(pending) == 1
    assert pending[0].action == "rollback"


def test_approve_and_execute_rollback(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    tmp_path: Path = fixer_env["tmp_path"]
    ver = tmp_path / "services" / "payments" / "VERSION"
    ver.write_text("2.0.0\n", encoding="utf-8")
    fixer.supervisor.state["payments"] = ServiceInfo(
        name="payments", port=8002, pid=9, status="running", version="2.0.0"
    )

    pending = fixer.apply_fix(
        {"action": "rollback", "args": {"service": "payments", "to_version": "1.0.0"}},
        incident=fixer_env["incident"],
    )
    assert pending.status == "pending_approval"

    result = fixer.execute_approved(pending.approval_id, decided_by="judge")
    assert result.status == "verified"
    assert result.tier == "ask_first"
    assert ver.read_text(encoding="utf-8").strip() == "1.0.0"
    assert result.apply_result["from_version"] == "2.0.0"
    assert result.apply_result["to_version"] == "1.0.0"

    approval = fixer.get_approval(pending.approval_id)
    assert approval is not None
    assert approval.status == "approved"
    assert approval.decided_by == "judge"


def test_deny_escalates(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    detector: IncidentDetector = fixer_env["detector"]

    pending = fixer.apply_fix(
        {"action": "scale_up", "args": {"service": "orders", "replicas": 3}},
        incident=fixer_env["incident"],
        wait_for_approval=False,
    )
    assert pending.status == "pending_approval"
    fixer.deny(pending.approval_id, decided_by="human", reason="not needed")

    # Drive wait path with already-denied approval via short wait on new request
    # Instead re-apply wait that sees deny through parallel approve thread pattern:
    # Use wait_for_approval on denied id
    decided = fixer.wait_for_approval(pending.approval_id, timeout=0.2)
    assert decided.status == "denied"

    # Escalation when applying with wait and deny:
    # create new pending, deny from another thread while waiting
    def deny_soon():
        time.sleep(0.15)
        latest = fixer.list_approvals(status="pending")
        if latest:
            fixer.deny(latest[0].id, decided_by="human")

    # Reset incident status
    detector.update_incident_status(fixer_env["incident"].id, "diagnosing")
    threading.Thread(target=deny_soon, daemon=True).start()
    result = fixer.apply_fix(
        {"action": "change_config", "args": {"service": "orders", "key": "x", "value": 1}},
        incident=fixer_env["incident"],
        wait_for_approval=True,
        approval_timeout=2.0,
    )
    assert result.status in ("denied", "escalated")
    refreshed = detector.get_incident(fixer_env["incident"].id)
    assert refreshed is not None
    assert refreshed.status == "escalated"


def test_circuit_breaker_trips_and_proposes_rollback(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    fixer.supervisor.state["payments"] = ServiceInfo(
        name="payments", port=8002, pid=1, status="running", version="1.0.0"
    )

    r1 = fixer.apply_fix(
        {"action": "restart_service", "args": {"service": "payments"}},
        incident=fixer_env["incident"],
    )
    assert r1.status == "verified"
    # Re-open incident for subsequent fixes
    fixer_env["detector"].update_incident_status(fixer_env["incident"].id, "diagnosing")

    r2 = fixer.apply_fix(
        {"action": "restart_service", "args": {"service": "payments"}},
        incident=fixer_env["incident"],
    )
    assert r2.status == "verified"
    fixer_env["detector"].update_incident_status(fixer_env["incident"].id, "diagnosing")

    assert fixer.circuit_open("payments") is True
    r3 = fixer.apply_fix(
        {"action": "restart_service", "args": {"service": "payments"}},
        incident=fixer_env["incident"],
    )
    assert r3.status == "escalated"
    assert r3.approval_id is not None
    approval = fixer.get_approval(r3.approval_id)
    assert approval is not None
    assert approval.action == "rollback"


def test_failed_verify_escalates(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    detector: IncidentDetector = fixer_env["detector"]

    # Restart "succeeds" but health stays down
    def bad_start(service: str, timeout: float = 8.0):
        info = ServiceInfo(
            name=service,
            port=fixer.supervisor.registry[service]["port"],
            pid=55,
            status="unhealthy",
            version="1.0.0",
        )
        fixer.supervisor.state[service] = info
        return info

    fixer.supervisor.start_service = bad_start  # type: ignore[method-assign]
    fixer.supervisor.restart_service = lambda s: bad_start(s)  # type: ignore[method-assign]
    fixer.obs.get_health = MagicMock(  # type: ignore[method-assign]
        return_value={"service": "payments", "healthy": False, "status": "down"}
    )
    fixer.supervisor.status = MagicMock(  # type: ignore[method-assign]
        return_value={
            "payments": {
                "service": "payments",
                "port": 8002,
                "pid": 55,
                "version": "1.0.0",
                "status": "unhealthy",
                "healthy": False,
                "calls": [],
            }
        }
    )

    result = fixer.apply_fix(
        {"action": "restart_service", "args": {"service": "payments"}},
        incident=fixer_env["incident"],
    )
    assert result.status == "escalated"
    refreshed = detector.get_incident(fixer_env["incident"].id)
    assert refreshed is not None
    assert refreshed.status == "escalated"


def test_apply_diagnosis_helper(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    fixer.supervisor.state["payments"] = ServiceInfo(
        name="payments", port=8002, pid=None, status="crashed", version="1.0.0"
    )
    diagnosis = {
        "incident_id": fixer_env["incident"].id,
        "proposed_fix": {
            "action": "restart_service",
            "args": {"service": "payments"},
        },
    }
    result = fixer.apply_diagnosis(diagnosis)
    assert result.status == "verified"


def test_approval_timeout_escalates(fixer_env):
    fixer: Fixer = fixer_env["fixer"]
    fixer.approval_timeout = 0.3
    result = fixer.apply_fix(
        {"action": "rollback", "args": {"service": "payments"}},
        incident=fixer_env["incident"],
        wait_for_approval=True,
        approval_timeout=0.3,
    )
    assert result.status == "escalated"
    assert "timed out" in result.message.lower()
