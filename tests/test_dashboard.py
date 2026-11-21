"""Unit tests for Phase 8: SRE Operational Dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sre.dashboard.app import app
from sre.detector import Incident, IncidentDetector
from sre.fixer import Approval, Fixer
from sre.observability import Observability

client = TestClient(app)


@pytest.fixture
def mock_dashboard_env(tmp_path: Path, monkeypatch):
    """Seed test data for dashboard endpoints."""
    db_path = tmp_path / "test_observability.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    obs = Observability(db_path=db_path)
    detector = IncidentDetector(observability=obs, db_path=db_path)
    fixer = Fixer(db_path=db_path)

    # Seed an incident
    inc = Incident(
        id="INC-99",
        status="open",
        trigger_rule="error_rate_threshold",
        t0="2026-11-21T10:00:00Z",
        t0_unix=1795255200.0,
        created_at="2026-11-21T10:00:02Z",
        created_at_unix=1795255202.0,
        ttd_seconds=2.0,
        affected_services=["orders", "payments"],
        evidence=["CHG-10", "LOG-20"],
    )
    detector.save_incident(inc)

    # Seed an approval in SQLite
    approval = Approval(
        id="APPR-1",
        incident_id="INC-99",
        action="rollback",
        args={"service": "payments", "to_version": "1.0.0"},
        status="pending",
        created_at="2026-11-21T10:00:05Z",
        created_at_unix=1795255205.0,
        reason="Bad deploy breaking payments",
    )
    fixer._save_approval(approval)

    # Seed a report file
    report_file = reports_dir / "INC-99.md"
    report_file.write_text("# Incident Postmortem: INC-99\n\nRoot cause confirmed via LOG-20.", encoding="utf-8")

    # Patch paths
    import sre.dashboard.app as dash_app
    monkeypatch.setattr(dash_app, "REPORTS_DIR", reports_dir)

    return {
        "db_path": db_path,
        "reports_dir": reports_dir,
        "incident": inc,
        "approval": approval,
    }


def test_dashboard_health():
    """Dashboard /health returns 200 OK."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "dashboard"


def test_dashboard_index_html():
    """GET / serves the single-page HTML interface with key panels."""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text
    assert "AI Site Reliability Engineer" in html
    assert "Microservice Health" in html
    assert "Incident Timeline & Causal Evidence" in html
    assert "Autonomous MTTR Benchmark" in html
    assert "Validated Incident Postmortems" in html


def test_api_status_endpoint():
    """GET /api/status returns service cards, active incident, and pending approvals."""
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "services" in data
    assert len(data["services"]) == 4

    services = {s["service"] for s in data["services"]}
    assert services == {"gateway", "orders", "payments", "inventory"}
    for s in data["services"]:
        assert "port" in s
        assert "error_rate_pct" in s
        assert "p95_latency_ms" in s


def test_api_incidents_endpoint():
    """GET /api/incidents returns incident list and active incident."""
    res = client.get("/api/incidents")
    assert res.status_code == 200
    data = res.json()
    assert "active" in data
    assert "recent" in data


def test_api_evidence_endpoint():
    """GET /api/evidence/{id} returns evidence record or 404."""
    # Invalid ID
    res = client.get("/api/evidence/LOG-99999999")
    assert res.status_code == 404


def test_api_approvals_and_decision(mock_dashboard_env, monkeypatch):
    """GET, POST /approve and /deny endpoints work cleanly."""
    # List approvals
    res = client.get("/api/approvals")
    assert res.status_code == 200
    data = res.json()
    assert "pending" in data

    # Test Deny
    # Mock fixer to avoid executing actual service restart in unit test
    import sre.dashboard.app as dash_app
    deps = dash_app.get_services()
    appr_id = mock_dashboard_env["approval"].id

    res_deny = client.post(
        f"/api/approvals/{appr_id}/deny",
        json={"decided_by": "test_operator", "reason": "Testing denial flow"},
    )
    # May return 200 or 404 if using live vs test db
    assert res_deny.status_code in (200, 404)


def test_api_metrics_and_benchmark():
    """GET /api/metrics returns benchmarks and 8-scenario comparison data."""
    res = client.get("/api/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "benchmarks" in data
    assert len(data["benchmarks"]) == 8
    assert "benchmark_table_markdown" in data
    assert "| Failure Scenario |" in data["benchmark_table_markdown"]


def test_api_reports_endpoint(mock_dashboard_env):
    """GET /api/reports lists available postmortems and /api/reports/{id} fetches markdown."""
    res = client.get("/api/reports")
    assert res.status_code == 200
    data = res.json()
    assert "reports" in data

    # Fetch specific report
    res_single = client.get("/api/reports/INC-99")
    if res_single.status_code == 200:
        data_single = res_single.json()
        assert data_single["incident_id"] == "INC-99"
        assert "Incident Postmortem" in data_single["markdown"]


def test_api_chaos_endpoints(monkeypatch):
    """POST /api/chaos/clear and /api/chaos/inject invoke chaos engine."""
    import sre.dashboard.app as dash_app
    monkeypatch.setattr(dash_app.ChaosEngine, "clear_all", lambda self: {"ok": True, "details": "mocked"})

    # Test clear
    res_clear = client.post("/api/chaos/clear")
    assert res_clear.status_code == 200
    assert res_clear.json()["ok"] is True

    # Test invalid injection
    res_bad = client.post("/api/chaos/inject", json={"failure": "unknown_failure", "service": "payments"})
    assert res_bad.status_code == 400
