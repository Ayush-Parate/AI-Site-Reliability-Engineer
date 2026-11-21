"""Comprehensive tests for Phase 3 Observability Layer (sre/observability.py).

Covers:
- Dependency graph: get_deps(service) upstream/downstream and full graph
- Changes query: get_changes(since, service) and CHG-x citation IDs
- Log reader: get_logs(service, since, level, trace_id) and LOG-x citation IDs
- Metric collector: poll /metrics and /health into SQLite (data/observability.db)
- Metric query: get_metrics(service, metric, window) with summary and MET-x citation IDs
- Derived metrics: error_rate(svc, window), p95_latency(svc, window), check_health()
- Evidence lookup: get_evidence(id) for LOG-x, MET-x, CHG-x
- Background collector start/stop lifecycle
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path

import pytest

from sre.observability import (
    DerivedMetricResult,
    Observability,
    format_iso,
    parse_since,
)


@pytest.fixture
def obs_env(tmp_path: Path):
    """Isolated test environment with test SQLite DB, logs, and changes."""
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
recover_after_seconds: 60
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
    yield obs, tmp_path
    obs.stop_background_collector()


def test_parse_since_helper():
    """Test parse_since handles relative seconds, ISO strings, and units."""
    now = time.time()
    # None
    assert parse_since(None, now_ts=now) == 0.0
    # Relative seconds as float/int
    assert abs(parse_since(30, now_ts=now) - (now - 30)) < 0.1
    # String units
    assert abs(parse_since("45s", now_ts=now) - (now - 45)) < 0.1
    assert abs(parse_since("2m", now_ts=now) - (now - 120)) < 0.1
    assert abs(parse_since("1h", now_ts=now) - (now - 3600)) < 0.1
    # ISO timestamp string
    iso = "2026-11-21T10:00:00Z"
    ts = parse_since(iso, now_ts=now)
    assert ts > 1_000_000_000


def test_dependency_graph(obs_env):
    """Test get_deps returns correct upstream and downstream dependencies."""
    obs, _ = obs_env

    # 1. Gateway
    gw = obs.get_deps("gateway")
    assert gw["service"] == "gateway"
    assert gw["downstream"] == ["orders"]
    assert gw["upstream"] == []

    # 2. Orders
    ord_deps = obs.get_deps("orders")
    assert ord_deps["downstream"] == ["payments", "inventory"]
    assert ord_deps["upstream"] == ["gateway"]

    # 3. Payments
    pay = obs.get_deps("payments")
    assert pay["downstream"] == []
    assert pay["upstream"] == ["orders"]

    # 4. Inventory
    inv = obs.get_deps("inventory")
    assert inv["downstream"] == []
    assert inv["upstream"] == ["orders"]

    # 5. Full graph
    all_deps = obs.get_deps()
    assert "services" in all_deps
    assert set(all_deps["services"].keys()) == {"gateway", "orders", "payments", "inventory"}

    # 6. Invalid service
    with pytest.raises(ValueError, match="Unknown service 'nonexistent'"):
        obs.get_deps("nonexistent")


def test_change_query_and_evidence_ids(obs_env):
    """Test get_changes filters by since and service, assigning CHG-x IDs."""
    obs, tmp_path = obs_env
    changes_file = tmp_path / "changes.jsonl"

    t0 = time.time()
    t_past = t0 - 100
    t_recent = t0 - 10

    records = [
        {
            "id": "CHG-1",
            "ts": format_iso(t_past),
            "action": "start",
            "service": "orders",
            "author": "supervisor",
            "details": {"pid": 1111},
        },
        {
            "id": "CHG-2",
            "ts": format_iso(t_recent),
            "action": "chaos_latency",
            "service": "payments",
            "author": "chaos",
            "details": {"latency_ms": 2500},
        },
        {
            "ts": format_iso(t_recent + 2),
            "action": "restart",
            "service": "payments",
            "author": "supervisor",
            "details": {"new_pid": 2222},
        },
    ]

    with open(changes_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    # 1. Retrieve all changes
    all_changes = obs.get_changes()
    assert len(all_changes) == 3
    # Newest first
    assert all_changes[0]["service"] == "payments"
    assert all_changes[0]["id"] == "CHG-3"  # Auto-generated ID if missing
    assert all_changes[1]["id"] == "CHG-2"
    assert all_changes[2]["id"] == "CHG-1"

    # 2. Filter by service
    pay_changes = obs.get_changes(service="payments")
    assert len(pay_changes) == 2
    for c in pay_changes:
        assert c["service"] == "payments"

    # 3. Filter by since window (last 30 seconds)
    recent_changes = obs.get_changes(since=30)
    assert len(recent_changes) == 2
    assert "CHG-1" not in [c["id"] for c in recent_changes]


def test_log_reader_filters_and_evidence_ids(obs_env):
    """Test get_logs filters by service, level, trace_id, since, and tags LOG-x."""
    obs, tmp_path = obs_env
    logs_dir = tmp_path / "logs"

    now = time.time()
    ts_now = format_iso(now)
    ts_past = format_iso(now - 120)

    # Write logs to payments.log
    pay_log = logs_dir / "payments.log"
    pay_lines = [
        {
            "ts": ts_past,
            "level": "INFO",
            "service": "payments",
            "trace_id": "trace-old",
            "msg": "Payment processed",
            "latency_ms": 15.2,
            "status": 200,
        },
        {
            "ts": ts_now,
            "level": "INFO",
            "service": "payments",
            "trace_id": "trace-101",
            "msg": "Charge request received",
            "latency_ms": 22.0,
            "status": 200,
        },
        {
            "ts": ts_now,
            "level": "ERROR",
            "service": "payments",
            "trace_id": "trace-102",
            "msg": "Payment gateway timeout",
            "latency_ms": 2500.0,
            "status": 500,
        },
    ]
    with open(pay_log, "w", encoding="utf-8") as f:
        for entry in pay_lines:
            f.write(json.dumps(entry) + "\n")

    # Write logs to orders.log
    ord_log = logs_dir / "orders.log"
    ord_lines = [
        {
            "ts": ts_now,
            "level": "INFO",
            "service": "orders",
            "trace_id": "trace-102",
            "msg": "Downstream call to payments",
            "latency_ms": 2505.0,
            "status": 500,
        }
    ]
    with open(ord_log, "w", encoding="utf-8") as f:
        for entry in ord_lines:
            f.write(json.dumps(entry) + "\n")

    # 1. Query all logs
    all_logs = obs.get_logs()
    assert len(all_logs) == 4
    for l in all_logs:
        assert l["id"].startswith("LOG-")
        assert "latency_ms" in l
        assert "status" in l

    # 2. Filter by service
    pay_logs = obs.get_logs(service="payments")
    assert len(pay_logs) == 3
    assert all(l["service"] == "payments" for l in pay_logs)

    # 3. Filter by level ERROR
    err_logs = obs.get_logs(level="ERROR")
    assert len(err_logs) == 1
    assert err_logs[0]["level"] == "ERROR"
    assert err_logs[0]["service"] == "payments"
    assert err_logs[0]["trace_id"] == "trace-102"

    # 4. Filter by trace_id (crosses payments and orders)
    trace_logs = obs.get_logs(trace_id="trace-102")
    assert len(trace_logs) == 2
    services_in_trace = {l["service"] for l in trace_logs}
    assert services_in_trace == {"payments", "orders"}

    # 5. Filter by since window (last 60 seconds ignores old log)
    recent_logs = obs.get_logs(since=60)
    assert len(recent_logs) == 3
    assert all(l["trace_id"] != "trace-old" for l in recent_logs)


def test_metric_storage_and_queries(obs_env):
    """Test get_metrics retrieves time series, summary, and MET-x citation IDs."""
    obs, tmp_path = obs_env

    # Insert test samples directly into metrics table
    now = time.time()
    samples = [
        (format_iso(now - 20), now - 20, "gateway", "http_requests_total", '{"endpoint":"/order"}', 10.0),
        (format_iso(now - 15), now - 15, "gateway", "http_requests_total", '{"endpoint":"/order"}', 25.0),
        (format_iso(now - 10), now - 10, "gateway", "http_requests_total", '{"endpoint":"/order"}', 50.0),
        (format_iso(now - 5), now - 5, "gateway", "http_errors_total", '{"endpoint":"/order"}', 2.0),
    ]

    with obs.get_connection() as conn:
        conn.executemany(
            "INSERT INTO metrics (ts, timestamp_unix, service, metric, labels, value) VALUES (?, ?, ?, ?, ?, ?)",
            samples,
        )
        conn.commit()

    # Query gateway http_requests_total
    res = obs.get_metrics(service="gateway", metric="http_requests_total", window=60)
    assert res["service"] == "gateway"
    assert res["metric"] == "http_requests_total"
    assert len(res["samples"]) == 3
    for s in res["samples"]:
        assert s["id"].startswith("MET-")
        assert s["service"] == "gateway"

    summary = res["summary"]
    assert summary["count"] == 3
    assert summary["min"] == 10.0
    assert summary["max"] == 50.0
    assert summary["latest"] == 50.0
    assert summary["avg"] == pytest.approx(28.333, rel=1e-2)
    assert summary["evidence_id"].startswith("MET-")


def test_derived_metrics_error_rate_and_p95(obs_env):
    """Test error_rate and p95_latency calculations and SLO breach detection."""
    obs, tmp_path = obs_env
    logs_dir = tmp_path / "logs"
    now = time.time()

    # Scenario: payments has 10 requests, 4 of which are 500 errors and slow (3000ms)
    pay_log = logs_dir / "payments.log"
    lines = []
    # 6 fast successful requests
    for i in range(6):
        lines.append({
            "ts": format_iso(now - i),
            "level": "INFO",
            "service": "payments",
            "trace_id": f"trace-{i}",
            "msg": "OK",
            "latency_ms": 25.0,
            "status": 200,
        })
    # 4 slow error requests
    for i in range(6, 10):
        lines.append({
            "ts": format_iso(now - i),
            "level": "ERROR",
            "service": "payments",
            "trace_id": f"trace-{i}",
            "msg": "Timeout error",
            "latency_ms": 3200.0,
            "status": 500,
        })

    with open(pay_log, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    # 1. Error rate test
    err_res = obs.error_rate("payments", window=30)
    assert isinstance(err_res, DerivedMetricResult)
    assert err_res["service"] == "payments"
    assert err_res["total_requests"] == 10
    assert err_res["error_count"] == 4
    assert float(err_res) == 0.4  # 40% error rate
    assert err_res["breached"] is True  # > 5% SLO threshold
    assert len(err_res["evidence_ids"]) > 0
    assert all(eid.startswith("LOG-") for eid in err_res["evidence_ids"])

    # 2. p95 latency test
    p95_res = obs.p95_latency("payments", window=30)
    assert isinstance(p95_res, DerivedMetricResult)
    assert p95_res["service"] == "payments"
    assert p95_res["sample_count"] == 10
    assert p95_res["p95_ms"] >= 3000.0
    assert float(p95_res) >= 3000.0
    assert p95_res["breached"] is True  # > 2000ms SLO limit
    assert len(p95_res["evidence_ids"]) > 0


def test_evidence_lookup(obs_env):
    """Test get_evidence retrieves exact records for LOG-x, MET-x, and CHG-x."""
    obs, tmp_path = obs_env
    logs_dir = tmp_path / "logs"
    changes_file = tmp_path / "changes.jsonl"

    # Write a log line
    now = time.time()
    gw_log = logs_dir / "gateway.log"
    gw_log.write_text(
        json.dumps({
            "ts": format_iso(now),
            "level": "WARNING",
            "service": "gateway",
            "trace_id": "test-trace",
            "msg": "Gateway latency warning",
            "latency_ms": 850.0,
            "status": 200,
        })
        + "\n",
        encoding="utf-8",
    )

    # Sync logs and retrieve its ID
    logs = obs.get_logs(service="gateway")
    assert len(logs) == 1
    log_id = logs[0]["id"]  # e.g. LOG-1

    # Insert a metric sample
    with obs.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO metrics (ts, timestamp_unix, service, metric, labels, value) VALUES (?, ?, ?, ?, ?, ?)",
            (format_iso(now), now, "gateway", "up", "{}", 1.0),
        )
        met_id = f"MET-{cur.lastrowid}"
        conn.commit()

    # Write a change record
    changes_file.write_text(
        json.dumps({
            "id": "CHG-42",
            "ts": format_iso(now),
            "action": "bad_deploy",
            "service": "payments",
            "author": "pipeline",
            "details": {"version": "2.0.0"},
        })
        + "\n",
        encoding="utf-8",
    )

    # Test evidence lookup for LOG-x
    log_item = obs.get_evidence(log_id)
    assert log_item is not None
    assert log_item["id"] == log_id
    assert log_item["type"] == "log"
    assert log_item["service"] == "gateway"
    assert log_item["msg"] == "Gateway latency warning"

    # Test evidence lookup for MET-x
    met_item = obs.get_evidence(met_id)
    assert met_item is not None
    assert met_item["id"] == met_id
    assert met_item["type"] == "metric"
    assert met_item["metric"] == "up"
    assert met_item["value"] == 1.0

    # Test evidence lookup for CHG-x
    chg_item = obs.get_evidence("CHG-42")
    assert chg_item is not None
    assert chg_item["id"] == "CHG-42"
    assert chg_item["type"] == "change"
    assert chg_item["action"] == "bad_deploy"

    # Nonexistent evidence ID
    assert obs.get_evidence("LOG-999999") is None
    assert obs.get_evidence("MET-999999") is None
    assert obs.get_evidence("CHG-999999") is None
    assert obs.get_evidence("INVALID-ID") is None


def test_background_collector_lifecycle(obs_env):
    """Test start_background_collector and stop_background_collector."""
    obs, _ = obs_env
    assert not obs.is_collector_running

    thread = obs.start_background_collector(interval_seconds=0.2)
    assert thread.is_alive()
    assert obs.is_collector_running

    time.sleep(0.5)
    obs.stop_background_collector()
    assert not obs.is_collector_running
    assert not thread.is_alive()
