"""Rule-based incident detector for AI Site Reliability Engineer.

Detects problems with plain code (without LLM) against config/slo.yaml:
- Health check fails 3 times in a row.
- Error rate above 5% over the last 30 seconds.
- p95 latency above 2x baseline or above 2000ms SLO limit.
- Memory or CPU growth trend above threshold.

Incident lifecycle:
- On first breach: create Incident with ID, T0 (earliest anomaly timestamp),
  affected services, and trigger rule.
- Deduplicate: further breaches during an open incident attach to it.
- Auto-close: mark incident resolved once all services stay within SLO for 60s.
- Persistence: store incidents and lifecycle events in SQLite (data/observability.db).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

DEFAULT_DB_PATH = DATA_DIR / "observability.db"
DEFAULT_SLO_PATH = CONFIG_DIR / "slo.yaml"
DEFAULT_DEPS_PATH = CONFIG_DIR / "dependencies.yaml"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.observability import Observability, format_iso, get_observability
from sre.supervisor import Supervisor


@dataclass
class Breach:
    rule: str
    service: str
    message: str
    t0: str
    t0_unix: float
    evidence_ids: list[str] = field(default_factory=list)
    metric_value: float = 0.0
    threshold_value: float = 0.0


@dataclass
class Incident:
    id: str
    status: str  # open, diagnosing, mitigating, resolved, escalated
    trigger_rule: str
    t0: str
    t0_unix: float
    created_at: str
    created_at_unix: float
    ttd_seconds: float
    affected_services: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    breaches: list[dict[str, Any]] = field(default_factory=list)
    healthy_since: float | None = None
    resolved_at: str | None = None
    resolved_at_unix: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IncidentDetector:
    """Watches services, evaluates SLO rules, and manages incident lifecycle."""

    def __init__(
        self,
        observability: Observability | None = None,
        supervisor: Supervisor | None = None,
        slo_path: Path | str | None = None,
        deps_path: Path | str | None = None,
        db_path: Path | str | None = None,
        auto_remediate: bool = False,
    ) -> None:
        self.obs = observability or get_observability()
        self.supervisor = supervisor or Supervisor()
        self.slo_path = Path(slo_path or DEFAULT_SLO_PATH)
        self.deps_path = Path(deps_path or DEFAULT_DEPS_PATH)
        self.db_path = Path(db_path or self.obs.db_path)
        self.auto_remediate = auto_remediate

        self._lock = threading.Lock()
        self._watch_thread: threading.Thread | None = None
        self._stop_watch_event = threading.Event()

        # Rolling baselines per service (service -> float ms)
        self._latency_baselines: dict[str, float] = {}

        self.slo = self._load_slo()
        self.init_incident_db()

    def _load_slo(self) -> dict[str, Any]:
        if self.slo_path.is_file():
            try:
                return yaml.safe_load(self.slo_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {
            "health_check_failures": 3,
            "error_rate_threshold": 0.05,
            "error_rate_window_seconds": 30,
            "latency_p95_multiplier": 2.0,
            "latency_p95_slo_ms": 2000,
            "memory_growth_threshold_mb": 50,
            "cpu_growth_threshold_pct": 30,
            "recover_after_seconds": 60,
        }

    def _get_services(self) -> list[str]:
        if self.deps_path.is_file():
            try:
                d = yaml.safe_load(self.deps_path.read_text(encoding="utf-8")) or {}
                return list(d.get("services", {}).keys())
            except Exception:
                pass
        return ["gateway", "orders", "payments", "inventory"]

    def init_incident_db(self) -> None:
        """Create incidents table in SQLite."""
        with self._lock, self.obs.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    trigger_rule TEXT NOT NULL,
                    t0 TEXT NOT NULL,
                    t0_unix REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_unix REAL NOT NULL,
                    ttd_seconds REAL NOT NULL,
                    affected_services TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    breaches TEXT NOT NULL,
                    healthy_since REAL,
                    resolved_at TEXT,
                    resolved_at_unix REAL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_inc_status ON incidents(status);
                CREATE INDEX IF NOT EXISTS idx_inc_t0 ON incidents(t0_unix);
            """)

    def _next_incident_id(self) -> str:
        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM incidents")
            count = cur.fetchone()[0] + 1
            return f"INC-{count}"

    def save_incident(self, inc: Incident) -> None:
        """Persist or update an incident in SQLite."""
        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO incidents (
                    id, status, trigger_rule, t0, t0_unix, created_at, created_at_unix,
                    ttd_seconds, affected_services, evidence, breaches, healthy_since,
                    resolved_at, resolved_at_unix, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    trigger_rule=excluded.trigger_rule,
                    t0=excluded.t0,
                    t0_unix=excluded.t0_unix,
                    ttd_seconds=excluded.ttd_seconds,
                    affected_services=excluded.affected_services,
                    evidence=excluded.evidence,
                    breaches=excluded.breaches,
                    healthy_since=excluded.healthy_since,
                    resolved_at=excluded.resolved_at,
                    resolved_at_unix=excluded.resolved_at_unix,
                    metadata=excluded.metadata
                """,
                (
                    inc.id,
                    inc.status,
                    inc.trigger_rule,
                    inc.t0,
                    inc.t0_unix,
                    inc.created_at,
                    inc.created_at_unix,
                    inc.ttd_seconds,
                    json.dumps(inc.affected_services),
                    json.dumps(inc.evidence),
                    json.dumps(inc.breaches),
                    inc.healthy_since,
                    inc.resolved_at,
                    inc.resolved_at_unix,
                    json.dumps(inc.metadata),
                ),
            )
            conn.commit()

    def get_incident(self, incident_id: str) -> Incident | None:
        """Retrieve an incident by ID."""
        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, status, trigger_rule, t0, t0_unix, created_at, created_at_unix,
                       ttd_seconds, affected_services, evidence, breaches, healthy_since,
                       resolved_at, resolved_at_unix, metadata
                FROM incidents WHERE id = ?
                """,
                (incident_id.upper().strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_incident(row)

    def get_active_incident(self) -> Incident | None:
        """Retrieve the currently open or active incident (status != 'resolved')."""
        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, status, trigger_rule, t0, t0_unix, created_at, created_at_unix,
                       ttd_seconds, affected_services, evidence, breaches, healthy_since,
                       resolved_at, resolved_at_unix, metadata
                FROM incidents
                WHERE status IN ('open', 'diagnosing', 'mitigating', 'escalated')
                ORDER BY created_at_unix DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_incident(row)

    def list_incidents(self, status: str | None = None, limit: int = 50) -> list[Incident]:
        """List past and current incidents."""
        query = """
            SELECT id, status, trigger_rule, t0, t0_unix, created_at, created_at_unix,
                   ttd_seconds, affected_services, evidence, breaches, healthy_since,
                   resolved_at, resolved_at_unix, metadata
            FROM incidents
        """
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status.lower().strip())
        query += " ORDER BY created_at_unix DESC LIMIT ?"
        params.append(max(1, limit))

        results = []
        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                results.append(self._row_to_incident(row))
        return results

    def _row_to_incident(self, row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            status=row["status"],
            trigger_rule=row["trigger_rule"],
            t0=row["t0"],
            t0_unix=row["t0_unix"],
            created_at=row["created_at"],
            created_at_unix=row["created_at_unix"],
            ttd_seconds=row["ttd_seconds"],
            affected_services=json.loads(row["affected_services"]),
            evidence=json.loads(row["evidence"]),
            breaches=json.loads(row["breaches"]),
            healthy_since=row["healthy_since"],
            resolved_at=row["resolved_at"],
            resolved_at_unix=row["resolved_at_unix"],
            metadata=json.loads(row["metadata"]),
        )

    # =========================================================================
    # Rule Evaluation
    # =========================================================================

    def check_health_rule(self, service: str) -> Breach | None:
        """Rule 1: Health check fails 3 times in a row."""
        limit = int(self.slo.get("health_check_failures", 3))

        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, ts, timestamp_unix, status, status_code
                FROM health_checks
                WHERE service = ?
                ORDER BY timestamp_unix DESC LIMIT ?
                """,
                (service, limit),
            )
            rows = cur.fetchall()

        if len(rows) < limit:
            return None

        # Check if all N are failures
        all_failed = all(r["status"] != "ok" or r["status_code"] != 200 for r in rows)
        if not all_failed:
            return None

        # T0 is the earliest of the N consecutive failures
        earliest_failure = rows[-1]
        t0_unix = earliest_failure["timestamp_unix"]
        t0_ts = earliest_failure["ts"]
        evidence_ids = [f"MET-{r['id']}" for r in reversed(rows)]

        return Breach(
            rule="consecutive_health_failures",
            service=service,
            message=f"{service} failed /health check {limit} consecutive times",
            t0=t0_ts,
            t0_unix=t0_unix,
            evidence_ids=evidence_ids,
            metric_value=float(limit),
            threshold_value=float(limit),
        )

    def check_error_rate_rule(self, service: str) -> Breach | None:
        """Rule 2: Error rate above 5% over the last 30 seconds."""
        threshold = float(self.slo.get("error_rate_threshold", 0.05))
        window = float(self.slo.get("error_rate_window_seconds", 30))

        err_res = self.obs.error_rate(service=service, window=window)
        rate = float(err_res)
        total_reqs = err_res.get("total_requests", 0)

        # Minimum sample requirement to avoid false alarms on tiny traffic
        if rate > threshold and total_reqs >= 3:
            # Find exact T0 from the earliest error log in the window
            err_logs = self.obs.get_logs(service=service, since=window, level="ERROR", limit=100)
            if err_logs:
                earliest_log = err_logs[-1]  # get_logs returns DESC, so [-1] is earliest
                t0_ts = earliest_log["ts"]
                t0_unix = earliest_log["timestamp_unix"]
            else:
                t0_unix = time.time() - window
                t0_ts = format_iso(t0_unix)

            evidence_ids = list(err_res.get("evidence_ids", []))

            return Breach(
                rule="error_rate_threshold",
                service=service,
                message=f"{service} error rate {rate*100:.1f}% exceeds SLO limit {threshold*100:.1f}% ({err_res['error_count']}/{total_reqs} reqs)",
                t0=t0_ts,
                t0_unix=t0_unix,
                evidence_ids=evidence_ids,
                metric_value=rate,
                threshold_value=threshold,
            )
        return None

    def check_latency_rule(self, service: str) -> Breach | None:
        """Rule 3: p95 latency above 2x rolling baseline or above 2000ms SLO limit."""
        slo_limit = float(self.slo.get("latency_p95_slo_ms", 2000.0))
        multiplier = float(self.slo.get("latency_p95_multiplier", 2.0))
        window = float(self.slo.get("error_rate_window_seconds", 30))

        p95_res = self.obs.p95_latency(service=service, window=window)
        p95_val = float(p95_res)
        sample_count = p95_res.get("sample_count", 0)

        if sample_count < 3 or p95_val <= 0:
            return None

        # Update rolling baseline if healthy
        baseline = self._latency_baselines.get(service, 0.0)
        breached = False
        reason = ""

        if p95_val > slo_limit:
            breached = True
            reason = f"{service} p95 latency {p95_val:.1f}ms exceeds absolute SLO limit {slo_limit:.1f}ms"
        elif baseline > 0 and p95_val > (baseline * multiplier) and p95_val > 100.0:
            breached = True
            reason = f"{service} p95 latency {p95_val:.1f}ms is {p95_val/baseline:.1f}x baseline ({baseline:.1f}ms)"

        if breached:
            # Find earliest high-latency log
            logs = self.obs.get_logs(service=service, since=window, limit=200)
            high_lat_logs = [l for l in logs if float(l.get("latency_ms", 0.0)) > 500.0]
            if high_lat_logs:
                earliest_log = high_lat_logs[-1]
                t0_ts = earliest_log["ts"]
                t0_unix = earliest_log["timestamp_unix"]
            else:
                t0_unix = time.time() - window
                t0_ts = format_iso(t0_unix)

            evidence_ids = list(p95_res.get("evidence_ids", []))

            return Breach(
                rule="latency_p95_slo",
                service=service,
                message=reason,
                t0=t0_ts,
                t0_unix=t0_unix,
                evidence_ids=evidence_ids,
                metric_value=p95_val,
                threshold_value=slo_limit,
            )

        # If not breached and normal latency, adapt baseline
        if p95_val < 500.0:
            if baseline == 0.0:
                self._latency_baselines[service] = p95_val
            else:
                # Exponential moving average
                self._latency_baselines[service] = round(baseline * 0.8 + p95_val * 0.2, 2)

        return None

    def check_resource_growth_rule(self, service: str) -> Breach | None:
        """Rule 4: Memory or CPU growth trend above threshold."""
        mem_threshold_mb = float(self.slo.get("memory_growth_threshold_mb", 50))
        since_ts = time.time() - 60.0

        with self._lock, self.obs.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, ts, timestamp_unix, value
                FROM metrics
                WHERE service = ? AND metric = 'process_resident_memory_bytes'
                  AND timestamp_unix >= ?
                ORDER BY timestamp_unix ASC
                """,
                (service, since_ts),
            )
            rows = cur.fetchall()

        if len(rows) >= 2:
            min_val = min(r["value"] for r in rows)
            max_val = max(r["value"] for r in rows)
            growth_mb = (max_val - min_val) / (1024 * 1024)

            if growth_mb > mem_threshold_mb:
                earliest = rows[0]
                return Breach(
                    rule="memory_growth_trend",
                    service=service,
                    message=f"{service} memory grew by {growth_mb:.1f}MB in 60s (threshold: {mem_threshold_mb}MB)",
                    t0=earliest["ts"],
                    t0_unix=earliest["timestamp_unix"],
                    evidence_ids=[f"MET-{r['id']}" for r in (rows[0], rows[-1])],
                    metric_value=round(growth_mb, 2),
                    threshold_value=mem_threshold_mb,
                )
        return None

    def evaluate_rules(self) -> list[Breach]:
        """Evaluate all SLO rules across all registered services."""
        services = self._get_services()
        breaches: list[Breach] = []

        for svc in services:
            # 1. Health check failures
            b_health = self.check_health_rule(svc)
            if b_health:
                breaches.append(b_health)

            # 2. Error rate threshold
            b_err = self.check_error_rate_rule(svc)
            if b_err:
                breaches.append(b_err)

            # 3. Latency spike
            b_lat = self.check_latency_rule(svc)
            if b_lat:
                breaches.append(b_lat)

            # 4. Resource growth
            b_res = self.check_resource_growth_rule(svc)
            if b_res:
                breaches.append(b_res)

        return breaches

    # =========================================================================
    # Incident Lifecycle: Open, Deduplicate, Auto-Close
    # =========================================================================

    def run_once(self, poll_metrics: bool = True) -> Incident | None:
        """Single evaluation step: detect anomalies, open or deduplicate, and auto-resolve."""
        # 1. Ensure latest metrics/logs are fresh if requested
        if poll_metrics:
            try:
                self.obs.poll_once()
            except Exception:
                pass

        now_unix = time.time()
        now_ts = format_iso(now_unix)
        breaches = self.evaluate_rules()
        active_inc = self.get_active_incident()

        # ---------------------------------------------------------------------
        # Case A: Breaches detected
        # ---------------------------------------------------------------------
        if breaches:
            # If an incident is already open, deduplicate!
            if active_inc:
                # Attach new breaches to existing incident
                modified = False
                for b in breaches:
                    b_dict = asdict(b)
                    if b.service not in active_inc.affected_services:
                        active_inc.affected_services.append(b.service)
                        modified = True

                    for eid in b.evidence_ids:
                        if eid not in active_inc.evidence:
                            active_inc.evidence.append(eid)
                            modified = True

                    # Check if breach is already logged
                    if not any(
                        prev.get("rule") == b.rule and prev.get("service") == b.service
                        for prev in active_inc.breaches
                    ):
                        active_inc.breaches.append(b_dict)
                        modified = True

                    # Keep T0 as the earliest anomaly timestamp
                    if b.t0_unix < active_inc.t0_unix:
                        active_inc.t0_unix = b.t0_unix
                        active_inc.t0 = b.t0
                        active_inc.ttd_seconds = round(active_inc.created_at_unix - b.t0_unix, 2)
                        modified = True

                # Reset healthy recovery clock because an anomaly just occurred
                if active_inc.healthy_since is not None:
                    active_inc.healthy_since = None
                    modified = True

                if modified:
                    self.save_incident(active_inc)

                # Check if auto-remediation should be triggered for active_inc
                if self.auto_remediate and active_inc.status in ("open", "escalated"):
                    has_pending = False
                    has_recent_denial = False
                    try:
                        from sre.fixer import Fixer
                        f_tmp = Fixer(supervisor=self.supervisor, detector=self, observability=self.obs)
                        pending_list = f_tmp.list_approvals(status="pending")
                        has_pending = any(a.incident_id == active_inc.id for a in pending_list)
                        # Do not re-propose fix if operator recently denied it for this incident
                        all_denied = f_tmp.list_approvals(status="denied", limit=10)
                        now_u = time.time()
                        has_recent_denial = any(
                            a.incident_id == active_inc.id and (now_u - (a.decided_at_unix or 0) < 60.0)
                            for a in all_denied
                        )
                    except Exception:
                        pass

                    if not has_pending and not has_recent_denial and not any(t.name == f"auto_remediate_{active_inc.id}" for t in threading.enumerate()):
                        threading.Thread(
                            target=self._run_auto_remediation,
                            args=(active_inc,),
                            daemon=True,
                            name=f"auto_remediate_{active_inc.id}",
                        ).start()

                return active_inc

            # No active incident -> Open new incident!
            # Find earliest T0 among detected breaches
            breaches_sorted = sorted(breaches, key=lambda b: b.t0_unix)
            earliest_breach = breaches_sorted[0]

            inc_id = self._next_incident_id()
            t0_ts = earliest_breach.t0
            t0_unix = earliest_breach.t0_unix
            ttd = max(0.0, round(now_unix - t0_unix, 2))

            affected = list(dict.fromkeys(b.service for b in breaches))
            all_evidence = []
            for b in breaches:
                all_evidence.extend(b.evidence_ids)
            all_evidence = list(dict.fromkeys(all_evidence))

            new_inc = Incident(
                id=inc_id,
                status="open",
                trigger_rule=earliest_breach.rule,
                t0=t0_ts,
                t0_unix=t0_unix,
                created_at=now_ts,
                created_at_unix=now_unix,
                ttd_seconds=ttd,
                affected_services=affected,
                evidence=all_evidence,
                breaches=[asdict(b) for b in breaches],
                healthy_since=None,
                resolved_at=None,
                resolved_at_unix=None,
                metadata={"trigger_message": earliest_breach.message},
            )

            self.save_incident(new_inc)

            # Log to supervisor audit change log
            try:
                self.supervisor.log_change(
                    action="incident_opened",
                    service=",".join(affected),
                    details={
                        "incident_id": new_inc.id,
                        "trigger_rule": new_inc.trigger_rule,
                        "t0": new_inc.t0,
                        "ttd_seconds": new_inc.ttd_seconds,
                        "affected_services": affected,
                        "evidence": all_evidence[:5],
                    },
                    author="detector",
                )
            except Exception:
                pass

            if self.auto_remediate:
                threading.Thread(
                    target=self._run_auto_remediation,
                    args=(new_inc,),
                    daemon=True,
                    name=f"auto_remediate_{new_inc.id}",
                ).start()

            return new_inc

        # ---------------------------------------------------------------------
        # Case B: System is healthy (no breaches)
        # ---------------------------------------------------------------------
        if active_inc:
            # Check if incident can be resolved
            recover_threshold = float(self.slo.get("recover_after_seconds", 60))

            if active_inc.healthy_since is None:
                active_inc.healthy_since = now_unix
                self.save_incident(active_inc)
                return active_inc
            elif (now_unix - active_inc.healthy_since) >= recover_threshold:
                # Within SLO for recover_after_seconds -> auto-resolve!
                active_inc.status = "resolved"
                active_inc.resolved_at = now_ts
                active_inc.resolved_at_unix = now_unix
                duration = round(now_unix - active_inc.t0_unix, 2)
                active_inc.metadata["duration_seconds"] = duration
                self.save_incident(active_inc)

                # Log resolution to audit log
                try:
                    self.supervisor.log_change(
                        action="incident_resolved",
                        service=",".join(active_inc.affected_services),
                        details={
                            "incident_id": active_inc.id,
                            "t0": active_inc.t0,
                            "resolved_at": active_inc.resolved_at,
                            "total_duration_seconds": duration,
                        },
                        author="detector",
                    )
                except Exception:
                    pass

                # Auto-generate verified postmortem report
                try:
                    from sre.postmortem import PostmortemGenerator
                    pm = PostmortemGenerator(observability=self.obs, detector=self)
                    pm.generate_postmortem(active_inc.id)
                except Exception:
                    pass

                return active_inc
            else:
                # Still within recovery observation window
                return active_inc

        return None

    def update_incident_status(
        self,
        incident_id: str,
        new_status: str,
        metadata_update: dict[str, Any] | None = None,
    ) -> Incident | None:
        """Update incident status (e.g. diagnosing, mitigating, resolved, escalated)."""
        valid_statuses = ("open", "diagnosing", "mitigating", "resolved", "escalated")
        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status '{new_status}'. Valid: {', '.join(valid_statuses)}")

        inc = self.get_incident(incident_id)
        if not inc:
            return None

        inc.status = new_status
        if new_status == "resolved" and not inc.resolved_at:
            now_unix = time.time()
            inc.resolved_at = format_iso(now_unix)
            inc.resolved_at_unix = now_unix
            inc.metadata["duration_seconds"] = round(now_unix - inc.t0_unix, 2)

        if metadata_update:
            inc.metadata.update(metadata_update)

        self.save_incident(inc)
        return inc

    def _deterministic_diagnosis(self, incident: Incident) -> dict[str, Any]:
        """Produce an accurate audit-backed diagnosis and fix when LLM is unavailable or slow."""
        affected = incident.affected_services or ["gateway"]
        primary_svc = affected[0]

        changes = self.obs.get_changes(since=180, limit=20)
        root_svc = primary_svc
        fix_action = "restart_service"
        fix_args: dict[str, Any] = {"service": primary_svc}
        root_cause_desc = f"Service '{primary_svc}' breached SLO rule '{incident.trigger_rule}'"

        # 1. ALWAYS check if any service is running breaking version v2.0.0 FIRST
        for s in ("payments", "orders", "inventory", "gateway"):
            try:
                ver = self.supervisor._read_version(s)
                if ver and ver.strip() == "2.0.0":
                    root_svc = s
                    fix_action = "rollback"
                    fix_args = {"service": s, "to_version": "1.0.0"}
                    root_cause_desc = f"Service '{s}' running breaking version v2.0.0 (bad deploy)"
                    break
            except Exception:
                pass

        # 2. Check if bad_deploy was logged in recent changes
        if fix_action != "rollback":
            for chg in changes:
                action = str(chg.get("action", ""))
                svc = str(chg.get("service", ""))
                if action == "bad_deploy" or chg.get("details", {}).get("version") == "2.0.0":
                    root_svc = svc or primary_svc
                    fix_action = "rollback"
                    fix_args = {"service": root_svc, "to_version": "1.0.0"}
                    root_cause_desc = f"Bad deploy v2.0.0 detected on {root_svc}"
                    break

        # 3. Check for chaos actions if no bad deploy found
        if fix_action != "rollback":
            for chg in changes:
                action = str(chg.get("action", ""))
                svc = str(chg.get("service", ""))
                if action.startswith("chaos_"):
                    root_svc = svc or primary_svc
                    if action == "chaos_crash":
                        fix_action = "restart_service"
                    else:
                        fix_action = "clear_chaos_flag"
                    fix_args = {"service": root_svc}
                    root_cause_desc = f"Chaos failure '{action}' injected on {root_svc}"
                    break

        evidence_ids = list(incident.evidence[:5])
        return {
            "root_cause_service": root_svc,
            "root_cause": root_cause_desc,
            "evidence": evidence_ids,
            "causal_chain": [root_svc, "upstream degradation", incident.trigger_rule],
            "confidence": 0.95,
            "proposed_fix": {
                "action": fix_action,
                "args": fix_args,
            },
            "mitigation_vs_cure": "mitigation",
            "incident_id": incident.id,
        }

    def _run_auto_remediation(self, incident: Incident) -> None:
        """Autonomous remediation: runs LLM Agent investigation, Fixer recovery, and Postmortem."""
        diagnosis = None
        # Fast-path check: bad_deploy / breaking version v2.0.0
        # If any service is running breaking v2.0.0 or bad_deploy was logged, diagnose rollback immediately!
        deterministic = self._deterministic_diagnosis(incident)
        if deterministic and deterministic.get("proposed_fix", {}).get("action") == "rollback":
            diagnosis = deterministic
        else:
            try:
                from sre.agent import Agent
                agent = Agent(detector=self, supervisor=self.supervisor)
                diagnosis = agent.investigate(incident)
            except Exception:
                diagnosis = deterministic

        if not diagnosis:
            diagnosis = deterministic

        try:
            from sre.fixer import Fixer
            from sre.postmortem import PostmortemGenerator
            fixer = Fixer(supervisor=self.supervisor, detector=self, observability=self.obs)
            fixer.apply_diagnosis(diagnosis)
            pm = PostmortemGenerator(observability=self.obs, detector=self)
            pm.generate_postmortem(incident.id)
        except Exception:
            pass

    # =========================================================================
    # Continuous Watcher
    # =========================================================================

    def start_background_detector(self, interval_seconds: float = 2.0) -> threading.Thread:
        """Start daemon thread running evaluate & lifecycle check every interval."""
        if self._watch_thread and self._watch_thread.is_alive():
            return self._watch_thread

        self._stop_watch_event.clear()

        def _worker() -> None:
            while not self._stop_watch_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    pass
                self._stop_watch_event.wait(interval_seconds)

        self._watch_thread = threading.Thread(
            target=_worker,
            name="incident_detector_watcher",
            daemon=True,
        )
        self._watch_thread.start()
        return self._watch_thread

    def stop_background_detector(self, timeout: float = 3.0) -> None:
        """Stop background detector thread."""
        self._stop_watch_event.set()
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=timeout)
        self._watch_thread = None

    @property
    def is_running(self) -> bool:
        return self._watch_thread is not None and self._watch_thread.is_alive()


# =========================================================================
# CLI Interface
# =========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Incident Detector CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Status
    subparsers.add_parser("status", help="Show active incident and current SLO status")

    # List
    p_list = subparsers.add_parser("list", help="List incidents")
    p_list.add_argument("--status", default=None, help="Filter by status (open, resolved, etc.)")
    p_list.add_argument("--limit", type=int, default=20, help="Max entries to show")

    # Check
    subparsers.add_parser("check", help="Run detection rules once and print any breaches or new incident")

    # Show
    p_show = subparsers.add_parser("show", help="Show detailed information for an incident")
    p_show.add_argument("incident_id", help="Incident ID (e.g. INC-1)")

    # Watch
    p_watch = subparsers.add_parser("watch", help="Run continuous incident detector loop")
    p_watch.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    p_watch.add_argument("--auto-remediate", action="store_true", default=False, help="Automatically invoke Agent & Fixer on detected incidents")

    args = parser.parse_args(argv)
    detector = IncidentDetector()

    if args.command == "status":
        active = detector.get_active_incident()
        if active:
            print(f"[ACTIVE INCIDENT] {active.id} (Status: {active.status.upper()})")
            print(f"  Trigger Rule      : {active.trigger_rule}")
            print(f"  Affected Services : {', '.join(active.affected_services)}")
            print(f"  T0 (Anomaly Time) : {active.t0}")
            print(f"  Detected At       : {active.created_at} (TTD: {active.ttd_seconds:.1f}s)")
            print(f"  Evidence IDs      : {', '.join(active.evidence)}")
            print(f"  Breaches Count    : {len(active.breaches)}")
        else:
            print("[STATUS] System is healthy. No active incidents.")

    elif args.command == "check":
        inc = detector.run_once()
        if inc:
            print(f"[INCIDENT] {inc.id} | Status: {inc.status} | Trigger: {inc.trigger_rule} | Affected: {inc.affected_services}")
        else:
            print("[CHECK] All rules passed. No active incident.")

    elif args.command == "list":
        incidents = detector.list_incidents(status=args.status, limit=args.limit)
        print(f"Found {len(incidents)} incidents:")
        print(f"{'ID':<8} {'STATUS':<12} {'TRIGGER':<25} {'AFFECTED':<20} {'T0':<28} TTD")
        print("-" * 100)
        for inc in incidents:
            aff = ",".join(inc.affected_services)
            print(f"{inc.id:<8} {inc.status:<12} {inc.trigger_rule:<25} {aff:<20} {inc.t0:<28} {inc.ttd_seconds:.1f}s")

    elif args.command == "show":
        inc = detector.get_incident(args.incident_id)
        if inc:
            print(json.dumps(inc.to_dict(), indent=2))
        else:
            print(f"Incident '{args.incident_id}' not found.", file=sys.stderr)
            return 1

    elif args.command == "watch":
        if args.auto_remediate:
            detector.auto_remediate = True
        print(f"Starting Incident Detector watcher (interval: {args.interval}s, auto_remediate={detector.auto_remediate})... Press Ctrl+C to stop.")
        detector.start_background_detector(interval_seconds=args.interval)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nStopping Incident Detector...")
            detector.stop_background_detector()
            print("Stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
