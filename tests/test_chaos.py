"""Tests for sre/chaos.py failure injectors and random chaos."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sre.chaos import ChaosEngine
from sre.supervisor import Supervisor


@pytest.fixture
def chaos_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    changes_log = tmp_path / "changes.jsonl"
    state_file = tmp_path / "supervisor_state.json"
    repo_root = Path(__file__).resolve().parent.parent

    # Isolate DB lock file
    monkeypatch.setattr("sre.chaos.LOCK_PID_FILE", tmp_path / "db_lock.pid")

    sup = Supervisor(
        repo_root=repo_root,
        changes_log=changes_log,
        state_file=state_file,
    )
    engine = ChaosEngine(supervisor=sup)
    return engine


def test_inject_crash(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Chaos crash sends SIGKILL to target service PID and logs to changes.jsonl."""
    # Pre-populate running PID
    chaos_engine.supervisor.state["payments"].pid = 88888
    killed_signals = []

    def mock_kill(pid, sig):
        killed_signals.append((pid, sig))

    monkeypatch.setattr("os.kill", mock_kill)
    monkeypatch.setattr(chaos_engine.supervisor, "is_pid_alive", lambda pid: True)

    res = chaos_engine.inject_crash("payments")
    assert res["action"] == "chaos_crash"
    assert res["service"] == "payments"
    assert res["author"] == "chaos"
    assert res["id"].startswith("CHG-")
    assert (88888, 9) in killed_signals  # SIGKILL is 9


def test_inject_latency(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Chaos latency calls service /chaos with latency_ms and records change."""
    called_payloads = []

    def mock_send(service, payload):
        called_payloads.append((service, payload))
        return {"latency_ms": payload.get("latency_ms")}

    monkeypatch.setattr(chaos_engine, "_send_chaos_http", mock_send)

    res = chaos_engine.inject_latency("payments", latency_ms=1800.0)
    assert res["action"] == "chaos_latency"
    assert res["service"] == "payments"
    assert called_payloads == [("payments", {"latency_ms": 1800.0})]


def test_inject_error_burst(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Chaos error burst calls service /chaos with error_rate and records change."""
    called_payloads = []

    def mock_send(service, payload):
        called_payloads.append((service, payload))
        return {"error_rate": payload.get("error_rate")}

    monkeypatch.setattr(chaos_engine, "_send_chaos_http", mock_send)

    res = chaos_engine.inject_error_burst("orders", error_rate=0.7)
    assert res["action"] == "chaos_error_burst"
    assert res["service"] == "orders"
    assert called_payloads == [("orders", {"error_rate": 0.7})]


def test_inject_memory_leak(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Chaos memory leak calls service /chaos with leak_mb and records change."""
    called_payloads = []

    def mock_send(service, payload):
        called_payloads.append((service, payload))
        return {"leak_mb_total": payload.get("leak_mb")}

    monkeypatch.setattr(chaos_engine, "_send_chaos_http", mock_send)

    res = chaos_engine.inject_memory_leak("orders", leak_mb=250)
    assert res["action"] == "chaos_memory_leak"
    assert res["service"] == "orders"
    assert called_payloads == [("orders", {"leak_mb": 250})]


def test_inject_cpu_hog(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Chaos CPU hog calls service /chaos with cpu_burn and records change."""
    called_payloads = []

    def mock_send(service, payload):
        called_payloads.append((service, payload))
        return {"cpu_burn": True}

    monkeypatch.setattr(chaos_engine, "_send_chaos_http", mock_send)

    res = chaos_engine.inject_cpu_hog("gateway")
    assert res["action"] == "chaos_cpu_hog"
    assert res["service"] == "gateway"
    assert called_payloads == [("gateway", {"cpu_burn": True})]


def test_inject_bad_deploy_and_clear(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Bad deploy bumps VERSION to 2.0.0, restarts, and clear restores 1.0.0."""
    # Use temporary VERSION file
    ver_dir = tmp_path / "services" / "payments"
    ver_dir.mkdir(parents=True, exist_ok=True)
    ver_file = ver_dir / "VERSION"
    ver_file.write_text("1.0.0\n")

    monkeypatch.setattr(chaos_engine, "repo_root", tmp_path)
    monkeypatch.setattr(chaos_engine.supervisor, "repo_root", tmp_path)
    monkeypatch.setattr(chaos_engine.supervisor, "restart_service", lambda svc: type("obj", (), {"pid": 77777})())
    monkeypatch.setattr(chaos_engine, "_reset_chaos_http", lambda svc: {"ok": True})
    monkeypatch.setattr(chaos_engine.supervisor, "start_service", lambda svc: type("obj", (), {"pid": 77777})())

    # 1. Bad deploy
    res = chaos_engine.inject_bad_deploy("payments")
    assert res["action"] == "bad_deploy"
    assert res["service"] == "payments"
    assert ver_file.read_text().strip() == "2.0.0"

    # 2. Clear
    clear_res = chaos_engine.clear_service("payments")
    assert clear_res["action"] == "chaos_clear"
    assert ver_file.read_text().strip() == "1.0.0"


def test_inject_dependency_down(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Dependency down stops target downstream service and records change."""
    stopped = []

    def mock_stop(svc):
        stopped.append(svc)
        return type("obj", (), {"status": "stopped"})()

    monkeypatch.setattr(chaos_engine.supervisor, "stop_service", mock_stop)

    res = chaos_engine.inject_dependency_down("inventory")
    assert res["action"] == "chaos_dependency_down"
    assert res["service"] == "inventory"
    assert stopped == ["inventory"]


def test_inject_db_lock_and_release(chaos_engine: ChaosEngine, tmp_path: Path):
    """DB lock launches lock process and release terminates it."""
    res = chaos_engine.inject_db_lock(duration_seconds=5.0)
    assert res["action"] == "chaos_db_lock"
    assert res["service"] == "inventory"

    # Verify lock pid file was created
    from sre.chaos import LOCK_PID_FILE
    assert LOCK_PID_FILE.is_file()

    # Release lock
    released = chaos_engine.release_db_lock()
    assert released is True
    assert not LOCK_PID_FILE.exists()


def test_inject_random_failure(chaos_engine: ChaosEngine, monkeypatch: pytest.MonkeyPatch):
    """Random failure injector executes one of the 8 failures and tags chosen_failure."""
    monkeypatch.setattr(chaos_engine, "_send_chaos_http", lambda *args: {"status": "ok"})
    monkeypatch.setattr(chaos_engine.supervisor, "start_service", lambda svc: type("obj", (), {"pid": 11111})())
    monkeypatch.setattr(chaos_engine.supervisor, "stop_service", lambda svc: type("obj", (), {"status": "stopped"})())
    monkeypatch.setattr(chaos_engine.supervisor, "restart_service", lambda svc: type("obj", (), {"pid": 22222})())
    monkeypatch.setattr(chaos_engine, "inject_db_lock", lambda *args, **kwargs: {"action": "chaos_db_lock", "id": "CHG-10"})

    res = chaos_engine.inject_random()
    assert "chosen_failure" in res
    valid_failures = {
        "crash", "latency", "error_burst", "memory_leak",
        "cpu_hog", "bad_deploy", "dependency_down", "db_lock",
    }
    assert res["chosen_failure"] in valid_failures
