"""Tiered fix pipeline and human approval flow for AI-SRE.

Pipeline (enforced in code, not in the LLM prompt):
    diagnosis -> tier check -> circuit breaker -> snapshot -> apply -> verify
                                                     -> resolve | escalate

Tiers:
  auto-fix : restart_service, clear_chaos_flag, rotate_logs
  ask-first : rollback, scale_up, change_config
  never-do  : delete_data, drop_db, edits outside the app folder
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "observability.db"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.agent import Diagnosis
from sre.chaos import ChaosEngine
from sre.detector import Incident, IncidentDetector
from sre.observability import Observability, format_iso, get_observability
from sre.supervisor import Supervisor

# ---------------------------------------------------------------------------
# Permission tiers (code-enforced)
# ---------------------------------------------------------------------------

AUTO_FIX_ACTIONS = frozenset({"restart_service", "clear_chaos_flag", "rotate_logs"})
ASK_FIRST_ACTIONS = frozenset({"rollback", "scale_up", "change_config"})
NEVER_DO_ACTIONS = frozenset(
    {
        "delete_data",
        "drop_db",
        "drop_database",
        "rm_rf",
        "format_disk",
        "wipe_data",
        "delete_database",
    }
)

KNOWN_SERVICES = ("gateway", "orders", "payments", "inventory")

CIRCUIT_MAX_RESTARTS = 2
CIRCUIT_WINDOW_SECONDS = 600  # 10 minutes
DEFAULT_VERIFY_TIMEOUT = 30.0
DEFAULT_APPROVAL_TIMEOUT = 120.0

NEVER_DO_PATTERNS = (
    re.compile(r"\bdrop\s+(table|database|db)\b", re.I),
    re.compile(r"\bdelete\s+(from|data|all)\b", re.I),
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\btruncate\s+table\b", re.I),
)


@dataclass
class Snapshot:
    """Pre-fix health/metrics snapshot for audit and undo context."""

    ts: str
    ts_unix: float
    service: str
    health: dict[str, Any]
    version: str
    error_rate: float | None = None
    p95_latency_ms: float | None = None
    supervisor_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Approval:
    """Pending or resolved human-approval record for ask-first actions."""

    id: str
    incident_id: str
    action: str
    args: dict[str, Any]
    status: str  # pending, approved, denied, timed_out, cancelled
    created_at: str
    created_at_unix: float
    reason: str = ""
    decided_at: str | None = None
    decided_at_unix: float | None = None
    decided_by: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FixResult:
    """Outcome of a fix pipeline run."""

    status: str  # applied, verified, failed, blocked, pending_approval, escalated, denied
    tier: str
    action: str
    args: dict[str, Any]
    incident_id: str | None = None
    service: str | None = None
    snapshot: dict[str, Any] | None = None
    apply_result: dict[str, Any] | None = None
    verify_result: dict[str, Any] | None = None
    approval_id: str | None = None
    change_ids: list[str] = field(default_factory=list)
    message: str = ""
    t2: str | None = None  # time fix applied / verified
    t2_unix: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FixerError(RuntimeError):
    """Raised for unrecoverable fix-pipeline errors."""


class Fixer:
    """Applies safe fixes, gates risky ones, and blocks forbidden ones."""

    def __init__(
        self,
        supervisor: Supervisor | None = None,
        chaos: ChaosEngine | None = None,
        observability: Observability | None = None,
        detector: IncidentDetector | None = None,
        db_path: Path | str | None = None,
        repo_root: Path | str | None = None,
        verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        circuit_max: int = CIRCUIT_MAX_RESTARTS,
        circuit_window: float = CIRCUIT_WINDOW_SECONDS,
    ) -> None:
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.supervisor = supervisor or Supervisor(repo_root=self.repo_root)
        self.chaos = chaos or ChaosEngine(supervisor=self.supervisor)
        self.obs = observability or get_observability()
        self.detector = detector
        self.db_path = Path(db_path or getattr(self.obs, "db_path", DEFAULT_DB_PATH))
        self.verify_timeout = float(verify_timeout)
        self.approval_timeout = float(approval_timeout)
        self.circuit_max = int(circuit_max)
        self.circuit_window = float(circuit_window)

        self._lock = threading.Lock()
        # service -> list of restart unix timestamps
        self._restart_times: dict[str, list[float]] = {}
        self._init_db()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    args TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_unix REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    decided_at TEXT,
                    decided_at_unix REAL,
                    decided_by TEXT,
                    result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
                CREATE INDEX IF NOT EXISTS idx_approvals_incident ON approvals(incident_id);
                """
            )

    def _next_approval_id(self) -> str:
        with self._conn() as conn:
            cur = conn.execute("SELECT COUNT(*) AS n FROM approvals")
            n = int(cur.fetchone()["n"]) + 1
        return f"APP-{n}"

    def _save_approval(self, approval: Approval) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    id, incident_id, action, args, status, created_at, created_at_unix,
                    reason, decided_at, decided_at_unix, decided_by, result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    decided_at=excluded.decided_at,
                    decided_at_unix=excluded.decided_at_unix,
                    decided_by=excluded.decided_by,
                    result=excluded.result,
                    reason=excluded.reason
                """,
                (
                    approval.id,
                    approval.incident_id,
                    approval.action,
                    json.dumps(approval.args),
                    approval.status,
                    approval.created_at,
                    approval.created_at_unix,
                    approval.reason,
                    approval.decided_at,
                    approval.decided_at_unix,
                    approval.decided_by,
                    json.dumps(approval.result),
                ),
            )

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_approval(row)

    def list_approvals(self, status: str | None = "pending", limit: int = 50) -> list[Approval]:
        query = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at_unix DESC LIMIT ?"
        params.append(max(1, limit))
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_approval(r) for r in rows]

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> Approval:
        return Approval(
            id=row["id"],
            incident_id=row["incident_id"],
            action=row["action"],
            args=json.loads(row["args"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
            created_at_unix=float(row["created_at_unix"]),
            reason=row["reason"] or "",
            decided_at=row["decided_at"],
            decided_at_unix=row["decided_at_unix"],
            decided_by=row["decided_by"],
            result=json.loads(row["result"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Tier classification
    # ------------------------------------------------------------------

    @classmethod
    def classify_tier(cls, action: str) -> str:
        """Return auto_fix | ask_first | never_do | unknown."""
        name = (action or "").strip().lower()
        if name in AUTO_FIX_ACTIONS:
            return "auto_fix"
        if name in ASK_FIRST_ACTIONS:
            return "ask_first"
        if name in NEVER_DO_ACTIONS or cls._looks_never_do(name):
            return "never_do"
        return "unknown"

    @staticmethod
    def _looks_never_do(action: str) -> bool:
        for pat in NEVER_DO_PATTERNS:
            if pat.search(action):
                return True
        return False

    def _validate_service(self, service: str | None) -> str:
        if not service:
            raise FixerError("proposed_fix.args.service is required")
        clean = service.lower().strip()
        try:
            self.supervisor.validate_service(clean)
        except Exception as exc:
            raise FixerError(str(exc)) from exc
        return clean

    def _validate_path_inside_app(self, path: str | Path) -> Path:
        """Reject edits outside the repository (never-do)."""
        target = Path(path).expanduser().resolve()
        root = self.repo_root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise FixerError(
                f"Blocked: path '{path}' is outside the application folder"
            ) from exc
        return target

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _prune_restarts(self, service: str, now: float | None = None) -> list[float]:
        now = now if now is not None else time.time()
        times = [t for t in self._restart_times.get(service, []) if now - t <= self.circuit_window]
        self._restart_times[service] = times
        return times

    def restart_count(self, service: str) -> int:
        return len(self._prune_restarts(service))

    def circuit_open(self, service: str) -> bool:
        return self.restart_count(service) >= self.circuit_max

    def _record_restart(self, service: str) -> None:
        times = self._prune_restarts(service)
        times.append(time.time())
        self._restart_times[service] = times

    # ------------------------------------------------------------------
    # Snapshot / verify
    # ------------------------------------------------------------------

    def snapshot(self, service: str) -> Snapshot:
        """Capture health + derived metrics before mutating anything."""
        svc = self._validate_service(service)
        now = time.time()
        health: dict[str, Any] = {}
        error_rate = None
        p95 = None
        try:
            health = self.obs.get_health(svc)
        except Exception as exc:  # noqa: BLE001
            health = {"error": str(exc), "healthy": False}
        try:
            er = self.obs.error_rate(svc, window=30)
            error_rate = float(er)
        except Exception:
            pass
        try:
            lat = self.obs.p95_latency(svc, window=60)
            p95 = float(lat)
        except Exception:
            pass

        st = self.supervisor.status().get(svc, {})
        version = st.get("version") or self.supervisor._read_version(svc)
        return Snapshot(
            ts=format_iso(now),
            ts_unix=now,
            service=svc,
            health=health,
            version=str(version),
            error_rate=error_rate,
            p95_latency_ms=p95,
            supervisor_status=dict(st),
        )

    def verify_recovery(
        self,
        service: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        """Poll health / error rate / p95 until healthy or timeout."""
        svc = self._validate_service(service)
        timeout = self.verify_timeout if timeout is None else float(timeout)
        deadline = time.time() + max(0.5, timeout)
        attempts: list[dict[str, Any]] = []
        slo = getattr(self.obs, "_load_slo", lambda: {})()
        err_threshold = float(slo.get("error_rate_threshold", 0.05))
        p95_limit = float(slo.get("latency_p95_slo_ms", 2000))

        while time.time() < deadline:
            try:
                self.obs.poll_service(svc, self.supervisor.registry[svc]["port"], timeout=0.8)
            except Exception:
                pass

            healthy = False
            health: dict[str, Any] = {}
            try:
                health = self.obs.get_health(svc)
                healthy = bool(health.get("healthy"))
            except Exception as exc:  # noqa: BLE001
                health = {"error": str(exc), "healthy": False}

            # Prefer supervisor health as ground truth for process liveness
            st = self.supervisor.status().get(svc, {})
            if st.get("healthy"):
                healthy = True

            err_rate = None
            p95 = None
            try:
                err_rate = float(self.obs.error_rate(svc, window=15))
            except Exception:
                pass
            try:
                p95 = float(self.obs.p95_latency(svc, window=30))
            except Exception:
                pass

            err_ok = err_rate is None or err_rate <= err_threshold
            # If there is no traffic yet after restart, treat latency as OK
            p95_ok = p95 is None or p95 <= p95_limit

            sample = {
                "ts": format_iso(),
                "healthy": healthy,
                "error_rate": err_rate,
                "p95_latency_ms": p95,
                "error_ok": err_ok,
                "p95_ok": p95_ok,
                "supervisor": {"status": st.get("status"), "pid": st.get("pid")},
            }
            attempts.append(sample)

            if healthy and err_ok and p95_ok:
                return {
                    "recovered": True,
                    "service": svc,
                    "attempts": attempts,
                    "final": sample,
                }

            time.sleep(poll_interval)

        return {
            "recovered": False,
            "service": svc,
            "attempts": attempts,
            "final": attempts[-1] if attempts else {},
            "message": f"Recovery not verified within {timeout}s",
        }

    # ------------------------------------------------------------------
    # Apply individual actions
    # ------------------------------------------------------------------

    def _apply_action(self, action: str, args: dict[str, Any], *, author: str) -> dict[str, Any]:
        service = self._validate_service(args.get("service"))

        if action == "restart_service":
            if self.circuit_open(service):
                raise FixerError(
                    f"Circuit breaker open for '{service}': "
                    f"{self.circuit_max} restarts in {int(self.circuit_window)}s"
                )
            st = self.supervisor.status().get(service, {})
            if st.get("status") in ("stopped", "crashed") or not st.get("pid"):
                info = self.supervisor.start_service(service)
            else:
                info = self.supervisor.restart_service(service)
            self._record_restart(service)
            change = self.supervisor.log_change(
                action="fix_restart_service",
                service=service,
                author=author,
                details={"pid": info.pid, "status": info.status, "version": info.version},
            )
            return {"service": service, "info": asdict(info), "change_id": change["id"]}

        if action == "clear_chaos_flag":
            entry = self.chaos.clear_service(service)
            change = self.supervisor.log_change(
                action="fix_clear_chaos_flag",
                service=service,
                author=author,
                details={"chaos_clear": entry},
            )
            return {"service": service, "chaos": entry, "change_id": change["id"]}

        if action == "rotate_logs":
            logs_dir = self.repo_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{service}.log"
            rotated_to = None
            if log_path.is_file():
                stamp = time.strftime("%Y%m%dT%H%M%S")
                rotated_to = logs_dir / f"{service}.log.{stamp}"
                log_path.replace(rotated_to)
            log_path.write_text("", encoding="utf-8")
            change = self.supervisor.log_change(
                action="fix_rotate_logs",
                service=service,
                author=author,
                details={"rotated_to": str(rotated_to) if rotated_to else None},
            )
            return {
                "service": service,
                "rotated_to": str(rotated_to) if rotated_to else None,
                "change_id": change["id"],
            }

        if action == "rollback":
            # Rollback is the recovery path after a circuit trip — always allowed.
            ver_file = self.repo_root / "services" / service / "VERSION"
            current = self.supervisor._read_version(service)
            target = str(args.get("to_version") or args.get("version") or "1.0.0").strip()
            if not target:
                target = "1.0.0"
            ver_file.parent.mkdir(parents=True, exist_ok=True)
            ver_file.write_text(target + "\n", encoding="utf-8")
            info = self.supervisor.restart_service(service)
            # Successful rollback resets the restart window for this service.
            self._restart_times[service] = []
            change = self.supervisor.log_change(
                action="fix_rollback",
                service=service,
                author=author,
                details={
                    "from_version": current,
                    "to_version": target,
                    "pid": info.pid,
                    "status": info.status,
                    "circuit_reset": True,
                },
            )
            return {
                "service": service,
                "from_version": current,
                "to_version": target,
                "info": asdict(info),
                "change_id": change["id"],
            }

        if action == "scale_up":
            # Local demo has no real horizontal scaling; ensure service is healthy
            # and record the approved intent for the audit trail.
            st = self.supervisor.status().get(service, {})
            if not st.get("healthy"):
                if self.circuit_open(service):
                    raise FixerError(f"Circuit breaker open for '{service}'")
                info = self.supervisor.start_service(service)
                self._record_restart(service)
            else:
                info = self.supervisor.state.get(service)
            replicas = int(args.get("replicas") or args.get("count") or 2)
            change = self.supervisor.log_change(
                action="fix_scale_up",
                service=service,
                author=author,
                details={
                    "requested_replicas": replicas,
                    "note": "local demo records scale intent; process kept single-instance",
                    "pid": getattr(info, "pid", None),
                },
            )
            return {
                "service": service,
                "requested_replicas": replicas,
                "change_id": change["id"],
            }

        if action == "change_config":
            key = str(args.get("key") or "").strip()
            value = args.get("value")
            if not key:
                raise FixerError("change_config requires args.key")
            # Only allow writes under data/runtime_config.json inside the repo
            cfg_path = self._validate_path_inside_app(
                self.repo_root / "data" / "runtime_config.json"
            )
            cfg: dict[str, Any] = {}
            if cfg_path.is_file():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    cfg = {}
            svc_cfg = dict(cfg.get(service) or {})
            svc_cfg[key] = value
            cfg[service] = svc_cfg
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            change = self.supervisor.log_change(
                action="fix_change_config",
                service=service,
                author=author,
                details={"key": key, "value": value, "path": str(cfg_path)},
            )
            return {"service": service, "key": key, "value": value, "change_id": change["id"]}

        raise FixerError(f"No applicator for action '{action}'")

    # ------------------------------------------------------------------
    # Public pipeline
    # ------------------------------------------------------------------

    def apply_fix(
        self,
        proposed_fix: dict[str, Any] | None,
        *,
        incident: Incident | dict[str, Any] | str | None = None,
        diagnosis: Diagnosis | dict[str, Any] | None = None,
        wait_for_approval: bool = False,
        approval_timeout: float | None = None,
        approved_by: str | None = None,
        skip_verify: bool = False,
    ) -> FixResult:
        """Run the full fix pipeline for a proposed_fix dict."""
        fix = self._normalize_proposed_fix(proposed_fix, diagnosis)
        action = fix["action"]
        args = fix["args"]
        tier = self.classify_tier(action)
        incident_obj = self._resolve_incident(incident, diagnosis)
        incident_id = incident_obj.id if incident_obj else (
            (diagnosis.incident_id if isinstance(diagnosis, Diagnosis) else None)
            or (diagnosis.get("incident_id") if isinstance(diagnosis, dict) else None)
        )

        # --- never-do ---
        if tier == "never_do":
            return self._block_never_do(action, args, incident_id)

        if tier == "unknown":
            return self._block_never_do(
                action,
                args,
                incident_id,
                message=f"Unknown action '{action}' is not allowlisted — blocked",
            )

        # Path-safety for any path-bearing args
        for path_key in ("path", "file", "target"):
            if path_key in args and args[path_key]:
                try:
                    self._validate_path_inside_app(args[path_key])
                except FixerError as exc:
                    return self._block_never_do(
                        action, args, incident_id, message=str(exc)
                    )

        service = args.get("service")
        if service:
            try:
                service = self._validate_service(service)
                args["service"] = service
            except FixerError as exc:
                return FixResult(
                    status="failed",
                    tier=tier,
                    action=action,
                    args=args,
                    incident_id=incident_id,
                    message=str(exc),
                )

        # --- ask-first (unless already approved) ---
        if tier == "ask_first" and not approved_by:
            approval = self._create_approval(
                incident_id=incident_id or "unknown",
                action=action,
                args=args,
                reason="ask_first tier requires human approval",
            )
            if incident_obj and self.detector:
                self.detector.update_incident_status(
                    incident_obj.id,
                    "mitigating",
                    metadata_update={
                        "pending_approval_id": approval.id,
                        "proposed_fix": fix,
                    },
                )
            result = FixResult(
                status="pending_approval",
                tier=tier,
                action=action,
                args=args,
                incident_id=incident_id,
                service=service,
                approval_id=approval.id,
                message=f"Awaiting human approval ({approval.id})",
            )
            if wait_for_approval:
                decided = self.wait_for_approval(
                    approval.id,
                    timeout=approval_timeout,
                )
                if decided.status == "approved":
                    return self.apply_fix(
                        {"action": action, "args": args},
                        incident=incident_obj or incident_id,
                        diagnosis=diagnosis,
                        approved_by=decided.decided_by or "human",
                        skip_verify=skip_verify,
                    )
                if decided.status == "denied":
                    return self._escalate(
                        FixResult(
                            status="denied",
                            tier=tier,
                            action=action,
                            args=args,
                            incident_id=incident_id,
                            service=service,
                            approval_id=approval.id,
                            message="Human denied the proposed fix",
                        ),
                        incident_obj,
                    )
                return self._escalate(
                    FixResult(
                        status="escalated",
                        tier=tier,
                        action=action,
                        args=args,
                        incident_id=incident_id,
                        service=service,
                        approval_id=approval.id,
                        message="Approval timed out — escalated",
                    ),
                    incident_obj,
                )
            return result

        # --- circuit breaker: block restart_service only; rollback remains the escape hatch ---
        if action == "restart_service" and service and self.circuit_open(service):
            msg = (
                f"Circuit breaker tripped for '{service}' "
                f"({self.circuit_max} restarts / {int(self.circuit_window)}s)"
            )
            rollback_approval = self._create_approval(
                incident_id=incident_id or "unknown",
                action="rollback",
                args={"service": service, "to_version": "1.0.0"},
                reason=msg + " — propose rollback",
            )
            blocked = FixResult(
                status="escalated",
                tier=tier,
                action=action,
                args=args,
                incident_id=incident_id,
                service=service,
                approval_id=rollback_approval.id,
                message=msg,
            )
            return self._escalate(blocked, incident_obj)

        # --- auto-fix or approved ask-first ---
        if incident_obj and self.detector:
            self.detector.update_incident_status(incident_obj.id, "mitigating")

        snap = self.snapshot(service) if service else None
        author = approved_by or ("agent" if tier == "auto_fix" else "human")

        try:
            apply_result = self._apply_action(action, args, author=author)
        except Exception as exc:  # noqa: BLE001
            failed = FixResult(
                status="failed",
                tier=tier,
                action=action,
                args=args,
                incident_id=incident_id,
                service=service,
                snapshot=snap.to_dict() if snap else None,
                message=f"Apply failed: {exc}",
            )
            change = self.supervisor.log_change(
                action="fix_failed",
                service=service or "unknown",
                author=author,
                details=failed.to_dict(),
            )
            failed.change_ids.append(change["id"])
            return self._escalate(failed, incident_obj)

        change_ids = []
        if apply_result.get("change_id"):
            change_ids.append(apply_result["change_id"])

        verify_result = None
        recovered = True
        if not skip_verify and service:
            verify_result = self.verify_recovery(service)
            recovered = bool(verify_result.get("recovered"))

        now = time.time()
        result = FixResult(
            status="verified" if recovered else "failed",
            tier=tier,
            action=action,
            args=args,
            incident_id=incident_id,
            service=service,
            snapshot=snap.to_dict() if snap else None,
            apply_result=apply_result,
            verify_result=verify_result,
            change_ids=change_ids,
            message="Fix verified" if recovered else "Fix applied but recovery not verified",
            t2=format_iso(now),
            t2_unix=now,
        )

        self.supervisor.log_change(
            action="fix_verified" if recovered else "fix_unverified",
            service=service or "unknown",
            author=author,
            details=result.to_dict(),
        )

        if recovered:
            if incident_obj and self.detector:
                self.detector.update_incident_status(
                    incident_obj.id,
                    "resolved",
                    metadata_update={
                        "fix_result": result.to_dict(),
                        "t2": result.t2,
                        "t2_unix": result.t2_unix,
                    },
                )
            return result

        return self._escalate(result, incident_obj)

    def apply_diagnosis(
        self,
        diagnosis: Diagnosis | dict[str, Any],
        **kwargs: Any,
    ) -> FixResult:
        """Convenience: apply the proposed_fix from a Diagnosis."""
        if isinstance(diagnosis, Diagnosis):
            proposed = diagnosis.proposed_fix
            incident_id = diagnosis.incident_id
        else:
            proposed = diagnosis.get("proposed_fix") or {}
            incident_id = diagnosis.get("incident_id")
        return self.apply_fix(
            proposed,
            incident=incident_id,
            diagnosis=diagnosis,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Approvals API
    # ------------------------------------------------------------------

    def _create_approval(
        self,
        *,
        incident_id: str,
        action: str,
        args: dict[str, Any],
        reason: str = "",
    ) -> Approval:
        now = time.time()
        approval = Approval(
            id=self._next_approval_id(),
            incident_id=incident_id,
            action=action,
            args=args,
            status="pending",
            created_at=format_iso(now),
            created_at_unix=now,
            reason=reason,
        )
        self._save_approval(approval)
        self.supervisor.log_change(
            action="approval_requested",
            service=str(args.get("service") or "unknown"),
            author="fixer",
            details={
                "approval_id": approval.id,
                "incident_id": incident_id,
                "fix_action": action,
                "args": args,
                "reason": reason,
            },
        )
        return approval

    def approve(self, approval_id: str, decided_by: str = "human") -> Approval:
        approval = self.get_approval(approval_id)
        if not approval:
            raise FixerError(f"Approval '{approval_id}' not found")
        if approval.status != "pending":
            raise FixerError(
                f"Approval '{approval_id}' is already {approval.status}"
            )
        now = time.time()
        approval.status = "approved"
        approval.decided_at = format_iso(now)
        approval.decided_at_unix = now
        approval.decided_by = decided_by
        self._save_approval(approval)
        self.supervisor.log_change(
            action="approval_approved",
            service=str(approval.args.get("service") or "unknown"),
            author=decided_by,
            details={"approval_id": approval.id, "action": approval.action},
        )
        return approval

    def deny(self, approval_id: str, decided_by: str = "human", reason: str = "") -> Approval:
        approval = self.get_approval(approval_id)
        if not approval:
            raise FixerError(f"Approval '{approval_id}' not found")
        if approval.status != "pending":
            raise FixerError(
                f"Approval '{approval_id}' is already {approval.status}"
            )
        now = time.time()
        approval.status = "denied"
        approval.decided_at = format_iso(now)
        approval.decided_at_unix = now
        approval.decided_by = decided_by
        if reason:
            approval.reason = reason
        self._save_approval(approval)
        self.supervisor.log_change(
            action="approval_denied",
            service=str(approval.args.get("service") or "unknown"),
            author=decided_by,
            details={
                "approval_id": approval.id,
                "action": approval.action,
                "reason": approval.reason,
            },
        )
        return approval

    def wait_for_approval(
        self,
        approval_id: str,
        timeout: float | None = None,
        poll_interval: float = 0.25,
    ) -> Approval:
        """Block until approval is decided or timeout (then mark timed_out)."""
        timeout = self.approval_timeout if timeout is None else float(timeout)
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            approval = self.get_approval(approval_id)
            if not approval:
                raise FixerError(f"Approval '{approval_id}' not found")
            if approval.status != "pending":
                return approval
            time.sleep(poll_interval)

        approval = self.get_approval(approval_id)
        if not approval:
            raise FixerError(f"Approval '{approval_id}' not found")
        if approval.status == "pending":
            now = time.time()
            approval.status = "timed_out"
            approval.decided_at = format_iso(now)
            approval.decided_at_unix = now
            approval.decided_by = "system"
            self._save_approval(approval)
            self.supervisor.log_change(
                action="approval_timed_out",
                service=str(approval.args.get("service") or "unknown"),
                author="fixer",
                details={"approval_id": approval.id},
            )
        return approval

    def execute_approved(
        self,
        approval_id: str,
        *,
        decided_by: str = "human",
        skip_verify: bool = False,
    ) -> FixResult:
        """Approve (if still pending) and immediately apply the fix."""
        approval = self.get_approval(approval_id)
        if not approval:
            raise FixerError(f"Approval '{approval_id}' not found")
        if approval.status == "pending":
            approval = self.approve(approval_id, decided_by=decided_by)
        if approval.status != "approved":
            raise FixerError(
                f"Cannot execute approval '{approval_id}' in status {approval.status}"
            )
        result = self.apply_fix(
            {"action": approval.action, "args": approval.args},
            incident=approval.incident_id,
            approved_by=approval.decided_by or decided_by,
            skip_verify=skip_verify,
        )
        approval.result = result.to_dict()
        self._save_approval(approval)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _block_never_do(
        self,
        action: str,
        args: dict[str, Any],
        incident_id: str | None,
        message: str | None = None,
    ) -> FixResult:
        msg = message or f"Blocked never-do action '{action}'"
        result = FixResult(
            status="blocked",
            tier="never_do",
            action=action,
            args=args,
            incident_id=incident_id,
            service=args.get("service") if isinstance(args, dict) else None,
            message=msg,
        )
        change = self.supervisor.log_change(
            action="fix_blocked",
            service=str(args.get("service") or "unknown"),
            author="fixer",
            details=result.to_dict(),
        )
        result.change_ids.append(change["id"])
        return result

    def _escalate(
        self,
        result: FixResult,
        incident: Incident | None,
    ) -> FixResult:
        # Keep "denied" distinct; everything else that reaches escalate becomes escalated
        # (including failed apply/verify), matching verify -> resolve | escalate.
        if result.status != "denied":
            result.status = "escalated"

        # If escalation happened without an approval (e.g. failed restart/verify or crash),
        # create an ask-first rollback approval so the operator has an actionable recovery card.
        # Guard: never propose rollback if the action that escalated was already rollback
        if result.service and result.action != "rollback" and not result.approval_id and result.status != "denied":
            try:
                inc_id = result.incident_id or (incident.id if incident else "unknown")
                rollback_app = self._create_approval(
                    incident_id=inc_id,
                    action="rollback",
                    args={"service": result.service, "to_version": "1.0.0"},
                    reason=f"Action '{result.action}' failed/escalated — operator approval required to rollback",
                )
                result.approval_id = rollback_app.id
            except Exception:
                pass

        if incident and self.detector:
            self.detector.update_incident_status(
                incident.id,
                "escalated",
                metadata_update={
                    "fix_result": result.to_dict(),
                    **({"pending_approval_id": result.approval_id} if result.approval_id else {}),
                },
            )
        self.supervisor.log_change(
            action="fix_escalated",
            service=result.service or "unknown",
            author="fixer",
            details=result.to_dict(),
        )
        return result

    @staticmethod
    def _normalize_proposed_fix(
        proposed_fix: dict[str, Any] | None,
        diagnosis: Diagnosis | dict[str, Any] | None,
    ) -> dict[str, Any]:
        if proposed_fix is None and diagnosis is not None:
            if isinstance(diagnosis, Diagnosis):
                proposed_fix = diagnosis.proposed_fix
            else:
                proposed_fix = diagnosis.get("proposed_fix")
        if not proposed_fix:
            raise FixerError("proposed_fix is required")
        if isinstance(proposed_fix, str):
            proposed_fix = {"action": proposed_fix, "args": {}}
        action = str(proposed_fix.get("action") or "").strip().lower()
        args = proposed_fix.get("args") or {}
        if not isinstance(args, dict):
            args = {"service": args}
        if not action:
            raise FixerError("proposed_fix.action is required")
        return {"action": action, "args": dict(args)}

    def _resolve_incident(
        self,
        incident: Incident | dict[str, Any] | str | None,
        diagnosis: Diagnosis | dict[str, Any] | None,
    ) -> Incident | None:
        if isinstance(incident, Incident):
            return incident
        if isinstance(incident, dict):
            return Incident(
                id=str(incident["id"]),
                status=str(incident.get("status", "diagnosing")),
                trigger_rule=str(incident.get("trigger_rule", "unknown")),
                t0=str(incident.get("t0", format_iso())),
                t0_unix=float(incident.get("t0_unix", time.time())),
                created_at=str(incident.get("created_at", format_iso())),
                created_at_unix=float(incident.get("created_at_unix", time.time())),
                ttd_seconds=float(incident.get("ttd_seconds", 0.0)),
                affected_services=list(incident.get("affected_services") or []),
                evidence=list(incident.get("evidence") or []),
                metadata=dict(incident.get("metadata") or {}),
            )
        incident_id = None
        if isinstance(incident, str):
            incident_id = incident
        elif isinstance(diagnosis, Diagnosis):
            incident_id = diagnosis.incident_id
        elif isinstance(diagnosis, dict):
            incident_id = diagnosis.get("incident_id")
        if incident_id and self.detector:
            return self.detector.get_incident(str(incident_id))
        return None


# =========================================================================
# CLI
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Fix Pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_apply = sub.add_parser("apply", help="Apply a proposed fix action")
    p_apply.add_argument("action", help="Action name (e.g. restart_service)")
    p_apply.add_argument("--service", required=True, help="Target service")
    p_apply.add_argument("--incident", default=None, help="Incident ID")
    p_apply.add_argument(
        "--arg",
        action="append",
        default=[],
        help="Extra arg as key=value (repeatable)",
    )
    p_apply.add_argument("--skip-verify", action="store_true")

    p_diag = sub.add_parser("from-incident", help="Apply proposed_fix from incident metadata")
    p_diag.add_argument("incident_id", help="Incident ID with diagnosis metadata")
    p_diag.add_argument("--wait", action="store_true", help="Wait for ask-first approval")
    p_diag.add_argument("--timeout", type=float, default=DEFAULT_APPROVAL_TIMEOUT)

    sub.add_parser("pending", help="List pending approvals")

    p_appr = sub.add_parser("approve", help="Approve a pending ask-first action")
    p_appr.add_argument("approval_id")
    p_appr.add_argument("--by", default="human")
    p_appr.add_argument("--execute", action="store_true", help="Apply immediately after approve")

    p_deny = sub.add_parser("deny", help="Deny a pending ask-first action")
    p_deny.add_argument("approval_id")
    p_deny.add_argument("--by", default="human")
    p_deny.add_argument("--reason", default="")

    p_tier = sub.add_parser("tier", help="Classify an action's permission tier")
    p_tier.add_argument("action")

    args = parser.parse_args(argv)
    obs = get_observability()
    detector = IncidentDetector(observability=obs)
    fixer = Fixer(observability=obs, detector=detector)

    if args.command == "tier":
        print(fixer.classify_tier(args.action))
        return 0

    if args.command == "pending":
        pending = fixer.list_approvals(status="pending")
        if not pending:
            print("No pending approvals.")
            return 0
        for a in pending:
            print(
                f"{a.id}  incident={a.incident_id}  action={a.action}  "
                f"args={json.dumps(a.args)}  reason={a.reason}"
            )
        return 0

    if args.command == "approve":
        if args.execute:
            result = fixer.execute_approved(args.approval_id, decided_by=args.by)
            print(json.dumps(result.to_dict(), indent=2, default=str))
            return 0 if result.status in ("verified", "applied") else 1
        approval = fixer.approve(args.approval_id, decided_by=args.by)
        print(json.dumps(approval.to_dict(), indent=2, default=str))
        return 0

    if args.command == "deny":
        approval = fixer.deny(args.approval_id, decided_by=args.by, reason=args.reason)
        print(json.dumps(approval.to_dict(), indent=2, default=str))
        return 0

    if args.command == "apply":
        extra: dict[str, Any] = {"service": args.service}
        for item in args.arg:
            if "=" not in item:
                print(f"Invalid --arg {item!r}; expected key=value")
                return 2
            k, v = item.split("=", 1)
            extra[k] = v
        result = fixer.apply_fix(
            {"action": args.action, "args": extra},
            incident=args.incident,
            skip_verify=args.skip_verify,
        )
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.status in ("verified", "pending_approval", "applied") else 1

    if args.command == "from-incident":
        inc = detector.get_incident(args.incident_id)
        if not inc:
            print(f"Incident '{args.incident_id}' not found")
            return 1
        diagnosis = (inc.metadata or {}).get("diagnosis")
        if not diagnosis:
            print(f"Incident '{args.incident_id}' has no diagnosis metadata")
            return 1
        result = fixer.apply_fix(
            diagnosis.get("proposed_fix"),
            incident=inc,
            diagnosis={**diagnosis, "incident_id": inc.id},
            wait_for_approval=args.wait,
            approval_timeout=args.timeout,
        )
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.status in ("verified", "pending_approval", "applied") else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
