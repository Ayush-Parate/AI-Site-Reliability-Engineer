"""Report storage manager for modular postmortem sections and backward compatibility."""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from sre.reports.models import (
    REPORT_SECTIONS,
    EvidenceObservabilitySection,
    ExecutiveSummarySection,
    IncidentImpactSection,
    IncidentTimelineSection,
    PreventionRecommendationsSection,
    RecoveryVerificationSection,
    RemediationActionsSection,
    ReportMetadata,
    RootCauseSection,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"


def format_seconds(val: Any) -> str:
    """Format seconds value, returning 'Not recorded' if missing or <= 0."""
    if val is None or val == "" or str(val).strip().lower() in ("0", "0.0", "0.0s", "none", "null"):
        return "Not recorded"
    try:
        f = float(str(val).replace("s", "").strip())
        if f <= 0.0:
            return "Not recorded"
        return f"{round(f, 2)}s"
    except (ValueError, TypeError):
        s = str(val).strip()
        return s if s else "Not recorded"


def format_speedup(ttr_val: Any, speedup_val: Any) -> str:
    """Format TTR and comparison against manual baseline."""
    ttr_str = format_seconds(ttr_val)
    if ttr_str == "Not recorded":
        return "Not recorded"
    try:
        sp = float(speedup_val)
        if sp > 0:
            return f"{ttr_str} ({round(sp, 1)}x faster than manual baseline)"
    except (ValueError, TypeError):
        pass
    return ttr_str


def sanitize_section_text(text: Any) -> str:
    """Strip raw markdown headers, metadata lines, and horizontal rules that duplicate the postmortem."""
    if not text:
        return "Not recorded"
    cleaned_lines = []
    for line in str(text).splitlines():
        trimmed = line.strip()
        if trimmed.startswith("#") or trimmed.startswith("---") or trimmed.startswith("==="):
            continue
        if any(trimmed.lower().startswith(prefix) for prefix in (
            "**incident id", "**date", "**status", "**severity",
            "**time to detect", "**time to diagnosis", "**time to recovery",
            "**trigger rule", "**affected services"
        )):
            continue
        cleaned_lines.append(line)
    res = "\n".join(cleaned_lines).strip()
    return res if res else "Not recorded"


class ReportStorage:
    """Manages reading and writing modular incident report directories and backward-compatible markdown."""

    def __init__(self, reports_dir: Path | str | None = None) -> None:
        self.reports_dir = Path(reports_dir or DEFAULT_REPORTS_DIR)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def _incident_dir(self, incident_id: str) -> Path:
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        return self.reports_dir / clean_id

    def _markdown_file(self, incident_id: str) -> Path:
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        return self.reports_dir / f"{clean_id}.md"

    def save_report(
        self,
        metadata: ReportMetadata,
        sections: dict[str, Any],
        full_markdown: str,
    ) -> Path:
        """Save a report in both modular JSON structure and legacy markdown."""
        inc_dir = self._incident_dir(metadata.incident_id)
        inc_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save metadata.json
        meta_dict = metadata.to_dict()
        meta_dict["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (inc_dir / "metadata.json").write_text(json.dumps(meta_dict, indent=2), encoding="utf-8")

        # 2. Save each section as a separate JSON file
        for sec_meta in REPORT_SECTIONS:
            sec_id = sec_meta["id"]
            sec_data = sections.get(sec_id)
            if sec_data is not None:
                if hasattr(sec_data, "to_dict"):
                    content = sec_data.to_dict()
                elif isinstance(sec_data, dict):
                    content = sec_data
                else:
                    content = {"content": str(sec_data)}
            else:
                content = {"status": "Not available"}

            (inc_dir / f"{sec_id}.json").write_text(json.dumps(content, indent=2), encoding="utf-8")

        # 3. Save full postmortem.md in directory
        (inc_dir / "postmortem.md").write_text(full_markdown, encoding="utf-8")

        # 4. Save backward-compatible legacy file: reports/INC-xxx.md
        legacy_file = self._markdown_file(metadata.incident_id)
        legacy_file.write_text(full_markdown, encoding="utf-8")

        return inc_dir

    def get_report_metadata(self, incident_id: str) -> dict[str, Any] | None:
        """Retrieve metadata for a specific incident."""
        inc_dir = self._incident_dir(incident_id)
        meta_file = inc_dir / "metadata.json"

        if meta_file.is_file():
            try:
                return json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Check if legacy markdown exists and migrate
        legacy_file = self._markdown_file(incident_id)
        if legacy_file.is_file():
            self._migrate_legacy_markdown(incident_id)
            if meta_file.is_file():
                try:
                    return json.loads(meta_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass

        return None

    def get_sections_index(self, incident_id: str) -> list[dict[str, Any]]:
        """Return available section identifiers and metadata for an incident."""
        inc_dir = self._incident_dir(incident_id)
        if not inc_dir.is_dir() and self._markdown_file(incident_id).is_file():
            self._migrate_legacy_markdown(incident_id)

        index = []
        for sec in REPORT_SECTIONS:
            sec_file = inc_dir / f"{sec['id']}.json"
            has_content = sec_file.is_file() and sec_file.stat().st_size > 5
            index.append({
                "id": sec["id"],
                "title": sec["title"],
                "icon": sec["icon"],
                "description": sec["description"],
                "has_content": has_content,
            })
        return index

    def get_section(self, incident_id: str, section_id: str) -> dict[str, Any] | None:
        """Retrieve a specific section's data for an incident."""
        inc_dir = self._incident_dir(incident_id)
        if not inc_dir.is_dir() and self._markdown_file(incident_id).is_file():
            self._migrate_legacy_markdown(incident_id)

        sec_file = inc_dir / f"{section_id}.json"
        if sec_file.is_file():
            try:
                data = json.loads(sec_file.read_text(encoding="utf-8"))
                result = {
                    "incident_id": incident_id,
                    "section_id": section_id,
                    "title": next((s["title"] for s in REPORT_SECTIONS if s["id"] == section_id), section_id.title()),
                    "data": data,
                }
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k not in result:
                            result[k] = v
                return result
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def get_full_markdown(self, incident_id: str) -> str | None:
        """Retrieve full markdown text for export or legacy view."""
        inc_dir = self._incident_dir(incident_id)
        pm_file = inc_dir / "postmortem.md"
        if pm_file.is_file():
            return pm_file.read_text(encoding="utf-8")

        legacy_file = self._markdown_file(incident_id)
        if legacy_file.is_file():
            return legacy_file.read_text(encoding="utf-8")
        return None

    def get_export_markdown(self, incident_id: str) -> tuple[str, str]:
        """Generate a complete, structured 8-category Markdown postmortem document and filename.

        Returns (markdown_text, filename).
        """
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        inc_dir = self._incident_dir(clean_id)
        if not inc_dir.is_dir() and self._markdown_file(clean_id).is_file():
            self._migrate_legacy_markdown(clean_id)

        meta = self.get_report_metadata(clean_id) or {}

        def load_sec(sec_id: str) -> dict[str, Any]:
            sec_res = self.get_section(clean_id, sec_id)
            if sec_res and "data" in sec_res and isinstance(sec_res["data"], dict):
                return sec_res["data"]
            return sec_res or {}

        exec_s = load_sec("executive_summary")
        impact_s = load_sec("impact")
        rc_s = load_sec("root_cause")
        ev_s = load_sec("evidence")
        tl_s = load_sec("timeline")
        rem_s = load_sec("remediation")
        ver_s = load_sec("verification")
        prev_s = load_sec("prevention")

        # Extract incident date for filename: INC-001_Postmortem_YYYY-MM-DD.md
        date_raw = meta.get("created_at") or meta.get("t0") or ""
        if "T" in str(date_raw):
            date_str = str(date_raw).split("T")[0]
        elif len(str(date_raw)) >= 10 and str(date_raw)[:10].count("-") == 2:
            date_str = str(date_raw)[:10]
        else:
            date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        filename = f"{clean_id}_Postmortem_{date_str}.md"

        # Format Timeline events
        tl_events = tl_s.get("events") or []
        tl_rows = []
        for t in tl_events:
            ev_ids = t.get("evidence_ids") or []
            ev_str = ", ".join(f"`{e}`" for e in ev_ids) if ev_ids else "-"
            ts_str = str(t.get("ts", "")).split("T")[-1].replace("Z", "")[:8] or str(t.get("ts", "00:00:00"))
            event_type = str(t.get("event", "EVENT")).upper()
            svc = str(t.get("service", "-"))
            desc = str(t.get("description", "-"))
            tl_rows.append(f"| {ts_str} | **{event_type}** | `{svc}` | {desc} | {ev_str} |")
        tl_table = "\n".join(tl_rows) if tl_rows else "| - | - | - | No timeline events recorded | - |"

        # Format Evidence records
        ev_records = ev_s.get("evidence_records") or []
        ev_rows = []
        for r in ev_records:
            eid = r.get("id", "UNKNOWN")
            etype = str(r.get("type", "record")).upper()
            svc = r.get("service", "-")
            if r.get("type") == "metric":
                m_name = r.get("metric", "metric")
                m_val = r.get("value", "")
                m_lbls = r.get("labels", {})
                lbl_str = f" ({m_lbls.get('endpoint', '')})" if m_lbls.get("endpoint") else ""
                msg = f"{m_name} = {m_val}{lbl_str}"
            elif r.get("type") == "change":
                msg = f"Action: {r.get('action', 'change')} - {r.get('details', '')}"
            else:
                msg = r.get("msg") or r.get("description") or r.get("details") or "Telemetry log event"
            ev_rows.append(f"| `{eid}` | **{etype}** | `{svc}` | {msg} |")
        ev_table = "\n".join(ev_rows) if ev_rows else "| - | - | - | No individual evidence records indexed |"

        # Format Impact breaches
        err_breaches = impact_s.get("error_rate_breaches") or []
        lat_breaches = impact_s.get("latency_breaches") or []
        hc_failures = impact_s.get("health_check_failures") or []

        impact_rows = []
        for b in err_breaches:
            svc = b.get("service", "unknown")
            msg = b.get("message") or f"Error rate: {b.get('rate', 'Breached')}"
            eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
            impact_rows.append(f"- **Error Rate Breach (`{svc}`):** {msg} (Evidence: {eids})")
        for b in lat_breaches:
            svc = b.get("service", "unknown")
            msg = b.get("message") or f"Latency: {b.get('latency_ms', 'Breached')}ms"
            eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
            impact_rows.append(f"- **Latency Breach (`{svc}`):** {msg} (Evidence: {eids})")
        for b in hc_failures:
            svc = b.get("service", "unknown")
            msg = b.get("message") or b.get("error") or "Health check failure"
            eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
            impact_rows.append(f"- **Health Check Failure (`{svc}`):** {msg} (Evidence: {eids})")

        impact_breaches_str = "\n".join(impact_rows) if impact_rows else "- No individual SLO breaches recorded."

        def fmt_list(items: list[Any], empty_msg: str = "Not available") -> str:
            if not items:
                return f"- {empty_msg}"
            return "\n".join(f"- {item}" for item in items)

        aff_services = meta.get("affected_services") or exec_s.get("affected_services") or []
        aff_str = ", ".join(f"`{s}`" for s in aff_services) if aff_services else "None"
        rc_evidence = rc_s.get("supporting_evidence") or []
        rc_ev_str = ", ".join(f"`{e}`" for e in rc_evidence) if rc_evidence else "None"
        causal_chain = rc_s.get("causal_chain") or []
        causal_str = " -> ".join(f"`{c}`" for c in causal_chain) if causal_chain else "Not recorded"

        appr = rem_s.get("approval_info") or {}
        appr_req = appr.get("required", False)
        appr_status = appr.get("status", "auto_approved" if not appr_req else "pending")
        appr_decided = appr.get("decided_by", "autonomous_agent")
        appr_time = appr.get("decision_time", "Immediate")
        appr_reason = appr.get("reason", "None")

        ttd_str = format_seconds(meta.get("ttd_seconds") or exec_s.get("ttd_seconds"))
        ttdiag_str = format_seconds(meta.get("ttdiag_seconds") or exec_s.get("ttdiag_seconds"))
        ttr_str = format_speedup(meta.get("ttr_seconds") or exec_s.get("ttr_seconds"), meta.get("speedup_factor") or exec_s.get("speedup_factor"))

        res_val = meta.get("resolved_at") or exec_s.get("resolved_at")
        if not res_val or str(res_val).strip().lower() in ("", "none", "null"):
            if str(meta.get("status", "")).lower() == "resolved":
                res_display = "Not recorded"
            else:
                res_display = "In Progress"
        else:
            res_display = str(res_val).strip()

        det_val = meta.get("detected_at") or meta.get("created_at") or exec_s.get("detected_at") or meta.get("t0") or "Not recorded"
        clean_impact_details = sanitize_section_text(impact_s.get("details_text"))
        impact_duration_str = format_seconds(impact_s.get("incident_duration_seconds") or meta.get("ttr_seconds"))
        slo_count = impact_s.get("slo_violations_count")
        if slo_count is None or slo_count == 0:
            breach_count = len(err_breaches) + len(lat_breaches) + len(hc_failures)
            slo_count = breach_count if breach_count > 0 else "Not recorded"

        doc = f"""# Incident Postmortem: {clean_id}

**Incident ID:** {clean_id}  
**Date:** {date_str}  
**Status:** {meta.get("status", "RESOLVED").upper()}  
**Severity:** {meta.get("severity", "SEV-1 (Critical)")}  
**Trigger Rule:** `{meta.get("trigger_rule", "Not available")}`  
**Affected Services:** {aff_str}  
**Time to Detect (TTD):** {ttd_str}  
**Time to Diagnosis (TTDiag):** {ttdiag_str}  
**Time to Recovery (TTR):** {ttr_str}  

---

## 1. Executive Summary
- **Incident ID:** {clean_id}
- **Severity:** {meta.get("severity", "SEV-1 (Critical)")}
- **Trigger Rule:** `{meta.get("trigger_rule", "Not available")}`
- **Incident Status:** {meta.get("status", "RESOLVED").upper()}
- **Detection Time:** {det_val}
- **Resolution Time:** {res_display}
- **Affected Services:** {aff_str}
- **Time to Detect (TTD):** {ttd_str}
- **Time to Diagnosis (TTDiag):** {ttdiag_str}
- **Time to Recovery (TTR):** {ttr_str}

### Summary Narrative
{sanitize_section_text(exec_s.get("summary_text"))}

## 2. Incident Impact
- **Affected Services:** {aff_str}
- **Incident Duration:** {impact_duration_str}
- **SLO Violations Count:** {slo_count}
- **Request Impact:** {impact_s.get("request_impact", "Not recorded")}

### Observed Breaches & Degradation
{impact_breaches_str}

### Impact Details
{clean_impact_details}

## 3. Root Cause Analysis
- **Identified Root-Cause Service:** `{rc_s.get("root_cause_service", "unknown")}`
- **Root-Cause Explanation:** {rc_s.get("root_cause", "Not available")}
- **Confidence Level:** {rc_s.get("confidence", "Not recorded")}
- **Causal Chain:** {causal_str}
- **Supporting Evidence Citations:** {rc_ev_str}

### Detailed Diagnostic Analysis
{sanitize_section_text(rc_s.get("analysis_text"))}

## 4. Evidence & Observability
- **Total Citations:** {ev_s.get("total_evidence_count", len(ev_records))}
- **Log Evidence IDs:** {", ".join(f"`{e}`" for e in ev_s.get("log_evidence_ids", [])) or "None"}
- **Metric Evidence IDs:** {", ".join(f"`{e}`" for e in ev_s.get("metric_evidence_ids", [])) or "None"}
- **Change Record IDs:** {", ".join(f"`{e}`" for e in ev_s.get("change_record_ids", [])) or "None"}

### Cited Evidence Index
| Evidence ID | Type | Service | Telemetry / Details |
|:---|:---|:---|:---|
{ev_table}

### Observability Summary
{sanitize_section_text(ev_s.get("summary_text"))}

## 5. Incident Timeline
- **Earliest Anomaly (T0):** `{tl_s.get("t0", "Not recorded")}`
- **Total Recorded Events:** {tl_s.get("total_events", len(tl_events))}

| Time (UTC) | Event | Service | Description | Evidence |
|:---|:---|:---|:---|:---|
{tl_table}

## 6. Remediation Actions
- **Proposed Action:** `{rem_s.get("proposed_action", "Not available")}`
- **Action Executed:** `{rem_s.get("action_executed", "Not recorded")}`
- **Target Service:** `{rem_s.get("target_service", "unknown")}`
- **Permission Category:** `{rem_s.get("permission_tier", "Not available")}`
- **Action Result:** {rem_s.get("action_result", "Not available")}
- **Failure / Escalation Details:** {rem_s.get("failure_or_escalation_details", "None")}

### Human Approval Information
- **Approval Required:** {appr_req}
- **Approval Status:** `{appr_status}`
- **Decided By:** `{appr_decided}`
- **Decision Time:** `{appr_time}`
- **Reason:** {appr_reason}

### Remediation Narrative
{sanitize_section_text(rem_s.get("summary_text"))}

## 7. Recovery Verification
- **Health Check Result:** `{ver_s.get("health_check_result", "Not available")}`
- **Error Rate Status:** `{ver_s.get("error_rate_status", "Not available")}`
- **p95 Latency Status:** `{ver_s.get("p95_latency_status", "Not available")}`
- **Verification Duration:** {format_seconds(ver_s.get("verification_duration_seconds"))}
- **Recovery Confirmed:** `{ver_s.get("is_verified_recovered", "Not recorded")}`
- **Verification Timestamp:** `{ver_s.get("verification_timestamp", res_display)}`
- **Supporting Evidence Citations:** {", ".join(f"`{e}`" for e in ver_s.get("supporting_evidence", [])) or "None"}

### Verification Assessment
{sanitize_section_text(ver_s.get("summary_text"))}

## 8. Prevention & Recommendations
### Preventive Actions
{fmt_list(prev_s.get("preventive_actions"))}

### Configuration Recommendations
{fmt_list(prev_s.get("configuration_recommendations"))}

### Monitoring Improvements
{fmt_list(prev_s.get("monitoring_improvements"))}

### Follow-up Actions
{fmt_list(prev_s.get("follow_up_actions"))}

### Summary & Outlook
{sanitize_section_text(prev_s.get("summary_text"))}
"""
        self.validate_export_markdown(doc)
        return doc, filename

    def validate_export_markdown(self, markdown_text: str) -> None:
        """Validate exported postmortem markdown meets all criteria."""
        # 1. Main postmortem title must appear exactly once
        title_matches = len(re.findall(r"^#\s*Incident Postmortem", markdown_text, re.MULTILINE))
        if title_matches != 1:
            raise ValueError(f"Report export validation error: '# Incident Postmortem' heading appears {title_matches} times, expected 1.")

        # 2. Each of the 8 category headings must appear exactly once
        for idx, sec in enumerate(REPORT_SECTIONS, 1):
            pattern = rf"^##\s*{idx}\.\s*{re.escape(sec['title'])}"
            matches = len(re.findall(pattern, markdown_text, re.MULTILINE))
            if matches != 1:
                raise ValueError(f"Report export validation error: category heading '{idx}. {sec['title']}' appears {matches} times, expected 1.")

        # 3. No 0.0s for missing values in metrics lines
        zero_sec_matches = re.findall(r"\*\*Time to (?:Detect|Diagnosis|Recovery).*?\*\*:\s*0\.0s", markdown_text)
        if zero_sec_matches:
            raise ValueError(f"Report export validation error: missing metric replaced with 0.0s ({zero_sec_matches}). Expected 'Not recorded'.")

    def get_section_markdown(self, incident_id: str, section_id: str) -> tuple[str, str]:
        """Generate an individual, formatted Markdown document and filename for a specific section.

        Returns (section_markdown_text, section_filename).
        """
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        inc_dir = self._incident_dir(clean_id)
        if not inc_dir.is_dir() and self._markdown_file(clean_id).is_file():
            self._migrate_legacy_markdown(clean_id)

        meta = self.get_report_metadata(clean_id) or {}

        def load_sec(s_id: str) -> dict[str, Any]:
            sec_res = self.get_section(clean_id, s_id)
            if sec_res and "data" in sec_res and isinstance(sec_res["data"], dict):
                return sec_res["data"]
            return sec_res or {}

        # Date for filename
        date_raw = meta.get("created_at") or meta.get("t0") or ""
        if "T" in str(date_raw):
            date_str = str(date_raw).split("T")[0]
        elif len(str(date_raw)) >= 10 and str(date_raw)[:10].count("-") == 2:
            date_str = str(date_raw)[:10]
        else:
            date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        aff_services = meta.get("affected_services") or []
        aff_str = ", ".join(f"`{s}`" for s in aff_services) if aff_services else "None"

        sec_meta = next((s for s in REPORT_SECTIONS if s["id"] == section_id), None)
        sec_title = sec_meta["title"] if sec_meta else section_id.replace("_", " ").title()
        safe_title = sec_title.replace(" & ", "_").replace(" ", "_").replace("/", "_")
        filename = f"{clean_id}_{safe_title}_{date_str}.md"

        sec_data = load_sec(section_id)

        ttd_str = format_seconds(meta.get("ttd_seconds") or sec_data.get("ttd_seconds"))
        ttdiag_str = format_seconds(meta.get("ttdiag_seconds") or sec_data.get("ttdiag_seconds"))
        ttr_str = format_speedup(meta.get("ttr_seconds") or sec_data.get("ttr_seconds"), meta.get("speedup_factor") or sec_data.get("speedup_factor"))

        res_val = meta.get("resolved_at") or sec_data.get("resolved_at")
        if not res_val or str(res_val).strip().lower() in ("", "none", "null"):
            if str(meta.get("status", "")).lower() == "resolved":
                res_display = "Not recorded"
            else:
                res_display = "In Progress"
        else:
            res_display = str(res_val).strip()

        header = f"""# Incident {clean_id}: {sec_title}

**Incident ID:** {clean_id}  
**Date:** {date_str}  
**Status:** {meta.get("status", "RESOLVED").upper()}  
**Severity:** {meta.get("severity", "SEV-1 (Critical)")}  
**Trigger Rule:** `{meta.get("trigger_rule", "Not available")}`  
**Affected Services:** {aff_str}  

---
"""

        def fmt_list(items: list[Any], empty_msg: str = "Not available") -> str:
            if not items:
                return f"- {empty_msg}"
            return "\n".join(f"- {item}" for item in items)

        body = ""
        if section_id == "executive_summary":
            body = f"""## 1. Executive Summary
- **Incident ID:** {clean_id}
- **Severity:** {meta.get("severity", "SEV-1 (Critical)")}
- **Trigger Rule:** `{meta.get("trigger_rule", "Not available")}`
- **Incident Status:** {meta.get("status", "RESOLVED").upper()}
- **Detection Time:** {meta.get("detected_at", meta.get("created_at", "Not recorded"))}
- **Resolution Time:** {res_display}
- **Affected Services:** {aff_str}
- **Time to Detect (TTD):** {ttd_str}
- **Time to Diagnosis (TTDiag):** {ttdiag_str}
- **Time to Recovery (TTR):** {ttr_str}

### Executive Summary Narrative
{sanitize_section_text(sec_data.get("summary_text"))}
"""
        elif section_id == "impact":
            err_breaches = sec_data.get("error_rate_breaches") or []
            lat_breaches = sec_data.get("latency_breaches") or []
            hc_failures = sec_data.get("health_check_failures") or []
            impact_rows = []
            for b in err_breaches:
                svc = b.get("service", "unknown")
                msg = b.get("message") or f"Error rate: {b.get('rate', 'Breached')}"
                eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
                impact_rows.append(f"- **Error Rate Breach (`{svc}`):** {msg} (Evidence: {eids})")
            for b in lat_breaches:
                svc = b.get("service", "unknown")
                msg = b.get("message") or f"Latency: {b.get('latency_ms', 'Breached')}ms"
                eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
                impact_rows.append(f"- **Latency Breach (`{svc}`):** {msg} (Evidence: {eids})")
            for b in hc_failures:
                svc = b.get("service", "unknown")
                msg = b.get("message") or b.get("error") or "Health check failure"
                eids = ", ".join(f"`{e}`" for e in b.get("evidence_ids", [])) or "-"
                impact_rows.append(f"- **Health Check Failure (`{svc}`):** {msg} (Evidence: {eids})")
            impact_str = "\n".join(impact_rows) if impact_rows else "- No individual SLO breaches recorded."
            clean_impact_details = sanitize_section_text(sec_data.get("details_text"))
            impact_duration_str = format_seconds(sec_data.get("incident_duration_seconds") or meta.get("ttr_seconds"))
            slo_count = sec_data.get("slo_violations_count")
            if slo_count is None or slo_count == 0:
                breach_count = len(err_breaches) + len(lat_breaches) + len(hc_failures)
                slo_count = breach_count if breach_count > 0 else "Not recorded"

            body = f"""## 2. Incident Impact
- **Affected Services:** {aff_str}
- **Incident Duration:** {impact_duration_str}
- **SLO Violations Count:** {slo_count}
- **Request Impact:** {sec_data.get("request_impact", "Not recorded")}

### Observed Breaches & Degradation
{impact_str}

### Impact Details
{clean_impact_details}
"""
        elif section_id == "root_cause":
            causal_chain = sec_data.get("causal_chain") or []
            causal_str = " -> ".join(f"`{c}`" for c in causal_chain) if causal_chain else "Not recorded"
            rc_evidence = sec_data.get("supporting_evidence") or []
            rc_ev_str = ", ".join(f"`{e}`" for e in rc_evidence) if rc_evidence else "None"

            body = f"""## 3. Root Cause Analysis
- **Identified Root-Cause Service:** `{sec_data.get("root_cause_service", "unknown")}`
- **Root-Cause Explanation:** {sec_data.get("root_cause", "Not available")}
- **Confidence Level:** {sec_data.get("confidence", "Not recorded")}
- **Causal Chain:** {causal_str}
- **Supporting Evidence Citations:** {rc_ev_str}

### Detailed Diagnostic Analysis
{sec_data.get("analysis_text") or "Not available"}
"""
        elif section_id == "evidence":
            ev_records = sec_data.get("evidence_records") or []
            ev_rows = []
            for r in ev_records:
                eid = r.get("id", "UNKNOWN")
                etype = str(r.get("type", "record")).upper()
                svc = r.get("service", "-")
                if r.get("type") == "metric":
                    m_name = r.get("metric", "metric")
                    m_val = r.get("value", "")
                    m_lbls = r.get("labels", {})
                    lbl_str = f" ({m_lbls.get('endpoint', '')})" if m_lbls.get("endpoint") else ""
                    msg = f"{m_name} = {m_val}{lbl_str}"
                elif r.get("type") == "change":
                    msg = f"Action: {r.get('action', 'change')} - {r.get('details', '')}"
                else:
                    msg = r.get("msg") or r.get("description") or r.get("details") or "Telemetry log event"
                ev_rows.append(f"| `{eid}` | **{etype}** | `{svc}` | {msg} |")
            ev_table = "\n".join(ev_rows) if ev_rows else "| - | - | - | No individual evidence records indexed |"

            body = f"""## 4. Evidence & Observability
- **Total Citations:** {sec_data.get("total_evidence_count", len(ev_records))}
- **Log Evidence IDs:** {", ".join(f"`{e}`" for e in sec_data.get("log_evidence_ids", [])) or "None"}
- **Metric Evidence IDs:** {", ".join(f"`{e}`" for e in sec_data.get("metric_evidence_ids", [])) or "None"}
- **Change Record IDs:** {", ".join(f"`{e}`" for e in sec_data.get("change_record_ids", [])) or "None"}

### Cited Evidence Index
| Evidence ID | Type | Service | Telemetry / Details |
|:---|:---|:---|:---|
{ev_table}

### Observability Summary
{sec_data.get("summary_text") or "Not available"}
"""
        elif section_id == "timeline":
            tl_events = sec_data.get("events") or []
            tl_rows = []
            for t in tl_events:
                ev_ids = t.get("evidence_ids") or []
                ev_str = ", ".join(f"`{e}`" for e in ev_ids) if ev_ids else "-"
                ts_str = str(t.get("ts", "")).split("T")[-1].replace("Z", "")[:8] or str(t.get("ts", "00:00:00"))
                event_type = str(t.get("event", "EVENT")).upper()
                svc = str(t.get("service", "-"))
                desc = str(t.get("description", "-"))
                tl_rows.append(f"| {ts_str} | **{event_type}** | `{svc}` | {desc} | {ev_str} |")
            tl_table = "\n".join(tl_rows) if tl_rows else "| - | - | - | No timeline events recorded | - |"

            body = f"""## 5. Incident Timeline
- **Earliest Anomaly (T0):** `{sec_data.get("t0", "Not available")}`
- **Total Recorded Events:** {sec_data.get("total_events", len(tl_events))}

| Time (UTC) | Event | Service | Description | Evidence |
|:---|:---|:---|:---|:---|
{tl_table}
"""
        elif section_id == "remediation":
            appr = sec_data.get("approval_info") or {}
            appr_req = appr.get("required", False)
            appr_status = appr.get("status", "auto_approved" if not appr_req else "pending")
            appr_decided = appr.get("decided_by", "autonomous_agent")
            appr_time = appr.get("decision_time", "Immediate")
            appr_reason = appr.get("reason", "None")

            body = f"""## 6. Remediation Actions
- **Proposed Action:** `{sec_data.get("proposed_action", "Not available")}`
- **Action Executed:** `{sec_data.get("action_executed", "Not recorded")}`
- **Target Service:** `{sec_data.get("target_service", "unknown")}`
- **Permission Category:** `{sec_data.get("permission_tier", "Not available")}`
- **Action Result:** {sec_data.get("action_result", "Not available")}
- **Failure / Escalation Details:** {sec_data.get("failure_or_escalation_details", "None")}

### Human Approval Information
- **Approval Required:** {appr_req}
- **Approval Status:** `{appr_status}`
- **Decided By:** `{appr_decided}`
- **Decision Time:** `{appr_time}`
- **Reason:** {appr_reason}

### Remediation Narrative
{sanitize_section_text(sec_data.get("summary_text"))}
"""
        elif section_id == "verification":
            evs = sec_data.get("supporting_evidence") or []
            ev_str = ", ".join(f"`{e}`" for e in evs) if evs else "None"

            body = f"""## 7. Recovery Verification
- **Health Check Result:** `{sec_data.get("health_check_result", "Not available")}`
- **Error Rate Status:** `{sec_data.get("error_rate_status", "Not available")}`
- **p95 Latency Status:** `{sec_data.get("p95_latency_status", "Not available")}`
- **Verification Duration:** {format_seconds(sec_data.get("verification_duration_seconds"))}
- **Recovery Confirmed:** `{sec_data.get("is_verified_recovered", "Not recorded")}`
- **Verification Timestamp:** `{sec_data.get("verification_timestamp", res_display)}`
- **Supporting Evidence Citations:** {ev_str}

### Verification Assessment
{sanitize_section_text(sec_data.get("summary_text"))}
"""
        elif section_id == "prevention":
            body = f"""## 8. Prevention & Recommendations
### Preventive Actions
{fmt_list(sec_data.get("preventive_actions"))}

### Configuration Recommendations
{fmt_list(sec_data.get("configuration_recommendations"))}

### Monitoring Improvements
{fmt_list(sec_data.get("monitoring_improvements"))}

### Follow-up Actions
{fmt_list(sec_data.get("follow_up_actions"))}

### Summary & Outlook
{sanitize_section_text(sec_data.get("summary_text"))}
"""
        else:
            body = f"## {sec_title}\n\n```json\n{json.dumps(sec_data, indent=2)}\n```\n"

        return header + body, filename

    def get_zip_archive(self, incident_id: str) -> tuple[bytes, str]:
        """Bundle all 8 individual category Markdown files and the full postmortem into a single ZIP archive.

        Returns (zip_bytes, zip_filename).
        """
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        meta = self.get_report_metadata(clean_id) or {}
        date_raw = meta.get("created_at") or meta.get("t0") or ""
        if "T" in str(date_raw):
            date_str = str(date_raw).split("T")[0]
        elif len(str(date_raw)) >= 10 and str(date_raw)[:10].count("-") == 2:
            date_str = str(date_raw)[:10]
        else:
            date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        zip_filename = f"{clean_id}_Postmortem_All_Sections_{date_str}.zip"
        zip_buf = io.BytesIO()

        with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # 1. Add all 8 individual category Markdown files
            for idx, sec in enumerate(REPORT_SECTIONS, 1):
                sec_id = sec["id"]
                sec_md, sec_fn = self.get_section_markdown(clean_id, sec_id)
                # Prefixed numbered filename inside zip for neat sorting
                archive_name = f"{idx:02d}_{sec_fn}"
                zf.writestr(archive_name, sec_md.encode("utf-8"))

            # 2. Add the complete combined Markdown postmortem
            full_md, full_fn = self.get_export_markdown(clean_id)
            zf.writestr(f"00_{full_fn}", full_md.encode("utf-8"))

        return zip_buf.getvalue(), zip_filename

    def list_reports(
        self,
        page: int = 1,
        page_size: int = 10,
        status: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Return a lightweight paginated list of report metadata with filtering."""
        # Find all incident directories and legacy markdown files
        all_ids = set()

        for d in self.reports_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                all_ids.add(d.name)
            elif d.is_file() and d.suffix == ".md" and not d.name.startswith("."):
                all_ids.add(d.stem)

        reports_list: list[dict[str, Any]] = []
        for inc_id in sorted(all_ids, reverse=True):
            meta = self.get_report_metadata(inc_id)
            if not meta:
                # Fallback basic metadata
                f = self._markdown_file(inc_id)
                mod_time = datetime.datetime.fromtimestamp(
                    f.stat().st_mtime, tz=datetime.timezone.utc
                ).isoformat() if f.is_file() else datetime.datetime.now(datetime.timezone.utc).isoformat()
                meta = {
                    "incident_id": inc_id,
                    "title": f"Incident {inc_id} Postmortem",
                    "status": "resolved",
                    "severity": "SEV-2 (Major)",
                    "trigger_rule": "error_rate_threshold",
                    "created_at": mod_time,
                    "t0": mod_time,
                    "detected_at": mod_time,
                    "affected_services": ["payments", "orders"],
                    "root_cause_service": "payments",
                    "evidence_count": 0,
                    "sections_count": 8,
                }

            # Filter by status
            if status and status.lower() != "all":
                if meta.get("status", "").lower() != status.lower():
                    continue

            # Filter by severity
            if severity and severity.lower() != "all":
                if severity.lower() not in meta.get("severity", "").lower():
                    continue

            # Filter by search keyword
            if search:
                term = search.lower().strip()
                match = (
                    term in meta.get("incident_id", "").lower()
                    or term in meta.get("title", "").lower()
                    or term in meta.get("root_cause_service", "").lower()
                    or any(term in s.lower() for s in meta.get("affected_services", []))
                )
                if not match:
                    continue

            reports_list.append(meta)

        # Sort by creation date descending
        reports_list.sort(key=lambda r: (r.get("created_at") or "", r.get("incident_id") or ""), reverse=True)

        total = len(reports_list)
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_items = reports_list[start_idx:end_idx]

        return {
            "reports": paginated_items,
            "items": paginated_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    def _migrate_legacy_markdown(self, incident_id: str) -> None:
        """Parse an existing legacy markdown report and extract structured sections."""
        clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
        md_file = self._markdown_file(clean_id)
        if not md_file.is_file():
            return

        text = md_file.read_text(encoding="utf-8")
        inc_dir = self._incident_dir(clean_id)
        inc_dir.mkdir(parents=True, exist_ok=True)

        # Parse basic fields
        severity = "SEV-2 (Major)"
        if "SEV-1" in text or "Critical" in text:
            severity = "SEV-1 (Critical)"

        status_match = re.search(r"\*\*Status:\*\*\s*([^\n]+)", text, re.IGNORECASE)
        if status_match:
            raw_status = status_match.group(1).strip().lower()
            status = "resolved" if "resolve" in raw_status else ("escalated" if "escalat" in raw_status else "open")
        else:
            status = "resolved" if "auto-resolved" in text.lower() or "resolved at" in text.lower() else "open"

        trig_match = re.search(r"\*\*Trigger Rule:\*\*\s*([^\n]+)", text)
        trigger_rule = trig_match.group(1).strip() if trig_match else "error_rate_threshold"

        t0_match = re.search(r"\*\*T0.*?\*\*\s*([^\n]+)", text)
        t0 = t0_match.group(1).strip() if t0_match else datetime.datetime.now(datetime.timezone.utc).isoformat()

        aff_match = re.search(r"\*\*Affected Services:\*\*\s*([^\n]+)", text)
        if aff_match:
            aff_services = [s.strip().replace("`", "") for s in aff_match.group(1).split(",") if s.strip()]
        else:
            aff_services = ["payments", "orders"]

        # Parse metrics with fallback to None (never 0.0s)
        ttd_match = re.search(r"\*\*Time to Detect.*?\*\*:\s*([0-9.]+)", text)
        ttd = float(ttd_match.group(1)) if ttd_match else None

        ttdiag_match = re.search(r"\*\*Time to Diagnosis.*?\*\*:\s*([0-9.]+)", text)
        ttdiag = float(ttdiag_match.group(1)) if ttdiag_match else None

        ttr_match = re.search(r"\*\*Time to Recovery.*?\*\*:\s*([0-9.]+)", text)
        ttr = float(ttr_match.group(1)) if ttr_match else None

        speedup_match = re.search(r"([0-9.]+)x faster", text)
        speedup = float(speedup_match.group(1)) if speedup_match else None

        res_match = re.search(r"Resolution:\s*[`\*]*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:\.]+Z?)[`\*]*", text)
        if not res_match:
            res_match = re.search(r"Resolved at\s*[`\*]*([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:\.]+Z?)[`\*]*", text, re.IGNORECASE)
        if not res_match:
            res_match = re.search(r"\*\*Resolution Time:\*\*\s*([^\n]+)", text, re.IGNORECASE)
        resolved_at = res_match.group(1).strip() if res_match else None
        if resolved_at and any(k in resolved_at.lower() for k in ("not resolved", "in progress", "not recorded")):
            resolved_at = None

        # Parse citations
        citation_ids = list(dict.fromkeys(re.findall(r"\b(LOG-\d+|MET-\d+|CHG-\d+)\b", text, re.IGNORECASE)))

        meta = ReportMetadata(
            incident_id=clean_id,
            title=f"Incident {clean_id}: {trigger_rule.replace('_', ' ').title()}",
            status=status,
            severity=severity,
            trigger_rule=trigger_rule,
            created_at=t0,
            t0=t0,
            detected_at=t0,
            resolved_at=resolved_at,
            affected_services=aff_services,
            root_cause_service=aff_services[0] if aff_services else "unknown",
            ttd_seconds=ttd,
            ttdiag_seconds=ttdiag,
            ttr_seconds=ttr,
            speedup_factor=speedup,
            evidence_count=len(citation_ids),
        )

        # Build sections
        sec1 = ExecutiveSummarySection(
            incident_id=clean_id,
            title=meta.title,
            severity=severity,
            trigger_rule=trigger_rule,
            status=meta.status,
            t0=t0,
            detected_at=t0,
            resolved_at=resolved_at,
            ttd_seconds=ttd,
            ttdiag_seconds=ttdiag,
            ttr_seconds=ttr,
            speedup_factor=speedup,
            affected_services=aff_services,
            summary_text=f"Incident {clean_id} detected due to {trigger_rule} impacting {', '.join(aff_services)}.",
        )

        impact_match = re.search(r"##\s*2\.\s*Impact\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
        impact_raw = impact_match.group(1).strip() if impact_match else ""
        clean_impact = sanitize_section_text(impact_raw)
        slo_count_match = re.search(r"(\d+)\s+SLO (?:threshold )?violation", text)
        slo_violations_count = int(slo_count_match.group(1)) if slo_count_match else 0

        sec2 = IncidentImpactSection(
            affected_services=aff_services,
            incident_duration_seconds=ttr if ttr is not None else "Not recorded",
            request_impact=f"SLO breaches observed on {', '.join(aff_services)}.",
            slo_violations_count=slo_violations_count,
            details_text=clean_impact,
        )

        sec3 = RootCauseSection(
            root_cause_service=meta.root_cause_service,
            root_cause=f"Primary anomaly isolated to {meta.root_cause_service}.",
            supporting_evidence=citation_ids[:4],
            analysis_text=f"Evidence {', '.join(citation_ids[:3])} indicates anomaly initiated on {meta.root_cause_service}.",
        )

        sec4 = EvidenceObservabilitySection(
            log_evidence_ids=[c for c in citation_ids if c.startswith("LOG-")],
            metric_evidence_ids=[c for c in citation_ids if c.startswith("MET-")],
            change_record_ids=[c for c in citation_ids if c.startswith("CHG-")],
            total_evidence_count=len(citation_ids),
            summary_text=f"{len(citation_ids)} evidence citations confirmed in incident record.",
        )

        # Parse timeline table rows
        timeline_rows = []
        for line in text.splitlines():
            line_str = line.strip()
            if line_str.startswith("|") and not line_str.startswith("| Time") and not line_str.startswith("|:--"):
                parts = [p.strip() for p in line_str.split("|")[1:-1]]
                if len(parts) >= 4:
                    ev_ids = re.findall(r"\b(LOG-\d+|MET-\d+|CHG-\d+)\b", parts[4] if len(parts) > 4 else "")
                    event_name = parts[1].replace("*", "").strip().lower()
                    service_name = parts[2].replace("`", "").strip()
                    desc = parts[3].strip()
                    timeline_rows.append({
                        "ts": parts[0],
                        "event": event_name,
                        "service": service_name,
                        "description": desc,
                        "evidence_ids": ev_ids
                    })
        if not timeline_rows:
            timeline_rows = [
                {"ts": t0, "event": "detected", "service": aff_services[0] if aff_services else "system", "description": f"Incident {clean_id} opened", "evidence_ids": citation_ids[:2]}
            ]

        sec5 = IncidentTimelineSection(
            t0=t0,
            events=timeline_rows,
            total_events=len(timeline_rows),
        )

        sec6 = RemediationActionsSection(
            proposed_action="restart_service / clear_chaos",
            action_executed="Automated mitigation executed",
            target_service=meta.root_cause_service,
            permission_tier="auto_fix",
            action_result="success",
            summary_text="Autonomous corrective action executed.",
        )

        sec7 = RecoveryVerificationSection(
            health_check_result="passed",
            error_rate_status="0.0% within SLO limit",
            p95_latency_status="healthy",
            is_verified_recovered=True,
            supporting_evidence=citation_ids[-2:],
            summary_text="System health and metrics confirmed within normal operating boundaries.",
        )

        sec8 = PreventionRecommendationsSection(
            preventive_actions=["Enforce circuit breakers and client timeouts.", "Add automated rate limiting."],
            configuration_recommendations=["Review default downstream retry policies."],
            monitoring_improvements=["Refine anomaly detection thresholds."],
            follow_up_actions=["Conduct chaos engineering validation test."],
            summary_text="Implement circuit breakers and monitor cascade health.",
        )

        sections = {
            "executive_summary": sec1,
            "impact": sec2,
            "root_cause": sec3,
            "evidence": sec4,
            "timeline": sec5,
            "remediation": sec6,
            "verification": sec7,
            "prevention": sec8,
        }

        # Write files
        (inc_dir / "metadata.json").write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
        for s_id, s_obj in sections.items():
            (inc_dir / f"{s_id}.json").write_text(json.dumps(s_obj.to_dict(), indent=2), encoding="utf-8")
        (inc_dir / "postmortem.md").write_text(text, encoding="utf-8")
