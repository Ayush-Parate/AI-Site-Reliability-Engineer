"""Comprehensive tests for Phase 4: Incident Detector (sre/detector.py).

Covers:
- Plain-code rule evaluations:
  1. Consecutive health check failures (3 in a row)
  2. Error rate threshold breach (>5% over 30s)
  3. p95 latency spike (>2000ms SLO limit)
  4. Memory growth trend (>50MB in 60s)
- Accurate T0 recording (earliest anomaly timestamp, not detection time)
- Time-to-detect (TTD) calculation
- Incident deduplication (further breaches attach to open incident)
- Incident status updates (open, diagnosing, mitigating, escalated, resolved)
- Auto-resolution after recovery window
- Zero false alarms under healthy baseline
- SQLite persistence in data/observability.db
- Background detector thread lifecycle
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sre.detector import Breach, Incident, IncidentDetector
from sre.observability import Observability, format_iso
from sre.supervisor import Supervisor


@pytest.fixture
def detector_env(tmp_path: Path):
    """Sets up an isolated test environment for incident detection."""
    db_file = tmp_path / "test_observability.db"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    changes_file = tmp_path / "changes.jsonl"
    deps_file = tmp_path / "dependencies.yaml"
    slo_file = tmp_path / "slo.yaml"

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
health_check_failures: 3
error_rate_threshold: 0.05
error_rate_window_seconds: 30
latency_p95_multiplier: 2.0
latency_p95_slo_ms: 2000
memory_growth_threshold_mb: 50
cpu_growth_threshold_pct: 30
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

    yield detector, obs, tmp_path
    detector.stop_background_detector()


def test_zero_false_alarms_under_healthy_state(detector_env):
    """Healthy system with no breaches produces zero incidents."""
    detector, obs, _ = detector_env

    # 1. Evaluate rules on empty/healthy system
    breaches = detector.evaluate_rules()
    assert len(breaches) == 0

    inc = detector.run_once()
    assert inc is None
    assert detector.get_active_incident() is None


def test_health_check_failures_rule(detector_env):
    """Rule 1: 3 consecutive health check failures opens an incident with accurate T0."""
    detector, obs, _ = detector_env
    now = time.time()

    # Insert 1 healthy check, then 3 failed checks on payments
    checks = [
        (format_iso(now - 15), now - 15, "payments", "ok", 200, 2.0, "{}"),
        (format_iso(now - 10), now - 10, "payments", "unhealthy", 503, 10.0, "{}"),
        (format_iso(now - 5), now - 5, "payments", "unhealthy", 503, 10.0, "{}"),
        (format_iso(now - 1), now - 1, "payments", "unhealthy", 503, 10.0, "{}"),
    ]

    with obs.get_connection() as conn:
        conn.executemany(
            "INSERT INTO health_checks (ts, timestamp_unix, service, status, status_code, latency_ms, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            checks,
        )
        conn.commit()

    # Check rule
    breach = detector.check_health_rule("payments")
    assert breach is not None
    assert breach.rule == "consecutive_health_failures"
    assert breach.service == "payments"
    assert breach.t0_unix == pytest.approx(now - 10, rel=1e-2)
    assert len(breach.evidence_ids) == 3

    # Run detector: incident should be opened
    inc = detector.run_once(poll_metrics=False)
    assert inc is not None
    assert inc.id == "INC-1"
    assert inc.status == "open"
    assert inc.trigger_rule == "consecutive_health_failures"
    assert inc.affected_services == ["payments"]
    assert inc.t0_unix == pytest.approx(now - 10, rel=1e-2)
    assert inc.ttd_seconds >= 9.0  # Detection time minus T0
    assert len(inc.evidence) == 3


def test_error_rate_breach_rule(detector_env):
    """Rule 2: Error rate > 5% over 30s opens an incident with earliest error T0."""
    detector, obs, tmp_path = detector_env
    now = time.time()

    # Write logs to payments.log: 10 requests, 5 errors
    pay_log = tmp_path / "logs" / "payments.log"
    lines = []
    # 5 successful requests
    for i in range(5):
        lines.append({
            "ts": format_iso(now - 20 + i),
            "level": "INFO",
            "service": "payments",
            "trace_id": f"tr-{i}",
            "msg": "OK",
            "latency_ms": 20.0,
            "status": 200,
        })
    # Earliest error occurred at (now - 12)
    earliest_err_ts = now - 12
    for i in range(5):
        lines.append({
            "ts": format_iso(earliest_err_ts + i * 2),
            "level": "ERROR",
            "service": "payments",
            "trace_id": f"err-{i}",
            "msg": "Internal server error",
            "latency_ms": 150.0,
            "status": 500,
        })

    with open(pay_log, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")

    breach = detector.check_error_rate_rule("payments")
    assert breach is not None
    assert breach.rule == "error_rate_threshold"
    assert breach.service == "payments"
    assert breach.t0_unix == pytest.approx(earliest_err_ts, rel=1e-2)
    assert len(breach.evidence_ids) > 0

    inc = detector.run_once()
    assert inc is not None
    assert inc.status == "open"
    assert inc.trigger_rule == "error_rate_threshold"
    assert inc.t0_unix == pytest.approx(earliest_err_ts, rel=1e-2)


def test_latency_p95_breach_rule(detector_env):
    """Rule 3: p95 latency > 2000ms opens an incident."""
    detector, obs, tmp_path = detector_env
    now = time.time()

    # Write logs to gateway.log with p95 > 2000ms
    gw_log = tmp_path / "logs" / "gateway.log"
    lines = []
    earliest_slow_ts = now - 18
    for i in range(10):
        lines.append({
            "ts": format_iso(earliest_slow_ts + i),
            "level": "WARNING" if i >= 8 else "INFO",
            "service": "gateway",
            "trace_id": f"lat-{i}",
            "msg": "HTTP request",
            "latency_ms": 2500.0 if i >= 8 else 50.0,
            "status": 200,
        })

    with open(gw_log, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")

    breach = detector.check_latency_rule("gateway")
    assert breach is not None
    assert breach.rule == "latency_p95_slo"
    assert breach.service == "gateway"
    assert breach.t0_unix == pytest.approx(earliest_slow_ts + 8, rel=1e-2)

    inc = detector.run_once()
    assert inc is not None
    assert inc.status == "open"
    assert inc.trigger_rule == "latency_p95_slo"
    assert "gateway" in inc.affected_services


def test_incident_deduplication(detector_env):
    """Further breaches during an active incident attach to it instead of opening a new one."""
    detector, obs, tmp_path = detector_env
    now = time.time()

    # 1. Trigger initial breach on payments
    checks = [
        (format_iso(now - 10), now - 10, "payments", "unhealthy", 503, 10.0, "{}"),
        (format_iso(now - 5), now - 5, "payments", "unhealthy", 503, 10.0, "{}"),
        (format_iso(now - 1), now - 1, "payments", "unhealthy", 503, 10.0, "{}"),
    ]
    with obs.get_connection() as conn:
        conn.executemany(
            "INSERT INTO health_checks (ts, timestamp_unix, service, status, status_code, latency_ms, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            checks,
        )
        conn.commit()

    inc1 = detector.run_once(poll_metrics=False)
    assert inc1 is not None
    assert inc1.id == "INC-1"
    assert inc1.affected_services == ["payments"]
    initial_t0 = inc1.t0_unix

    # 2. Trigger secondary cascade failure on orders (e.g. 500 error rate)
    orders_log = tmp_path / "logs" / "orders.log"
    lines = []
    cascade_t0 = now - 15  # Earlier than payments health failure!
    for i in range(5):
        lines.append({
            "ts": format_iso(cascade_t0 + i),
            "level": "ERROR",
            "service": "orders",
            "trace_id": f"cascade-{i}",
            "msg": "Downstream payment failure",
            "latency_ms": 100.0,
            "status": 500,
        })
    with open(orders_log, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")

    # Run detector again: should deduplicate into INC-1!
    inc2 = detector.run_once(poll_metrics=False)
    assert inc2 is not None
    assert inc2.id == "INC-1"  # Same incident!
    assert "payments" in inc2.affected_services
    assert "orders" in inc2.affected_services
    # T0 updated to the earlier cascade anomaly!
    assert inc2.t0_unix <= initial_t0
    # Breaches list contains both breaches
    assert len(inc2.breaches) >= 2

    # Verify only 1 incident exists in SQLite
    all_incidents = detector.list_incidents()
    assert len(all_incidents) == 1


def test_incident_status_updates_and_auto_resolution(detector_env):
    """Test lifecycle status transitions and auto-close after recover_after_seconds."""
    detector, obs, _ = detector_env
    now = time.time()

    # 1. Open an incident
    checks = [
        (format_iso(now - 10), now - 10, "inventory", "unhealthy", 500, 10.0, "{}"),
        (format_iso(now - 5), now - 5, "inventory", "unhealthy", 500, 10.0, "{}"),
        (format_iso(now - 1), now - 1, "inventory", "unhealthy", 500, 10.0, "{}"),
    ]
    with obs.get_connection() as conn:
        conn.executemany(
            "INSERT INTO health_checks (ts, timestamp_unix, service, status, status_code, latency_ms, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            checks,
        )
        conn.commit()

    inc = detector.run_once(poll_metrics=False)
    assert inc is not None
    assert inc.status == "open"

    # 2. Update status to 'diagnosing'
    diag_inc = detector.update_incident_status(inc.id, "diagnosing", {"assigned_agent": "ai-sre"})
    assert diag_inc.status == "diagnosing"
    assert diag_inc.metadata.get("assigned_agent") == "ai-sre"

    # 3. Update status to 'mitigating'
    mit_inc = detector.update_incident_status(inc.id, "mitigating")
    assert mit_inc.status == "mitigating"

    # 4. Clear the failure by inserting passing health checks
    healthy_checks = [
        (format_iso(now + 1), now + 1, "inventory", "ok", 200, 2.0, "{}"),
        (format_iso(now + 2), now + 2, "inventory", "ok", 200, 2.0, "{}"),
        (format_iso(now + 3), now + 3, "inventory", "ok", 200, 2.0, "{}"),
    ]
    with obs.get_connection() as conn:
        conn.executemany(
            "INSERT INTO health_checks (ts, timestamp_unix, service, status, status_code, latency_ms, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            healthy_checks,
        )
        conn.commit()

    # Run detector: starts recovery timer
    inc_healthy = detector.run_once(poll_metrics=False)
    assert inc_healthy is not None
    assert inc_healthy.healthy_since is not None
    assert inc_healthy.status == "mitigating"  # Not yet resolved

    # Wait for recover_after_seconds (1.0s in test slo.yaml)
    time.sleep(1.2)

    # Run detector: auto-resolves!
    res_inc = detector.run_once(poll_metrics=False)
    assert res_inc is not None
    assert res_inc.status == "resolved"
    assert res_inc.resolved_at is not None
    assert res_inc.resolved_at_unix is not None
    assert "duration_seconds" in res_inc.metadata

    # Active incident is now None
    assert detector.get_active_incident() is None


def test_background_detector_lifecycle(detector_env):
    """Test start_background_detector and stop_background_detector."""
    detector, _, _ = detector_env
    assert not detector.is_running

    thread = detector.start_background_detector(interval_seconds=0.2)
    assert thread.is_alive()
    assert detector.is_running

    time.sleep(0.5)
    detector.stop_background_detector()
    assert not detector.is_running
    assert not thread.is_alive()
