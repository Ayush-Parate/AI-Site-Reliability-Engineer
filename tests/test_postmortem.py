"""Unit tests for Phase 7: Timeline, Postmortem, and SRE Metrics."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sre.detector import Breach, Incident, IncidentDetector
from sre.observability import Observability, format_iso
from sre.postmortem import (
    BenchmarkSuite,
    PostmortemGenerator,
    PostmortemValidator,
    TimelineBuilder,
    TimelineEntry,
)


@pytest.fixture
def temp_env(tmp_path: Path):
    """Set up temporary database, changes log, and reports directory."""
    db_path = tmp_path / "test_observability.db"
    changes_path = tmp_path / "changes.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Seed changes log
    chg_lines = [
        {"id": "CHG-1", "ts": "2026-11-21T10:00:00Z", "action": "chaos_latency", "service": "payments", "author": "chaos", "details": {"latency_ms": 2000}},
        {"id": "CHG-2", "ts": "2026-11-21T10:00:30Z", "action": "restart", "service": "payments", "author": "supervisor", "details": {"pid": 12345}},
    ]
    with open(changes_path, "w", encoding="utf-8") as f:
        for c in chg_lines:
            f.write(json.dumps(c) + "\n")

    obs = Observability(db_path=db_path)
    # Seed log index in SQLite for evidence lookup
    with obs.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO log_index (service, file_line, ts, timestamp_unix, level, trace_id, msg, latency_ms, status, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("payments", 1, "2026-11-21T10:00:05Z", 1795255205.0, "ERROR", "tr_1", "Connection timeout to bank upstream", 2005.2, 504, "{}"),
        )
        cursor.execute(
            """INSERT INTO metrics (ts, timestamp_unix, service, metric, labels, value)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("2026-11-21T10:00:05Z", 1795255205.0, "payments", "http_requests_total", "{}", 45.0),
        )
        conn.commit()

    detector = IncidentDetector(observability=obs, db_path=db_path)

    # Create and persist a sample incident
    inc = Incident(
        id="INC-101",
        status="resolved",
        trigger_rule="latency_p95_breach",
        t0="2026-11-21T10:00:02Z",
        t0_unix=1795255202.0,
        created_at="2026-11-21T10:00:06Z",
        created_at_unix=1795255206.0,
        ttd_seconds=4.0,
        affected_services=["payments", "orders"],
        evidence=["CHG-1", "LOG-1", "MET-1"],
        breaches=[
            {
                "rule": "latency_p95_breach",
                "service": "payments",
                "message": "p95 latency is 2005ms (> 2000ms SLO)",
                "t0": "2026-11-21T10:00:02Z",
                "t0_unix": 1795255202.0,
                "evidence_ids": ["LOG-1", "MET-1"],
            },
            {
                "rule": "error_rate_breach",
                "service": "orders",
                "message": "Cascade 504 error rate is 25% (> 5% SLO)",
                "t0": "2026-11-21T10:00:10Z",
                "t0_unix": 1795255210.0,
                "evidence_ids": ["LOG-1"],
            },
        ],
        healthy_since=1795255240.0,
        resolved_at="2026-11-21T10:00:45Z",
        resolved_at_unix=1795255245.0,
        metadata={
            "diagnosis": {
                "root_cause_service": "payments",
                "root_cause": "High latency on payment gateway caused upstream timeouts",
                "t1": "2026-11-21T10:00:18Z",
                "t1_unix": 1795255218.0,
                "evidence": ["CHG-1", "LOG-1"],
                "causal_chain": ["payments latency", "orders 504", "gateway SLO breach"],
            },
            "fix": {
                "action": "restart_service",
                "service": "payments",
                "applied_at": "2026-11-21T10:00:30Z",
                "applied_at_unix": 1795255230.0,
                "evidence_ids": ["CHG-2"],
            },
        },
    )
    detector.save_incident(inc)

    gen = PostmortemGenerator(
        observability=obs,
        detector=detector,
        reports_dir=reports_dir,
    )
    gen.timeline_builder.changes_log = changes_path

    return {
        "obs": obs,
        "detector": detector,
        "generator": gen,
        "incident": inc,
        "reports_dir": reports_dir,
        "changes_path": changes_path,
    }


def test_timeline_construction(temp_env):
    """Timeline is built deterministically with chronologically sorted entries and evidence IDs."""
    gen = temp_env["generator"]
    inc = temp_env["incident"]

    timeline = gen.timeline_builder.build_timeline(inc)
    assert len(timeline) >= 4

    # Verify chronological sorting
    for i in range(len(timeline) - 1):
        assert timeline[i].timestamp_unix <= timeline[i + 1].timestamp_unix

    event_types = [t.event for t in timeline]
    assert "detected" in event_types
    assert "breach" in event_types
    assert "diagnosed" in event_types
    assert "mitigated" in event_types
    assert "resolved" in event_types

    # Ensure evidence IDs are linked
    eids = [e for t in timeline for e in t.evidence_ids]
    assert "CHG-1" in eids
    assert "LOG-1" in eids


def test_evidence_expansion(temp_env):
    """Citations (LOG-x, MET-x, CHG-x) expand into full database and audit records."""
    gen = temp_env["generator"]
    expanded = gen.expand_evidence_index(["LOG-1", "MET-1", "CHG-1"])

    assert len(expanded) == 3
    types = {e.get("type") for e in expanded}
    assert "log" in types
    assert "metric" in types
    assert "change" in types

    log_rec = next(e for e in expanded if e["id"] == "LOG-1")
    assert log_rec["service"] == "payments"
    assert log_rec["status"] == 504


def test_postmortem_validator_rejects_uncited_claims(temp_env):
    """Validator rejects reports lacking evidence IDs in root cause or overall."""
    val = PostmortemValidator(observability=temp_env["obs"])

    # 1. Invalid: no citations at all
    bad_md_1 = """# Postmortem
## 1. Summary
Everything was broken.
## 3. Root Cause Analysis
Someone broke the server.
"""
    res_1 = val.validate(bad_md_1)
    assert res_1["valid"] is False
    assert any("No evidence IDs found" in e for e in res_1["errors"])

    # 2. Invalid: citation in summary but completely missing in Root Cause
    bad_md_2 = """# Postmortem
## 1. Summary
Found LOG-1 in logs.
## 3. Root Cause Analysis
The server just ran out of memory mysteriously without evidence.
"""
    res_2 = val.validate(bad_md_2)
    assert res_2["valid"] is False
    assert any("Root Cause Analysis section contains no evidence IDs" in e for e in res_2["errors"])

    # 3. Valid: cited properly
    good_md = """# Postmortem
## 1. Summary
Detected breach with LOG-1.
## 3. Root Cause Analysis
Root cause on payments verified via evidence LOG-1 and MET-1.
"""
    res_3 = val.validate(good_md)
    assert res_3["valid"] is True
    assert "LOG-1" in res_3["citations"]
    assert "MET-1" in res_3["citations"]


def test_deterministic_report_generation(temp_env):
    """Postmortem is generated and saved as markdown with all 8 required sections."""
    gen = temp_env["generator"]
    inc_id = temp_env["incident"].id

    md, path = gen.generate_postmortem(inc_id, use_llm=False)
    assert path.is_file()
    assert path.name == f"{inc_id}.md"

    # Verify all 8 standard sections exist
    required_sections = [
        "## 1. Summary & Severity",
        "## 2. Impact",
        "## 3. Root Cause Analysis",
        "## 4. Incident Timeline",
        "## 5. What Worked & What Didn't",
        "## 6. Fix Applied & Verification",
        "## 7. Prevention & Follow-up Actions",
        "## 8. Cited Evidence Index",
    ]
    for sec in required_sections:
        assert sec in md, f"Missing section: {sec}"

    # Verify citations exist in the report
    assert "LOG-1" in md
    assert "CHG-1" in md
    assert "payments" in md

    # Verify report passes validator
    val_res = gen.validator.validate(md)
    assert val_res["valid"] is True


def test_metrics_calculation_and_baselines(temp_env):
    """Metrics correctly compute TTD, TTDiag, TTR, and comparison vs manual baseline."""
    gen = temp_env["generator"]
    inc = temp_env["incident"]

    m = gen.calculate_metrics(inc)
    assert m["incident_id"] == "INC-101"
    assert m["scenario"] == "latency"
    assert m["ttd_seconds"] == 4.0
    assert m["ttdiag_seconds"] == 16.0  # 1795255218 - 1795255202
    assert m["ttr_seconds"] == 43.0     # 1795255245 - 1795255202
    assert m["manual_baseline_seconds"] == 360.0
    assert m["speedup_factor"] > 1.0


def test_benchmark_suite():
    """Benchmark suite covers all 8 failure scenarios with realistic speedup numbers."""
    rows = BenchmarkSuite.get_scenario_metrics()
    assert len(rows) == 8

    scenarios = [r["scenario"] for r in rows]
    expected_scenarios = [
        "crash",
        "latency",
        "error_burst",
        "memory_leak",
        "cpu_hog",
        "bad_deploy",
        "dependency_down",
        "db_lock",
    ]
    for s in expected_scenarios:
        assert s in scenarios

    for r in rows:
        assert r["ai_ttr_s"] < r["manual_ttr_s"]
        assert r["speedup"] > 1.0
        assert len(r["key_evidence"]) > 0

    table_md = BenchmarkSuite.render_benchmark_table()
    assert "| Failure Scenario |" in table_md
    assert "Process Crash" in table_md
    assert "SQLite DB Write Lock" in table_md


def test_llm_postmortem_generation_with_fallback(temp_env, monkeypatch):
    """Postmortem falls back to deterministic generator if LLM fails or is unavailable."""
    gen = temp_env["generator"]
    inc_id = temp_env["incident"].id

    # 1. Simulate LLM failure
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = Exception("LLM quota exceeded")
    monkeypatch.setattr(gen, "llm", mock_llm)

    # Should not raise exception; falls back cleanly
    md, path = gen.generate_postmortem(inc_id, use_llm=True)
    assert path.is_file()
    assert "## 3. Root Cause Analysis" in md

    # 2. Simulate valid LLM response
    valid_llm_text = """# Incident Postmortem: INC-101

**Incident ID:** INC-101
**Status:** RESOLVED
**Severity:** SEV-2

## 1. Summary & Severity
Failure detected via LOG-1.

## 2. Impact
Affected payments service.

## 3. Root Cause Analysis
Root cause was latency spike on payments confirmed by LOG-1 and MET-1.

## 4. Incident Timeline
| Time | Event | Service | Description | Evidence |
| 10:00:00 | Change | payments | Injected latency | LOG-1 |

## 5. What Worked & What Didn't
What worked: fast TTD.

## 6. Fix Applied & Verification
Restarted via CHG-2.

## 7. Prevention & Follow-up Actions
Tune timeout.

## 8. Cited Evidence Index
LOG-1: error log.
"""
    mock_llm.chat.side_effect = None
    mock_llm.chat.return_value = {"text": valid_llm_text, "provider": "mock"}

    md_llm, path_llm = gen.generate_postmortem(inc_id, use_llm=True)
    assert md_llm == valid_llm_text.strip()
    assert path_llm.is_file()
