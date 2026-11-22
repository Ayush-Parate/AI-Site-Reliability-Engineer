"""Observability layer for AI Site Reliability Engineer.

Provides:
- Metric Collector: Poll /metrics and /health into SQLite (data/observability.db).
- Log Reader: Query and filter structured JSON lines from logs/*.log (get_logs).
- Dependency Graph: Query upstream/downstream dependencies (get_deps).
- Change Query: Retrieve recent change events from data/changes.jsonl (get_changes).
- Derived Metrics: Functions for error_rate(svc, window), p95_latency(svc, window), and check_health().
- Stable Evidence IDs: Every returned item tagged with citation IDs (LOG-1042, MET-77, CHG-9).
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from prometheus_client.parser import text_string_to_metric_families

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
LOGS_DIR = REPO_ROOT / "logs"

DEFAULT_DB_PATH = DATA_DIR / "observability.db"
DEFAULT_CHANGES_PATH = DATA_DIR / "changes.jsonl"
DEFAULT_DEPS_PATH = CONFIG_DIR / "dependencies.yaml"
DEFAULT_SLO_PATH = CONFIG_DIR / "slo.yaml"

SERVICES_ORDER = ["gateway", "orders", "payments", "inventory"]


def parse_since(
    since: str | float | int | datetime.datetime | None,
    now_ts: float | None = None,
) -> float:
    """Parse 'since' parameter into a unix timestamp.

    Accepts:
    - None: returns 0.0 (beginning of time)
    - float/int < 1_000_000_000: relative seconds in the past (now - since)
    - float/int >= 1_000_000_000: absolute unix epoch timestamp
    - str like "30s", "5m", "1h": relative duration in seconds
    - str numeric: parsed as float
    - str ISO timestamp: parsed as ISO 8601 UTC
    """
    if since is None:
        return 0.0

    current_ts = time.time() if now_ts is None else now_ts

    if isinstance(since, (int, float)):
        val = float(since)
        if val <= 0:
            return 0.0
        if val < 1_000_000_000:
            return max(0.0, current_ts - val)
        return val

    if isinstance(since, datetime.datetime):
        if since.tzinfo is None:
            since = since.replace(tzinfo=datetime.timezone.utc)
        return since.timestamp()

    if isinstance(since, str):
        since_clean = since.strip()
        if not since_clean:
            return 0.0

        # Match durations like "30s", "5m", "2h", "1d"
        m = re.match(r"^([0-9.]+)\s*([smhdSMHD]?)$", since_clean)
        if m:
            num = float(m.group(1))
            unit = m.group(2).lower()
            multiplier = 1.0
            if unit == "m":
                multiplier = 60.0
            elif unit == "h":
                multiplier = 3600.0
            elif unit == "d":
                multiplier = 86400.0
            seconds = num * multiplier
            if seconds < 1_000_000_000:
                return max(0.0, current_ts - seconds)
            return seconds

        # Try ISO 8601 parsing
        try:
            iso_str = since_clean.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

    return 0.0


def format_iso(unix_ts: float | None = None) -> str:
    """Format a unix timestamp as ISO 8601 UTC string."""
    ts = time.time() if unix_ts is None else unix_ts
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DerivedMetricResult(dict[str, Any]):
    """Dictionary holding derived metric calculations with numeric float casting."""

    def __init__(self, metric_key: str, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.metric_key = metric_key

    def __float__(self) -> float:
        val = self.get(self.metric_key, 0.0)
        return float(val) if val is not None else 0.0

    def __int__(self) -> int:
        return int(float(self))


class Observability:
    """Unified observability store, collector, and reader."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        logs_dir: Path | str | None = None,
        changes_path: Path | str | None = None,
        deps_path: Path | str | None = None,
        slo_path: Path | str | None = None,
        repo_root: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.data_dir = self.repo_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = Path(db_path or (self.data_dir / "observability.db"))
        self.logs_dir = Path(logs_dir or (self.repo_root / "logs"))
        self.changes_path = Path(changes_path or (self.data_dir / "changes.jsonl"))
        self.deps_path = Path(deps_path or (self.repo_root / "config" / "dependencies.yaml"))
        self.slo_path = Path(slo_path or (self.repo_root / "config" / "slo.yaml"))

        self._db_lock = threading.Lock()
        self._collector_thread: threading.Thread | None = None
        self._stop_collector_event = threading.Event()
        self._last_line_indexed: dict[str, int] = {}

        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """Create a configured SQLite connection."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_db(self) -> None:
        """Create database tables and indexes if they do not exist."""
        with self._db_lock, self.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    timestamp_unix REAL NOT NULL,
                    service TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    labels TEXT NOT NULL DEFAULT '{}',
                    value REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_metrics_svc_ts
                    ON metrics(service, timestamp_unix);
                CREATE INDEX IF NOT EXISTS idx_metrics_name_ts
                    ON metrics(metric, timestamp_unix);
                CREATE INDEX IF NOT EXISTS idx_metrics_svc_name_ts
                    ON metrics(service, metric, timestamp_unix);

                CREATE TABLE IF NOT EXISTS health_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    timestamp_unix REAL NOT NULL,
                    service TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_health_svc_ts
                    ON health_checks(service, timestamp_unix);

                CREATE TABLE IF NOT EXISTS log_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    file_line INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    timestamp_unix REAL NOT NULL,
                    level TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    msg TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    status INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    UNIQUE(service, file_line)
                );

                CREATE INDEX IF NOT EXISTS idx_log_svc_ts
                    ON log_index(service, timestamp_unix);
                CREATE INDEX IF NOT EXISTS idx_log_trace
                    ON log_index(trace_id);
                CREATE INDEX IF NOT EXISTS idx_log_level
                    ON log_index(level);

                CREATE TABLE IF NOT EXISTS log_sync_tracker (
                    service TEXT PRIMARY KEY,
                    last_byte_offset INTEGER NOT NULL,
                    last_ts TEXT NOT NULL DEFAULT ''
                );
            """)

    def _load_dependencies(self) -> dict[str, dict[str, Any]]:
        if self.deps_path.is_file():
            try:
                data = yaml.safe_load(self.deps_path.read_text(encoding="utf-8")) or {}
                return data.get("services", {})
            except Exception:
                pass
        return {
            "gateway": {"port": 8000, "calls": ["orders"]},
            "orders": {"port": 8001, "calls": ["payments", "inventory"]},
            "payments": {"port": 8002, "calls": []},
            "inventory": {"port": 8003, "calls": []},
        }

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

    # =========================================================================
    # 1. Dependency Graph (get_deps)
    # =========================================================================
    def get_deps(self, service: str | None = None) -> dict[str, Any]:
        """Query upstream and downstream dependencies for a service or all services.

        Upstream = services that call this service.
        Downstream = services this service calls.
        """
        registry = self._load_dependencies()

        upstream_map: dict[str, list[str]] = {s: [] for s in registry}
        downstream_map: dict[str, list[str]] = {}

        for svc_name, cfg in registry.items():
            calls = cfg.get("calls", [])
            downstream_map[svc_name] = list(calls)
            for downstream in calls:
                if downstream in upstream_map:
                    upstream_map[downstream].append(svc_name)
                else:
                    upstream_map[downstream] = [svc_name]

        if service:
            service_clean = service.lower().strip()
            if service_clean not in registry:
                valid = ", ".join(sorted(registry.keys()))
                raise ValueError(f"Unknown service '{service}'. Valid services: {valid}")

            return {
                "service": service_clean,
                "port": registry[service_clean].get("port"),
                "upstream": upstream_map.get(service_clean, []),
                "downstream": downstream_map.get(service_clean, []),
            }

        result: dict[str, Any] = {"services": {}}
        for s, cfg in registry.items():
            result["services"][s] = {
                "port": cfg.get("port"),
                "upstream": upstream_map.get(s, []),
                "downstream": downstream_map.get(s, []),
            }
        return result

    # =========================================================================
    # 2. Change Query (get_changes)
    # =========================================================================
    def get_changes(
        self,
        since: str | float | int | datetime.datetime | None = None,
        service: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Retrieve recent change events from data/changes.jsonl.

        Every change carries a stable evidence ID (e.g. CHG-9).
        """
        if not self.changes_path.is_file():
            return []

        since_ts = parse_since(since)
        target_service = service.lower().strip() if service else None
        if target_service in ("all", "*"):
            target_service = None

        changes: list[dict[str, Any]] = []
        try:
            with open(self.changes_path, "r", encoding="utf-8") as f:
                for line_idx, line in enumerate(f, start=1):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        record = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue

                    # Ensure stable evidence ID
                    if not record.get("id"):
                        record["id"] = f"CHG-{line_idx}"

                    # Timestamp parsing
                    record_ts_str = record.get("ts", "")
                    try:
                        dt = datetime.datetime.fromisoformat(record_ts_str.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                        record_ts = dt.timestamp()
                    except (ValueError, TypeError):
                        record_ts = 0.0

                    if since_ts > 0.0 and record_ts < since_ts:
                        continue

                    rec_svc = str(record.get("service", "")).lower()
                    if target_service and rec_svc != target_service and rec_svc != "all":
                        continue

                    record["timestamp_unix"] = record_ts
                    changes.append(record)
        except OSError:
            return []

        # Return latest changes first, capped at limit
        changes.sort(key=lambda x: x.get("timestamp_unix", 0.0), reverse=True)
        return changes[: max(1, limit)]

    # =========================================================================
    # 3. Log Reader (get_logs) & Indexing
    # =========================================================================
    def _sync_logs(self) -> None:
        """Scan logs/*.log and index new lines into log_index table."""
        if not self.logs_dir.is_dir():
            return

        registry = self._load_dependencies()
        service_names = list(registry.keys()) + [
            p.stem for p in self.logs_dir.glob("*.log") if p.stem not in registry
        ]

        with self._db_lock, self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS log_sync_tracker (
                    service TEXT PRIMARY KEY,
                    last_byte_offset INTEGER NOT NULL,
                    last_ts TEXT NOT NULL DEFAULT ''
                );
                """
            )
            for svc in service_names:
                log_file = self.logs_dir / f"{svc}.log"
                if not log_file.is_file():
                    continue

                try:
                    file_size = log_file.stat().st_size
                except OSError:
                    continue

                if file_size == 0:
                    continue

                cursor.execute(
                    "SELECT last_byte_offset, last_ts FROM log_sync_tracker WHERE service = ?",
                    (svc,),
                )
                tracker_row = cursor.fetchone()

                if tracker_row:
                    last_offset = int(tracker_row[0])
                    last_ts = tracker_row[1] or ""
                    # Detect log rotation or truncation
                    if file_size < last_offset:
                        last_offset = 0
                        last_ts = ""
                else:
                    # Initial sync: check if any logs already exist in log_index for this service
                    cursor.execute(
                        "SELECT COALESCE(MAX(ts), '') FROM log_index WHERE service = ?",
                        (svc,),
                    )
                    max_ts_db = cursor.fetchone()[0] or ""
                    last_offset = 0
                    last_ts = max_ts_db

                cursor.execute(
                    "SELECT COALESCE(MAX(file_line), 0) FROM log_index WHERE service = ?",
                    (svc,),
                )
                max_line_db = cursor.fetchone()[0]

                new_rows: list[tuple[Any, ...]] = []
                new_last_ts = last_ts

                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        if last_offset > 0:
                            f.seek(last_offset)

                        for line in f:
                            line_str = line.strip()
                            if not line_str:
                                continue

                            try:
                                d = json.loads(line_str)
                            except json.JSONDecodeError:
                                # Non-JSON log line fallback
                                ts_now = format_iso()
                                d = {
                                    "ts": ts_now,
                                    "level": "INFO",
                                    "service": svc,
                                    "trace_id": "-",
                                    "msg": line_str,
                                    "latency_ms": 0.0,
                                    "status": 0,
                                }

                            ts_str = d.get("ts", format_iso())
                            # If scanning from beginning (last_offset == 0) and we have a last_ts from DB, skip lines already in DB
                            if last_offset == 0 and last_ts and ts_str <= last_ts:
                                continue

                            try:
                                dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                                ts_unix = dt.timestamp()
                            except (ValueError, TypeError):
                                ts_unix = time.time()

                            level = str(d.get("level", "INFO")).upper()
                            trace_id = str(d.get("trace_id", "-"))
                            msg = str(d.get("msg", ""))
                            latency_ms = float(d.get("latency_ms", 0.0))
                            status = int(d.get("status", 0))

                            max_line_db += 1
                            new_rows.append((
                                svc,
                                max_line_db,
                                ts_str,
                                ts_unix,
                                level,
                                trace_id,
                                msg,
                                latency_ms,
                                status,
                                line_str,
                            ))
                            if ts_str > new_last_ts:
                                new_last_ts = ts_str

                        new_offset = f.tell()

                    if new_rows:
                        cursor.executemany(
                            """
                            INSERT OR IGNORE INTO log_index (
                                service, file_line, ts, timestamp_unix, level,
                                trace_id, msg, latency_ms, status, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            new_rows,
                        )

                    cursor.execute(
                        """
                        INSERT INTO log_sync_tracker (service, last_byte_offset, last_ts)
                        VALUES (?, ?, ?)
                        ON CONFLICT(service) DO UPDATE SET
                            last_byte_offset = excluded.last_byte_offset,
                            last_ts = excluded.last_ts
                        """,
                        (svc, new_offset, new_last_ts),
                    )
                    conn.commit()
                except OSError:
                    continue

    def get_logs(
        self,
        service: str | None = None,
        since: str | float | int | datetime.datetime | None = None,
        level: str | None = None,
        trace_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query and filter structured JSON lines from logs/*.log.

        Every returned item is tagged with a stable citation ID (e.g. LOG-1042).
        """
        # Ensure fresh logs from disk are synced to index
        self._sync_logs()

        since_ts = parse_since(since)
        query = "SELECT id, service, file_line, ts, timestamp_unix, level, trace_id, msg, latency_ms, status, raw_json FROM log_index WHERE 1=1"
        params: list[Any] = []

        if service and service.lower() not in ("all", "*"):
            query += " AND service = ?"
            params.append(service.lower().strip())

        if since_ts > 0.0:
            query += " AND timestamp_unix >= ?"
            params.append(since_ts)

        if level:
            lvl = level.upper().strip()
            if lvl == "ERROR":
                query += " AND level IN ('ERROR', 'CRITICAL')"
            elif lvl == "WARNING":
                query += " AND level IN ('WARNING', 'ERROR', 'CRITICAL')"
            else:
                query += " AND level = ?"
                params.append(lvl)

        if trace_id:
            query += " AND trace_id = ?"
            params.append(trace_id.strip())

        query += " ORDER BY timestamp_unix DESC LIMIT ?"
        params.append(max(1, limit))

        results: list[dict[str, Any]] = []
        with self._db_lock, self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                item = {
                    "id": f"LOG-{row['id']}",
                    "ts": row["ts"],
                    "timestamp_unix": row["timestamp_unix"],
                    "service": row["service"],
                    "level": row["level"],
                    "trace_id": row["trace_id"],
                    "msg": row["msg"],
                    "latency_ms": row["latency_ms"],
                    "status": row["status"],
                }
                # Attach extra data from raw_json if present
                try:
                    raw_dict = json.loads(row["raw_json"])
                    if "extra_data" in raw_dict and isinstance(raw_dict["extra_data"], dict):
                        item["extra"] = raw_dict["extra_data"]
                except Exception:
                    pass
                results.append(item)

        return results

    # =========================================================================
    # 4. Metric Collector & Query (get_metrics)
    # =========================================================================
    def poll_service(self, service: str, port: int, timeout: float = 1.0) -> dict[str, Any]:
        """Poll /health and /metrics for a single service and store in SQLite."""
        now_unix = time.time()
        now_ts = format_iso(now_unix)
        stats = {"service": service, "health": "unknown", "samples": 0}

        # 1. Poll /health
        health_url = f"http://127.0.0.1:{port}/health"
        health_ok = False
        health_status = "unreachable"
        status_code = 0
        health_latency_ms = 0.0
        health_details: dict[str, Any] = {}

        t0 = time.perf_counter()
        try:
            r = httpx.get(health_url, timeout=timeout)
            health_latency_ms = (time.perf_counter() - t0) * 1000.0
            status_code = r.status_code
            if status_code == 200:
                health_ok = True
                health_status = "ok"
                try:
                    health_details = r.json()
                except Exception:
                    health_details = {"text": r.text}
            else:
                health_status = "unhealthy"
                try:
                    health_details = r.json()
                except Exception:
                    health_details = {"text": r.text}
        except Exception as exc:
            health_latency_ms = (time.perf_counter() - t0) * 1000.0
            health_details = {"error": str(exc)}

        stats["health"] = health_status

        # 2. Poll /metrics
        metrics_url = f"http://127.0.0.1:{port}/metrics"
        metric_samples: list[tuple[str, float, str, str, str, float]] = []

        # Synthetic health metrics
        metric_samples.append((
            now_ts,
            now_unix,
            service,
            "up",
            json.dumps({"service": service}),
            1.0 if health_ok else 0.0,
        ))
        metric_samples.append((
            now_ts,
            now_unix,
            service,
            "health_latency_ms",
            json.dumps({"service": service}),
            round(health_latency_ms, 2),
        ))

        try:
            r_metrics = httpx.get(metrics_url, timeout=timeout)
            if r_metrics.status_code == 200:
                families = text_string_to_metric_families(r_metrics.text)
                for fam in families:
                    for sample in fam.samples:
                        metric_samples.append((
                            now_ts,
                            now_unix,
                            service,
                            sample.name,
                            json.dumps(sample.labels or {}),
                            float(sample.value),
                        ))
        except Exception:
            pass

        stats["samples"] = len(metric_samples)

        # Batch insert into DB
        with self._db_lock, self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO health_checks (
                    ts, timestamp_unix, service, status, status_code, latency_ms, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts,
                    now_unix,
                    service,
                    health_status,
                    status_code,
                    health_latency_ms,
                    json.dumps(health_details),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO metrics (
                    ts, timestamp_unix, service, metric, labels, value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                metric_samples,
            )
            conn.commit()

        return stats

    def poll_once(self, timeout: float = 1.0) -> dict[str, Any]:
        """Poll /health and /metrics across all registered services and sync logs."""
        registry = self._load_dependencies()
        results: dict[str, Any] = {}
        for svc, cfg in registry.items():
            port = cfg["port"]
            results[svc] = self.poll_service(svc, port, timeout=timeout)

        # Also sync fresh logs
        self._sync_logs()
        return results

    def start_background_collector(self, interval_seconds: float = 5.0) -> threading.Thread:
        """Start background daemon thread polling metrics every interval_seconds."""
        if self._collector_thread and self._collector_thread.is_alive():
            return self._collector_thread

        self._stop_collector_event.clear()

        def _worker() -> None:
            while not self._stop_collector_event.is_set():
                try:
                    self.poll_once()
                except Exception:
                    pass
                self._stop_collector_event.wait(interval_seconds)

        self._collector_thread = threading.Thread(
            target=_worker,
            name="observability_metric_collector",
            daemon=True,
        )
        self._collector_thread.start()
        return self._collector_thread

    def stop_background_collector(self, timeout: float = 3.0) -> None:
        """Stop background metric collector thread."""
        self._stop_collector_event.set()
        if self._collector_thread and self._collector_thread.is_alive():
            self._collector_thread.join(timeout=timeout)
        self._collector_thread = None

    @property
    def is_collector_running(self) -> bool:
        return self._collector_thread is not None and self._collector_thread.is_alive()

    def get_metrics(
        self,
        service: str | None = None,
        metric: str | None = None,
        window: str | float | int | None = 60,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query time series metric samples plus summary statistics.

        Every sample carries a stable evidence ID (e.g. MET-77).
        """
        since_ts = parse_since(window)
        query = "SELECT id, ts, timestamp_unix, service, metric, labels, value FROM metrics WHERE 1=1"
        params: list[Any] = []

        if service and service.lower() not in ("all", "*"):
            query += " AND service = ?"
            params.append(service.lower().strip())

        if metric:
            m_clean = metric.strip()
            # If wildcard in metric
            if "%" in m_clean or "*" in m_clean:
                query += " AND metric LIKE ?"
                params.append(m_clean.replace("*", "%"))
            else:
                query += " AND metric = ?"
                params.append(m_clean)

        if since_ts > 0.0:
            query += " AND timestamp_unix >= ?"
            params.append(since_ts)

        query += " ORDER BY timestamp_unix DESC LIMIT ?"
        params.append(max(1, limit))

        samples: list[dict[str, Any]] = []
        values: list[float] = []

        with self._db_lock, self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                val = float(row["value"])
                values.append(val)
                lbl_dict = {}
                try:
                    lbl_dict = json.loads(row["labels"])
                except Exception:
                    pass

                samples.append({
                    "id": f"MET-{row['id']}",
                    "ts": row["ts"],
                    "timestamp_unix": row["timestamp_unix"],
                    "service": row["service"],
                    "metric": row["metric"],
                    "labels": lbl_dict,
                    "value": val,
                })

        summary: dict[str, Any] = {
            "count": len(values),
            "latest": values[0] if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "avg": round(sum(values) / len(values), 3) if values else None,
            "evidence_id": samples[0]["id"] if samples else None,
        }

        window_sec = (time.time() - since_ts) if since_ts > 0.0 else None

        return {
            "service": service,
            "metric": metric,
            "window_seconds": round(window_sec, 1) if window_sec else None,
            "summary": summary,
            "samples": samples,
        }

    # =========================================================================
    # 5. Derived Metrics: error_rate, p95_latency, check_health
    # =========================================================================
    def error_rate(
        self,
        service: str,
        window: str | float | int = 30,
    ) -> DerivedMetricResult:
        """Compute error rate for a service over the given window.

        Checks both ground-truth logs and Prometheus counters.
        Returns a DerivedMetricResult containing error_rate, total_requests,
        slo_threshold, breached flag, and cited evidence IDs.
        Casting float(result) yields the error_rate as a float (0.0 to 1.0).
        """
        svc_clean = service.lower().strip()
        slo = self._load_slo()
        threshold = float(slo.get("error_rate_threshold", 0.05))
        since_ts = parse_since(window)
        now_ts = time.time()
        window_seconds = round(now_ts - since_ts, 1) if since_ts > 0 else 30.0

        evidence_ids: list[str] = []
        total_requests = 0
        error_count = 0

        # 1. Check logs in the window for exact per-request ground truth
        logs = self.get_logs(service=svc_clean, since=window, limit=500)
        http_logs = [l for l in logs if l.get("status", 0) > 0]

        if http_logs:
            total_requests = len(http_logs)
            error_logs = [l for l in http_logs if l.get("status", 0) >= 400]
            error_count = len(error_logs)
            for el in error_logs[:5]:
                evidence_ids.append(el["id"])
        else:
            # 2. Fall back to Prometheus counters in metrics table
            with self._db_lock, self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, metric, value, timestamp_unix FROM metrics
                    WHERE service = ? AND metric IN ('http_requests_total', 'http_errors_total')
                      AND timestamp_unix >= ?
                    ORDER BY timestamp_unix ASC
                    """,
                    (svc_clean, since_ts),
                )
                rows = cursor.fetchall()

            req_samples = [r for r in rows if r["metric"] == "http_requests_total"]
            err_samples = [r for r in rows if r["metric"] == "http_errors_total"]

            if len(req_samples) >= 2:
                total_requests = max(0, int(req_samples[-1]["value"] - req_samples[0]["value"]))
                evidence_ids.append(f"MET-{req_samples[-1]['id']}")
            elif len(req_samples) == 1:
                total_requests = int(req_samples[0]["value"])
                evidence_ids.append(f"MET-{req_samples[0]['id']}")

            if len(err_samples) >= 2:
                error_count = max(0, int(err_samples[-1]["value"] - err_samples[0]["value"]))
                evidence_ids.append(f"MET-{err_samples[-1]['id']}")
            elif len(err_samples) == 1:
                error_count = int(err_samples[0]["value"])
                evidence_ids.append(f"MET-{err_samples[0]['id']}")

        rate = (error_count / total_requests) if total_requests > 0 else 0.0
        rate = round(rate, 4)
        breached = bool(rate > threshold and total_requests > 0)

        data = {
            "service": svc_clean,
            "window_seconds": window_seconds,
            "error_rate": rate,
            "total_requests": total_requests,
            "error_count": error_count,
            "slo_threshold": threshold,
            "breached": breached,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
        }
        return DerivedMetricResult("error_rate", data)

    def p95_latency(
        self,
        service: str,
        window: str | float | int = 60,
    ) -> DerivedMetricResult:
        """Compute p95 (and p50, p90, p99, max) latency in ms over window.

        Extracts latencies from structured logs and Prometheus histogram.
        Returns DerivedMetricResult where float(result) is p95_ms.
        """
        svc_clean = service.lower().strip()
        slo = self._load_slo()
        slo_limit = float(slo.get("latency_p95_slo_ms", 2000.0))
        since_ts = parse_since(window)
        now_ts = time.time()
        window_seconds = round(now_ts - since_ts, 1) if since_ts > 0 else 60.0

        evidence_ids: list[str] = []
        latencies: list[float] = []

        # 1. From recent logs
        logs = self.get_logs(service=svc_clean, since=window, limit=500)
        for log_entry in logs:
            lat = float(log_entry.get("latency_ms", 0.0))
            if lat > 0:
                latencies.append(lat)
                if lat >= slo_limit or lat > 500.0:
                    evidence_ids.append(log_entry["id"])

        if latencies:
            latencies.sort()
            n = len(latencies)

            def _percentile(p: float) -> float:
                idx = min(n - 1, max(0, int(math.ceil(p / 100.0 * n)) - 1))
                return round(latencies[idx], 2)

            p50 = _percentile(50)
            p90 = _percentile(90)
            p95 = _percentile(95)
            p99 = _percentile(99)
            avg_lat = round(sum(latencies) / n, 2)
            max_lat = round(latencies[-1], 2)
            sample_count = n
        else:
            # Fallback to health latency metrics
            metrics_res = self.get_metrics(
                service=svc_clean,
                metric="health_latency_ms",
                window=window,
                limit=50,
            )
            vals = [s["value"] for s in metrics_res["samples"]]
            if vals:
                vals.sort()
                p50 = p90 = p95 = p99 = round(vals[int(len(vals) * 0.95) - 1], 2)
                avg_lat = max_lat = p95
                sample_count = len(vals)
                if metrics_res["summary"]["evidence_id"]:
                    evidence_ids.append(metrics_res["summary"]["evidence_id"])
            else:
                p50 = p90 = p95 = p99 = avg_lat = max_lat = 0.0
                sample_count = 0

        breached = bool(p95 > slo_limit and sample_count > 0)

        data = {
            "service": svc_clean,
            "window_seconds": window_seconds,
            "p50_ms": p50,
            "p90_ms": p90,
            "p95_ms": p95,
            "p99_ms": p99,
            "avg_ms": avg_lat,
            "max_ms": max_lat,
            "sample_count": sample_count,
            "slo_threshold_ms": slo_limit,
            "breached": breached,
            "evidence_ids": list(dict.fromkeys(evidence_ids[:5])),
        }
        return DerivedMetricResult("p95_ms", data)

    def check_health(self, service: str | None = None) -> dict[str, Any]:
        """Check live health status of a service or all registered services."""
        registry = self._load_dependencies()

        if service and service.lower() not in ("all", "*"):
            svc_clean = service.lower().strip()
            if svc_clean not in registry:
                valid = ", ".join(sorted(registry.keys()))
                raise ValueError(f"Unknown service '{svc_clean}'. Valid services: {valid}")

            port = registry[svc_clean]["port"]
            poll_res = self.poll_service(svc_clean, port, timeout=1.0)
            is_healthy = (poll_res["health"] == "ok")

            # Look up recent health check evidence ID
            evidence_id = None
            with self._db_lock, self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, status, status_code, latency_ms FROM health_checks WHERE service = ? ORDER BY id DESC LIMIT 1",
                    (svc_clean,),
                )
                row = cursor.fetchone()
                if row:
                    evidence_id = f"MET-{row['id']}"

            return {
                "service": svc_clean,
                "healthy": is_healthy,
                "status": poll_res["health"],
                "port": port,
                "evidence_id": evidence_id,
            }

        # Check all services
        all_results = {}
        all_healthy = True
        for svc, cfg in registry.items():
            port = cfg["port"]
            poll_res = self.poll_service(svc, port, timeout=1.0)
            healthy = (poll_res["health"] == "ok")
            if not healthy:
                all_healthy = False
            all_results[svc] = {
                "service": svc,
                "healthy": healthy,
                "status": poll_res["health"],
                "port": port,
            }

        return {
            "healthy": all_healthy,
            "services": all_results,
        }

    def get_health(self, service: str | None = None) -> dict[str, Any]:
        """Alias for check_health."""
        return self.check_health(service)

    # =========================================================================
    # 6. Evidence Lookup (get_evidence)
    # =========================================================================
    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        """Look up the underlying record for any stable citation ID.

        Supports:
        - LOG-1042: log record from log_index
        - MET-77: metric record from metrics table
        - CHG-9: change record from changes.jsonl
        """
        eid = evidence_id.strip()
        if not eid:
            return None

        # 1. LOG-<id>
        if eid.upper().startswith("LOG-"):
            try:
                log_pk = int(eid.split("-", 1)[1])
                with self._db_lock, self.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, service, ts, timestamp_unix, level, trace_id, msg, latency_ms, status, raw_json FROM log_index WHERE id = ?",
                        (log_pk,),
                    )
                    row = cursor.fetchone()
                    if row:
                        res = {
                            "id": f"LOG-{row['id']}",
                            "type": "log",
                            "ts": row["ts"],
                            "timestamp_unix": row["timestamp_unix"],
                            "service": row["service"],
                            "level": row["level"],
                            "trace_id": row["trace_id"],
                            "msg": row["msg"],
                            "latency_ms": row["latency_ms"],
                            "status": row["status"],
                        }
                        try:
                            raw = json.loads(row["raw_json"])
                            if "extra_data" in raw:
                                res["extra"] = raw["extra_data"]
                        except Exception:
                            pass
                        return res
            except ValueError:
                pass

        # 2. MET-<id>
        if eid.upper().startswith("MET-"):
            try:
                met_pk = int(eid.split("-", 1)[1])
                with self._db_lock, self.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, ts, timestamp_unix, service, metric, labels, value FROM metrics WHERE id = ?",
                        (met_pk,),
                    )
                    row = cursor.fetchone()
                    if row:
                        lbls = {}
                        try:
                            lbls = json.loads(row["labels"])
                        except Exception:
                            pass
                        return {
                            "id": f"MET-{row['id']}",
                            "type": "metric",
                            "ts": row["ts"],
                            "timestamp_unix": row["timestamp_unix"],
                            "service": row["service"],
                            "metric": row["metric"],
                            "labels": lbls,
                            "value": float(row["value"]),
                        }
            except ValueError:
                pass

        # 3. CHG-<id>
        if eid.upper().startswith("CHG-"):
            changes = self.get_changes(limit=1000)
            for c in changes:
                if c.get("id") == eid.upper():
                    c["type"] = "change"
                    return c

        return None


# Global singleton instance for module-level functions
_OBSERVABILITY_INSTANCE: Observability | None = None


def get_observability() -> Observability:
    """Return the shared Observability singleton instance."""
    global _OBSERVABILITY_INSTANCE
    if _OBSERVABILITY_INSTANCE is None:
        _OBSERVABILITY_INSTANCE = Observability()
    return _OBSERVABILITY_INSTANCE


# Convenience module-level functions matching tool specifications
def get_logs(
    service: str | None = None,
    since: str | float | int | datetime.datetime | None = None,
    level: str | None = None,
    trace_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return get_observability().get_logs(
        service=service, since=since, level=level, trace_id=trace_id, limit=limit
    )


def get_metrics(
    service: str | None = None,
    metric: str | None = None,
    window: str | float | int | None = 60,
    limit: int = 100,
) -> dict[str, Any]:
    return get_observability().get_metrics(
        service=service, metric=metric, window=window, limit=limit
    )


def get_deps(service: str | None = None) -> dict[str, Any]:
    return get_observability().get_deps(service=service)


def get_changes(
    since: str | float | int | datetime.datetime | None = None,
    service: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return get_observability().get_changes(since=since, service=service, limit=limit)


def get_health(service: str | None = None) -> dict[str, Any]:
    return get_observability().get_health(service=service)


def check_health(service: str | None = None) -> dict[str, Any]:
    return get_observability().check_health(service=service)


def error_rate(
    service: str,
    window: str | float | int = 30,
) -> DerivedMetricResult:
    return get_observability().error_rate(service=service, window=window)


def p95_latency(
    service: str,
    window: str | float | int = 60,
) -> DerivedMetricResult:
    return get_observability().p95_latency(service=service, window=window)


def get_evidence(evidence_id: str) -> dict[str, Any] | None:
    return get_observability().get_evidence(evidence_id=evidence_id)


# =========================================================================
# CLI Interface
# =========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Observability Layer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Health
    p_health = subparsers.add_parser("health", help="Check service health")
    p_health.add_argument("service", nargs="?", default=None, help="Service name or omit for all")

    # Deps
    p_deps = subparsers.add_parser("deps", help="Query service call graph")
    p_deps.add_argument("service", nargs="?", default=None, help="Service name or omit for all")

    # Changes
    p_chg = subparsers.add_parser("changes", help="Query recent change events")
    p_chg.add_argument("--since", default=None, help="Since window (e.g. 60, 5m, ISO timestamp)")
    p_chg.add_argument("--service", default=None, help="Filter by service")
    p_chg.add_argument("--limit", type=int, default=20, help="Max entries to return")

    # Logs
    p_logs = subparsers.add_parser("logs", help="Query structured logs")
    p_logs.add_argument("--service", default=None, help="Service name")
    p_logs.add_argument("--since", default=None, help="Since window")
    p_logs.add_argument("--level", default=None, help="Log level (INFO, WARNING, ERROR)")
    p_logs.add_argument("--trace", default=None, help="Trace ID")
    p_logs.add_argument("--limit", type=int, default=20, help="Max entries")

    # Metrics
    p_met = subparsers.add_parser("metrics", help="Query metrics")
    p_met.add_argument("--service", default=None, help="Service name")
    p_met.add_argument("--metric", default=None, help="Metric name (e.g. up, http_requests_total)")
    p_met.add_argument("--window", default=60, help="Time window")
    p_met.add_argument("--limit", type=int, default=20, help="Max entries")

    # Derived
    p_drv = subparsers.add_parser("derived", help="Compute derived error rate and p95 latency")
    p_drv.add_argument("service", help="Target service")
    p_drv.add_argument("--window", default=30, help="Window in seconds")

    # Poll once
    subparsers.add_parser("poll", help="Poll /metrics and /health once and store in SQLite")

    # Collect continuous
    p_col = subparsers.add_parser("collect", help="Start continuous metric collector")
    p_col.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")

    # Evidence lookup
    p_evi = subparsers.add_parser("evidence", help="Look up citation by evidence ID")
    p_evi.add_argument("id", help="Evidence ID (e.g. LOG-1042, MET-77, CHG-9)")

    args = parser.parse_args(argv)
    obs = get_observability()

    try:
        if args.command == "health":
            res = obs.check_health(args.service)
            print(json.dumps(res, indent=2))
        elif args.command == "deps":
            res = obs.get_deps(args.service)
            print(json.dumps(res, indent=2))
        elif args.command == "changes":
            res = obs.get_changes(since=args.since, service=args.service, limit=args.limit)
            print(f"Retrieved {len(res)} change records:")
            for c in res:
                print(f"  [{c['id']}] {c['ts']} | {c['service']} | {c['action']} ({c['author']}): {c['details']}")
        elif args.command == "logs":
            res = obs.get_logs(
                service=args.service,
                since=args.since,
                level=args.level,
                trace_id=args.trace,
                limit=args.limit,
            )
            print(f"Retrieved {len(res)} log records:")
            for l in res:
                print(f"  [{l['id']}] {l['ts']} [{l['level']}] {l['service']}: {l['msg']} ({l['latency_ms']}ms, {l['status']})")
        elif args.command == "metrics":
            res = obs.get_metrics(
                service=args.service,
                metric=args.metric,
                window=args.window,
                limit=args.limit,
            )
            print("Summary:", json.dumps(res["summary"], indent=2))
            print(f"Samples ({len(res['samples'])}):")
            for s in res["samples"][:10]:
                print(f"  [{s['id']}] {s['ts']} | {s['service']} | {s['metric']} = {s['value']} {s['labels']}")
        elif args.command == "derived":
            err = obs.error_rate(args.service, window=args.window)
            p95 = obs.p95_latency(args.service, window=args.window)
            print(f"--- Derived Metrics for '{args.service}' (window: {args.window}s) ---")
            print(f"  Error Rate : {err['error_rate'] * 100:.2f}% ({err['error_count']}/{err['total_requests']}) [Breached: {err['breached']}]")
            print(f"  Evidence   : {err['evidence_ids']}")
            print(f"  Latency p95: {p95['p95_ms']:.1f}ms (p50: {p95['p50_ms']:.1f}ms, max: {p95['max_ms']:.1f}ms) [Breached: {p95['breached']}]")
            print(f"  Evidence   : {p95['evidence_ids']}")
        elif args.command == "poll":
            res = obs.poll_once()
            print("Poll completed:")
            for s, d in res.items():
                print(f"  {s:<12}: health={d['health']}, samples_collected={d['samples']}")
        elif args.command == "collect":
            print(f"Starting metric collector (interval: {args.interval}s)... Press Ctrl+C to stop.")
            obs.start_background_collector(interval_seconds=args.interval)
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                print("\nStopping metric collector...")
                obs.stop_background_collector()
                print("Stopped.")
        elif args.command == "evidence":
            res = obs.get_evidence(args.id)
            if res:
                print(json.dumps(res, indent=2))
            else:
                print(f"Evidence ID '{args.id}' not found.", file=sys.stderr)
                return 1
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
