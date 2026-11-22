"""Data models for modular, categorized incident postmortems and reports.

Categories per specification:
1. Executive Summary (executive_summary)
2. Incident Impact (impact)
3. Root Cause Analysis (root_cause)
4. Evidence & Observability (evidence)
5. Incident Timeline (timeline)
6. Remediation Actions (remediation)
7. Recovery Verification (verification)
8. Prevention & Recommendations (prevention)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ReportMetadata:
    """Lightweight metadata for report lists and indexing."""

    incident_id: str
    title: str
    status: str
    severity: str
    trigger_rule: str
    created_at: str
    t0: str
    detected_at: str
    resolved_at: str | None = None
    affected_services: list[str] = field(default_factory=list)
    root_cause_service: str = "unknown"
    ttd_seconds: float | str | None = None
    ttdiag_seconds: float | str | None = None
    ttr_seconds: float | str | None = None
    speedup_factor: float | str | None = None
    evidence_count: int = 0
    sections_count: int = 8
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutiveSummarySection:
    """Section 1: Executive Summary."""

    incident_id: str
    title: str
    severity: str
    trigger_rule: str
    status: str
    t0: str
    detected_at: str
    resolved_at: str | None = None
    ttd_seconds: float | str | None = None
    ttdiag_seconds: float | str | None = None
    ttr_seconds: float | str | None = None
    affected_services: list[str] = field(default_factory=list)
    speedup_factor: float | str | None = None
    manual_baseline_seconds: float | str | None = None
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentImpactSection:
    """Section 2: Incident Impact."""

    affected_services: list[str] = field(default_factory=list)
    incident_duration_seconds: float | str = "Not available"
    error_rate_breaches: list[dict[str, Any]] = field(default_factory=list)
    latency_breaches: list[dict[str, Any]] = field(default_factory=list)
    health_check_failures: list[dict[str, Any]] = field(default_factory=list)
    request_impact: str = "Not available"
    slo_violations_count: int = 0
    details_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RootCauseSection:
    """Section 3: Root Cause Analysis."""

    root_cause_service: str = "unknown"
    root_cause: str = "Not available"
    causal_chain: list[str] = field(default_factory=list)
    confidence: float | str = "Not recorded"
    supporting_evidence: list[str] = field(default_factory=list)
    analysis_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceObservabilitySection:
    """Section 4: Evidence & Observability."""

    log_evidence_ids: list[str] = field(default_factory=list)
    metric_evidence_ids: list[str] = field(default_factory=list)
    change_record_ids: list[str] = field(default_factory=list)
    evidence_records: list[dict[str, Any]] = field(default_factory=list)
    total_evidence_count: int = 0
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentTimelineSection:
    """Section 5: Incident Timeline."""

    t0: str = "Not available"
    t0_unix: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    total_events: int = 0
    milestones: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemediationActionsSection:
    """Section 6: Remediation Actions."""

    proposed_action: str = "Not available"
    action_executed: str = "Not recorded"
    target_service: str = "unknown"
    permission_tier: str = "Not available"  # auto_fix, ask_first, never_do
    approval_info: dict[str, Any] = field(
        default_factory=lambda: {
            "required": False,
            "status": "auto_approved",
            "decided_by": "autonomous_agent",
            "decision_time": "Not applicable",
            "reason": "Permitted tier action executed without manual approval",
        }
    )
    action_result: str = "Not available"
    failure_or_escalation_details: str = "None"
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecoveryVerificationSection:
    """Section 7: Recovery Verification."""

    health_check_result: str = "Not available"
    error_rate_status: str = "Not available"
    p95_latency_status: str = "Not available"
    verification_duration_seconds: float | str = "Not available"
    is_verified_recovered: bool = False
    verification_timestamp: str = "Not recorded"
    supporting_evidence: list[str] = field(default_factory=list)
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreventionRecommendationsSection:
    """Section 8: Prevention & Recommendations."""

    preventive_actions: list[str] = field(default_factory=list)
    configuration_recommendations: list[str] = field(default_factory=list)
    monitoring_improvements: list[str] = field(default_factory=list)
    follow_up_actions: list[str] = field(default_factory=list)
    summary_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Section identifiers and titles in standardized presentation order
REPORT_SECTIONS = [
    {
        "id": "executive_summary",
        "title": "Executive Summary",
        "icon": "fa-clipboard-check",
        "description": "High-level incident summary, severity, and key time metrics.",
    },
    {
        "id": "impact",
        "title": "Incident Impact",
        "icon": "fa-chart-pie",
        "description": "Affected microservices, SLO error rates, and latency spikes.",
    },
    {
        "id": "root_cause",
        "title": "Root Cause Analysis",
        "icon": "fa-magnifying-glass-chart",
        "description": "Identified root cause service, failure chain, and cited claims.",
    },
    {
        "id": "evidence",
        "title": "Evidence & Observability",
        "icon": "fa-database",
        "description": "Audit changes (CHG-x), logs (LOG-x), and metric breaches (MET-x).",
    },
    {
        "id": "timeline",
        "title": "Incident Timeline",
        "icon": "fa-clock-rotate-left",
        "description": "Chronological audit-backed sequence from earliest anomaly (T0) to resolution.",
    },
    {
        "id": "remediation",
        "title": "Remediation Actions",
        "icon": "fa-wrench",
        "description": "Autonomous fixes, permission tier checks, and human approvals.",
    },
    {
        "id": "verification",
        "title": "Recovery Verification",
        "icon": "fa-shield-halved",
        "description": "Health check confirmation, error rate, and p95 post-fix validation.",
    },
    {
        "id": "prevention",
        "title": "Prevention & Recommendations",
        "icon": "fa-lightbulb",
        "description": "Action items, architectural safeguards, and monitoring adjustments.",
    },
]
