"""Tests for sre/supervisor.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sre.supervisor import ServiceInfo, Supervisor


@pytest.fixture
def test_supervisor(tmp_path: Path):
    changes_log = tmp_path / "changes.jsonl"
    state_file = tmp_path / "supervisor_state.json"
    repo_root = Path(__file__).resolve().parent.parent

    sup = Supervisor(
        repo_root=repo_root,
        changes_log=changes_log,
        state_file=state_file,
    )
    return sup


def test_supervisor_registry_and_validation(test_supervisor: Supervisor):
    """Supervisor loads services from dependencies.yaml and validates service names."""
    assert "gateway" in test_supervisor.registry
    assert "orders" in test_supervisor.registry
    assert "payments" in test_supervisor.registry
    assert "inventory" in test_supervisor.registry

    # Validation succeeds for known services
    test_supervisor.validate_service("gateway")
    test_supervisor.validate_service("payments")

    # Raises ValueError on unknown services
    with pytest.raises(ValueError, match="Unknown service 'unknown_svc'"):
        test_supervisor.validate_service("unknown_svc")


def test_supervisor_log_change_sequential_ids(test_supervisor: Supervisor):
    """Every change logged appends to changes.jsonl with a sequential CHG-x ID."""
    e1 = test_supervisor.log_change(action="start", service="gateway", details={"pid": 1234})
    assert e1["id"] == "CHG-1"
    assert e1["action"] == "start"
    assert e1["service"] == "gateway"

    e2 = test_supervisor.log_change(action="restart", service="orders", details={"pid": 5678})
    assert e2["id"] == "CHG-2"

    e3 = test_supervisor.log_change(action="stop", service="payments")
    assert e3["id"] == "CHG-3"

    # Verify file content
    lines = test_supervisor.changes_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert [p["id"] for p in parsed] == ["CHG-1", "CHG-2", "CHG-3"]


def test_supervisor_status(test_supervisor: Supervisor):
    """Supervisor status returns all registered services and health flags."""
    st = test_supervisor.status()
    assert len(st) >= 4
    for name in ("gateway", "orders", "payments", "inventory"):
        assert name in st
        assert "port" in st[name]
        assert "status" in st[name]
        assert "healthy" in st[name]
        assert "version" in st[name]


def test_supervisor_start_stop_mock(test_supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch):
    """Supervisor start, stop, restart methods update state and log changes."""
    # Mock subprocess.Popen and os.kill
    class DummyProc:
        pid = 99999

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: DummyProc())
    monkeypatch.setattr(test_supervisor, "is_pid_alive", lambda pid: pid == 99999)
    monkeypatch.setattr(test_supervisor, "check_health", lambda svc, timeout=1.0: True)

    info = test_supervisor.start_service("payments")
    assert info.pid == 99999
    assert info.status == "running"

    # Check status
    st = test_supervisor.status()
    assert st["payments"]["pid"] == 99999
    assert st["payments"]["status"] == "running"

    # Stop service
    stopped = test_supervisor.stop_service("payments")
    assert stopped.pid is None
    assert stopped.status == "stopped"

    # Restart service
    restarted = test_supervisor.restart_service("payments")
    assert restarted.pid == 99999
    assert restarted.status == "running"
