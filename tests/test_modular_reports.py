"""Unit tests for Modular, Categorized Incident Reporting System."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sre.dashboard.app import app
from sre.detector import Incident, IncidentDetector
from sre.observability import Observability
from sre.reports import (
    REPORT_SECTIONS,
    EvidenceObservabilitySection,
    ExecutiveSummarySection,
    IncidentImpactSection,
    IncidentTimelineSection,
    PreventionRecommendationsSection,
    RecoveryVerificationSection,
    RemediationActionsSection,
    ReportMetadata,
    ReportService,
    ReportStorage,
    RootCauseSection,
)

client = TestClient(app)


@pytest.fixture
def mock_reports_env(tmp_path: Path, monkeypatch):
    """Set up temporary database, reports directory, and sample incidents."""
    db_path = tmp_path / "test_observability.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    obs = Observability(db_path=db_path)
    detector = IncidentDetector(observability=obs, db_path=db_path)

    # Seed Incident 1: Resolved Latency Spike
    inc1 = Incident(
        id="INC-201",
        status="resolved",
        trigger_rule="latency_p95_breach",
        t0="2026-11-21T10:00:00Z",
        t0_unix=1795255200.0,
        created_at="2026-11-21T10:00:04Z",
        created_at_unix=1795255204.0,
        resolved_at="2026-11-21T10:00:45Z",
        resolved_at_unix=1795255245.0,
        ttd_seconds=4.0,
        affected_services=["payments", "orders"],
        evidence=["CHG-10", "LOG-20", "MET-30"],
        breaches=[
            {"service": "payments", "message": "latency breach p95=2800ms", "evidence_ids": ["MET-30"]},
            {"service": "orders", "message": "error rate breach 6.2%", "evidence_ids": ["LOG-20"]},
        ],
        metadata={
            "diagnosis": {
                "root_cause_service": "payments",
                "root_cause": "High latency on payment gateway caused upstream timeouts",
                "causal_chain": ["payments latency", "orders 504 timeout", "gateway SLO breach"],
                "confidence": 0.95,
                "evidence": ["CHG-10", "LOG-20"],
            },
            "fix": {
                "action": "restart_service",
                "service": "payments",
                "tier": "auto_fix",
                "evidence_ids": ["CHG-10"],
            },
        },
    )
    detector.save_incident(inc1)

    # Seed Incident 2: Open Crash Incident
    inc2 = Incident(
        id="INC-202",
        status="open",
        trigger_rule="health_check_failure",
        t0="2026-11-21T11:00:00Z",
        t0_unix=1795258800.0,
        created_at="2026-11-21T11:00:03Z",
        created_at_unix=1795258803.0,
        ttd_seconds=3.0,
        affected_services=["orders"],
        evidence=["LOG-50", "CHG-60"],
        breaches=[
            {"service": "orders", "message": "health check failed", "evidence_ids": ["LOG-50"]},
        ],
    )
    detector.save_incident(inc2)

    # Patch dashboard app's REPORTS_DIR
    import sre.dashboard.app as dash_app
    monkeypatch.setattr(dash_app, "REPORTS_DIR", reports_dir)

    storage = ReportStorage(reports_dir=reports_dir)
    service = ReportService(observability=obs, detector=detector, storage=storage)

    return {
        "db_path": db_path,
        "reports_dir": reports_dir,
        "obs": obs,
        "detector": detector,
        "storage": storage,
        "service": service,
        "incident_resolved": inc1,
        "incident_open": inc2,
    }


def test_report_models_serialization():
    """Verify dataclasses serialize cleanly to dict and retain all 8 categories."""
    meta = ReportMetadata(
        incident_id="INC-1",
        title="Test Incident",
        status="resolved",
        severity="SEV-1",
        trigger_rule="latency_breach",
        created_at="2026-11-21T10:00:00Z",
        t0="2026-11-21T10:00:00Z",
        detected_at="2026-11-21T10:00:02Z",
        affected_services=["payments"],
        evidence_count=2,
    )
    meta_dict = meta.to_dict()
    assert meta_dict["incident_id"] == "INC-1"
    assert meta_dict["severity"] == "SEV-1"
    assert meta_dict["sections_count"] == 8

    exec_sec = ExecutiveSummarySection(
        incident_id="INC-1",
        title="Test Incident",
        severity="SEV-1",
        trigger_rule="latency_breach",
        status="resolved",
        t0="2026-11-21T10:00:00Z",
        detected_at="2026-11-21T10:00:02Z",
        affected_services=["payments"],
        summary_text="Automated summary",
    )
    assert exec_sec.to_dict()["summary_text"] == "Automated summary"

    assert len(REPORT_SECTIONS) == 8
    expected_ids = [
        "executive_summary",
        "impact",
        "root_cause",
        "evidence",
        "timeline",
        "remediation",
        "verification",
        "prevention",
    ]
    assert [s["id"] for s in REPORT_SECTIONS] == expected_ids


def test_report_service_generate_and_save(mock_reports_env):
    """ReportService generates all 8 sections and stores them as modular JSON files."""
    service: ReportService = mock_reports_env["service"]
    inc_id = mock_reports_env["incident_resolved"].id

    res, inc_dir = service.generate_and_save_report(inc_id, use_llm=False)
    assert inc_dir.is_dir()
    assert inc_dir.name == inc_id

    # Verify all 8 section files exist in reports/<INC-ID>/
    for sec in REPORT_SECTIONS:
        sec_file = inc_dir / f"{sec['id']}.json"
        assert sec_file.is_file(), f"Missing section file: {sec_file}"
        sec_content = json.loads(sec_file.read_text(encoding="utf-8"))
        assert isinstance(sec_content, dict)

    # Verify metadata.json and postmortem.md
    meta_file = inc_dir / "metadata.json"
    assert meta_file.is_file()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["incident_id"] == inc_id
    assert meta["status"] == "resolved"
    assert meta["evidence_count"] >= 3

    # Verify backward-compatible markdown file in reports/
    legacy_md = mock_reports_env["reports_dir"] / f"{inc_id}.md"
    assert legacy_md.is_file()
    assert f"# Incident Postmortem: {inc_id}" in legacy_md.read_text(encoding="utf-8")


def test_report_storage_migration_of_legacy_markdown(mock_reports_env):
    """Storage automatically migrates a standalone legacy markdown file into 8 modular JSON files."""
    reports_dir: Path = mock_reports_env["reports_dir"]
    storage: ReportStorage = mock_reports_env["storage"]

    # Write a standalone legacy markdown report
    legacy_file = reports_dir / "INC-999.md"
    legacy_file.write_text(
        """# Incident Postmortem: INC-999

**Incident ID:** INC-999
**Status:** RESOLVED
**Severity:** SEV-1 (Critical)
**Trigger Rule:** error_rate_threshold
**T0 (Anomaly):** 2026-11-21T12:00:00Z
**Affected Services:** payments, orders

## 1. Summary & Severity
Failure detected via LOG-901 and CHG-902.

## 3. Root Cause Analysis
Root cause was bad deploy confirmed by LOG-901 and MET-903.
""",
        encoding="utf-8",
    )

    # Retrieve metadata triggers on-the-fly migration
    meta = storage.get_report_metadata("INC-999")
    assert meta is not None
    assert meta["incident_id"] == "INC-999"
    assert meta["status"] == "resolved"
    assert meta["severity"] == "SEV-1 (Critical)"

    # Verify migrated directory and section files
    inc_dir = reports_dir / "INC-999"
    assert inc_dir.is_dir()
    assert (inc_dir / "metadata.json").is_file()
    assert (inc_dir / "executive_summary.json").is_file()
    assert (inc_dir / "impact.json").is_file()
    assert (inc_dir / "root_cause.json").is_file()
    assert (inc_dir / "evidence.json").is_file()
    assert (inc_dir / "timeline.json").is_file()
    assert (inc_dir / "remediation.json").is_file()
    assert (inc_dir / "verification.json").is_file()
    assert (inc_dir / "prevention.json").is_file()


def test_api_list_reports_pagination_and_filters(mock_reports_env):
    """GET /api/reports returns lightweight metadata and supports pagination and filters."""
    service: ReportService = mock_reports_env["service"]
    inc1_id = mock_reports_env["incident_resolved"].id
    inc2_id = mock_reports_env["incident_open"].id

    service.generate_and_save_report(inc1_id, use_llm=False)
    service.generate_and_save_report(inc2_id, use_llm=False)

    # 1. Fetch all reports
    res = client.get("/api/reports")
    assert res.status_code == 200
    data = res.json()
    assert "reports" in data  # backward-compatible key
    assert "items" in data
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # Verify items contain only lightweight metadata (not full markdown or all section bodies)
    for item in data["items"]:
        assert "incident_id" in item
        assert "title" in item
        assert "severity" in item
        assert "status" in item
        assert "markdown" not in item  # ensures lightweight retrieval

    # 2. Filter by status: resolved
    res_res = client.get("/api/reports?status=resolved")
    assert res_res.status_code == 200
    data_res = res_res.json()
    assert data_res["total"] == 1
    assert data_res["items"][0]["incident_id"] == inc1_id

    # 3. Filter by status: open
    res_open = client.get("/api/reports?status=open")
    assert res_open.status_code == 200
    data_open = res_open.json()
    assert data_open["total"] == 1
    assert data_open["items"][0]["incident_id"] == inc2_id

    # 4. Filter by severity
    res_sev = client.get("/api/reports?severity=SEV-1")
    assert res_sev.status_code == 200

    # 5. Search query
    res_search = client.get(f"/api/reports?search={inc1_id}")
    assert res_search.status_code == 200
    assert res_search.json()["total"] == 1
    assert res_search.json()["items"][0]["incident_id"] == inc1_id

    # 6. Pagination page_size=1
    res_p1 = client.get("/api/reports?page=1&page_size=1")
    assert res_p1.status_code == 200
    d_p1 = res_p1.json()
    assert d_p1["page"] == 1
    assert d_p1["page_size"] == 1
    assert d_p1["total"] == 2
    assert d_p1["total_pages"] == 2
    assert len(d_p1["items"]) == 1


def test_api_get_individual_report_and_sections(mock_reports_env):
    """GET /api/reports/{id}, /sections, and /sections/{section_id} isolate retrieved content."""
    service: ReportService = mock_reports_env["service"]
    inc_id = mock_reports_env["incident_resolved"].id
    service.generate_and_save_report(inc_id, use_llm=False)

    # 1. GET /api/reports/{id}
    res = client.get(f"/api/reports/{inc_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["incident_id"] == inc_id
    assert "metadata" in data
    assert "sections" in data
    assert len(data["sections"]) == 8
    assert "markdown" in data  # backward-compatible

    # 2. GET /api/reports/{id}/sections
    res_sec_list = client.get(f"/api/reports/{inc_id}/sections")
    assert res_sec_list.status_code == 200
    sec_index = res_sec_list.json()
    assert sec_index["incident_id"] == inc_id
    assert len(sec_index["sections"]) == 8
    sec_ids = [s["id"] for s in sec_index["sections"]]
    assert "executive_summary" in sec_ids
    assert "root_cause" in sec_ids
    assert "timeline" in sec_ids

    # 3. GET /api/reports/{id}/sections/{section_id} for Executive Summary
    res_exec = client.get(f"/api/reports/{inc_id}/sections/executive_summary")
    assert res_exec.status_code == 200
    exec_data = res_exec.json()
    assert exec_data["incident_id"] == inc_id
    assert exec_data["severity"] in ("SEV-1 (Critical)", "SEV-2 (Major)")
    assert "summary_text" in exec_data

    # 4. GET /api/reports/{id}/sections/root_cause
    res_rc = client.get(f"/api/reports/{inc_id}/sections/root_cause")
    assert res_rc.status_code == 200
    rc_data = res_rc.json()
    assert rc_data["root_cause_service"] == "payments"
    assert len(rc_data["supporting_evidence"]) > 0

    # 5. GET /api/reports/{id}/sections/timeline
    res_tl = client.get(f"/api/reports/{inc_id}/sections/timeline")
    assert res_tl.status_code == 200
    tl_data = res_tl.json()
    assert "events" in tl_data
    assert tl_data["t0"] == "2026-11-21T10:00:00Z"

    # 6. GET /api/reports/{id}/sections/evidence
    res_ev = client.get(f"/api/reports/{inc_id}/sections/evidence")
    assert res_ev.status_code == 200
    ev_data = res_ev.json()
    assert "evidence_records" in ev_data
    assert ev_data["total_evidence_count"] >= 3

    # 7. Non-existent section returns 404
    res_not_found = client.get(f"/api/reports/{inc_id}/sections/non_existent_section")
    assert res_not_found.status_code == 404

    # 8. Non-existent report returns 404
    res_rep_not_found = client.get("/api/reports/INC-DOES-NOT-EXIST")
    assert res_rep_not_found.status_code == 404


def test_markdown_export_feature(mock_reports_env):
    """Verify 'Download as Markdown (.md)' exports all 8 sections with dynamic filename."""
    service: ReportService = mock_reports_env["service"]
    storage: ReportStorage = mock_reports_env["storage"]
    inc_id = mock_reports_env["incident_resolved"].id
    service.generate_and_save_report(inc_id, use_llm=False)

    # 1. Test storage.get_export_markdown directly
    md_content, filename = storage.get_export_markdown(inc_id)

    # Filename format: INC-xxx_Postmortem_YYYY-MM-DD.md
    assert filename.startswith(f"{inc_id}_Postmortem_")
    assert filename.endswith(".md")
    assert len(filename.split("_")) == 3

    # All 8 sections must exist in order
    expected_headings = [
        f"# Incident Postmortem: {inc_id}",
        "## 1. Executive Summary",
        "## 2. Incident Impact",
        "## 3. Root Cause Analysis",
        "## 4. Evidence & Observability",
        "## 5. Incident Timeline",
        "## 6. Remediation Actions",
        "## 7. Recovery Verification",
        "## 8. Prevention & Recommendations",
    ]
    last_idx = -1
    for h in expected_headings:
        idx = md_content.find(h)
        assert idx != -1, f"Missing section heading: '{h}'"
        assert idx > last_idx, f"Heading '{h}' is out of order"
        last_idx = idx

    # Evidence IDs preserved
    assert "CHG-10" in md_content
    assert "LOG-20" in md_content
    assert "MET-30" in md_content

    # Tables and markdown elements verified
    assert "| Time (UTC) | Event | Service | Description | Evidence |" in md_content
    assert "| Evidence ID | Type | Service | Telemetry / Details |" in md_content
    assert "- **Incident ID:**" in md_content
    assert "- **Root-Cause Explanation:**" in md_content

    # 2. Test GET /api/reports/{id}/export endpoint
    res_export = client.get(f"/api/reports/{inc_id}/export")
    assert res_export.status_code == 200
    export_data = res_export.json()
    assert export_data["incident_id"] == inc_id
    assert export_data["filename"] == filename
    assert export_data["markdown"] == md_content

    # 3. Test GET /api/reports/{id}/download attachment endpoint
    res_dl = client.get(f"/api/reports/{inc_id}/download")
    assert res_dl.status_code == 200
    assert "text/markdown" in res_dl.headers["content-type"]
    assert f'filename="{filename}"' in res_dl.headers["content-disposition"]
    assert res_dl.text == md_content

    # 4. Verify UI button in Dashboard HTML
    res_ui = client.get("/")
    assert res_ui.status_code == 200
    assert "Download .md" in res_ui.text
    assert "downloadMarkdownReport" in res_ui.text
    assert "mod-rep-download-btn" in res_ui.text


def test_individual_category_downloads(mock_reports_env):
    """Verify each of the 8 categories can be downloaded independently as a separate .md file."""
    service: ReportService = mock_reports_env["service"]
    storage: ReportStorage = mock_reports_env["storage"]
    inc_id = mock_reports_env["incident_resolved"].id
    service.generate_and_save_report(inc_id, use_llm=False)

    for sec in REPORT_SECTIONS:
        sec_id = sec["id"]
        # 1. Test storage.get_section_markdown
        sec_md, sec_fn = storage.get_section_markdown(inc_id, sec_id)
        assert sec_fn.startswith(f"{inc_id}_")
        assert sec_fn.endswith(".md")
        assert f"# Incident {inc_id}:" in sec_md
        assert len(sec_md) > 50

        # 2. Test /api/reports/{id}/sections/{section_id}/export
        res_exp = client.get(f"/api/reports/{inc_id}/sections/{sec_id}/export")
        assert res_exp.status_code == 200
        d_exp = res_exp.json()
        assert d_exp["incident_id"] == inc_id
        assert d_exp["section_id"] == sec_id
        assert d_exp["filename"] == sec_fn
        assert d_exp["markdown"] == sec_md

        # 3. Test /api/reports/{id}/sections/{section_id}/download attachment
        res_dl = client.get(f"/api/reports/{inc_id}/sections/{sec_id}/download")
        assert res_dl.status_code == 200
        assert "text/markdown" in res_dl.headers["content-type"]
        assert f'filename="{sec_fn}"' in res_dl.headers["content-disposition"]
        assert res_dl.text == sec_md


def test_download_all_zip_archive(mock_reports_env):
    """Verify 'Download All (ZIP)' bundles all 8 individual Markdown files in one ZIP archive."""
    import io
    import zipfile

    service: ReportService = mock_reports_env["service"]
    storage: ReportStorage = mock_reports_env["storage"]
    inc_id = mock_reports_env["incident_resolved"].id
    service.generate_and_save_report(inc_id, use_llm=False)

    # 1. Test storage.get_zip_archive directly
    zip_bytes, zip_fn = storage.get_zip_archive(inc_id)
    assert zip_fn.startswith(f"{inc_id}_Postmortem_All_Sections_")
    assert zip_fn.endswith(".zip")
    assert len(zip_bytes) > 100

    # Inspect zip contents
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        namelist = zf.namelist()
        # Must contain all 8 sections + combined postmortem
        assert len(namelist) >= 9
        for sec in REPORT_SECTIONS:
            sec_id_clean = sec["title"].replace(" & ", "_").replace(" ", "_").replace("/", "_")
            matches = [n for n in namelist if sec_id_clean in n]
            assert len(matches) == 1, f"Missing section '{sec_id_clean}' in zip: {namelist}"

    # 2. Test GET /api/reports/{id}/zip endpoint
    res_zip = client.get(f"/api/reports/{inc_id}/zip")
    assert res_zip.status_code == 200
    assert "application/zip" in res_zip.headers["content-type"]
    assert f'filename="{zip_fn}"' in res_zip.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(res_zip.content), mode="r") as zf:
        assert len(zf.namelist()) >= 9
        # Verify content of one section inside zip
        exec_content = [zf.read(n).decode("utf-8") for n in zf.namelist() if "Executive_Summary" in n][0]
        assert f"# Incident {inc_id}: Executive Summary" in exec_content
        assert "## 1. Executive Summary" in exec_content

    # 3. Verify UI buttons and actions in HTML
    res_ui = client.get("/")
    assert res_ui.status_code == 200
    assert "Download .md" in res_ui.text
    assert "Download All (ZIP)" in res_ui.text
    assert "mod-rep-download-btn" in res_ui.text
    assert "mod-rep-zip-btn" in res_ui.text
    assert "downloadAllZip" in res_ui.text
    assert "downloadCategoryMarkdown" in res_ui.text


def test_canonical_export_no_duplicate_headers_and_exact_eight_sections(tmp_path: Path):
    """Verify full markdown export satisfies all 12 criteria:
    - Exactly 8 categories appearing once.
    - No duplicate postmortem headers.
    - Canonical resolution time and metrics.
    - No 0.0s for missing metrics.
    - SLO violation count agrees with breaches.
    """
    import re
    from sre.detector import Incident, IncidentDetector
    from sre.observability import Observability
    from sre.reports import ReportService, ReportStorage

    db_path = tmp_path / "obs.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    obs = Observability(db_path=db_path)
    detector = IncidentDetector(observability=obs, db_path=db_path)
    storage = ReportStorage(reports_dir=reports_dir)
    service = ReportService(observability=obs, detector=detector, storage=storage)

    # Seed canonical incident
    inc = Incident(
        id="INC-1",
        status="resolved",
        trigger_rule="error_rate_threshold",
        t0="2026-11-21T16:01:47.350174Z",
        t0_unix=1795276907.35,
        created_at="2026-11-21T16:01:49.156895Z",
        created_at_unix=1795276909.16,
        resolved_at="2026-11-21T19:01:53.128094Z",
        resolved_at_unix=1795287713.13,
        ttd_seconds=1.81,
        affected_services=["payments", "gateway", "orders", "inventory"],
        evidence=[],
        breaches=[
            {"service": "payments", "message": "error rate breach 8.4%", "evidence_ids": []},
            {"service": "gateway", "message": "error rate breach 5.3%", "evidence_ids": []},
        ],
        metadata={"trigger_message": "error rate breached 5%"},
    )
    detector.save_incident(inc)

    md, fn = service.get_export_markdown("INC-1")

    # 1. Filename dynamic format
    assert fn.startswith("INC-1_Postmortem_")
    assert fn.endswith(".md")

    # 2. Main title header appears exactly once
    title_matches = len(re.findall(r"^#\s*Incident Postmortem: INC-1", md, re.MULTILINE))
    assert title_matches == 1

    # 3. Exactly 8 categories, each appearing exactly once
    category_names = [
        "Executive Summary",
        "Incident Impact",
        "Root Cause Analysis",
        "Evidence & Observability",
        "Incident Timeline",
        "Remediation Actions",
        "Recovery Verification",
        "Prevention & Recommendations",
    ]
    for idx, name in enumerate(category_names, 1):
        heading = f"## {idx}. {name}"
        assert md.count(heading) == 1, f"Heading '{heading}' occurred {md.count(heading)} times, expected 1."

    # 4. No 0.0s for missing metrics
    assert not re.search(r"\b0\.0s\b", md)
    assert "Not recorded" in md  # TTDiag was missing, so Not recorded

    # 5. Canonical resolution timestamp used
    assert "2026-11-21T19:01:53.128094Z" in md

    # 6. Canonical TTD used
    assert "1.81s" in md

    # 7. SLO violations count matches breaches
    assert "SLO Violations Count:** 2" in md

    # 8. No embedded markdown postmortem header inside Section 2
    assert "### Impact Details\n# Incident Postmortem" not in md


def test_legacy_markdown_migration_sanitizes_nested_headers(tmp_path: Path):
    """Verify legacy markdown migration does not leak raw document headers into Section 2."""
    from sre.reports import ReportStorage

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    storage = ReportStorage(reports_dir=reports_dir)

    legacy_file = reports_dir / "INC-99.md"
    legacy_file.write_text(
        "# Incident Postmortem: INC-99\n\n"
        "**Incident ID:** INC-99  \n"
        "**Date:** 2026-11-21  \n"
        "**Status:** RESOLVED  \n"
        "**Severity:** SEV-2 (Major)  \n"
        "**Time to Detect (TTD):** 5.0s  \n"
        "**Time to Diagnosis (TTDiag):** 15.0s  \n"
        "**Time to Recovery (TTR):** 30.0s (10.0x faster)\n\n"
        "---\n\n"
        "## 1. Summary\n"
        "Legacy summary text.\n\n"
        "## 2. Impact\n"
        "- Affected Services: `orders`\n"
        "Downstream failure caused timeouts.\n",
        encoding="utf-8",
    )

    md, fn = storage.get_export_markdown("INC-99")
    assert md.count("# Incident Postmortem: INC-99") == 1
    assert "Downstream failure caused timeouts." in md
    assert "### Impact Details\n# Incident Postmortem" not in md

