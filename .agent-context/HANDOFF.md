# Current Handoff

## Last updated
2026-11-21

## Current task
Phase 3 (Observability Layer) complete. Next: Phase 4 (Incident Detector).

## Verified state
- Python 3.14 venv at `.venv` with all runtime and test dependencies installed.
- Microservices (`gateway`, `orders`, `payments`, `inventory` on ports 8000–8003) operational with health, metrics, logging, and chaos middleware.
- Supervisor (`sre/supervisor.py`) managing process lifecycle and changes log.
- Chaos Engine (`sre/chaos.py`) with 8 failure modes, clear/reset, and random selector.
- Observability Layer (`sre/observability.py`):
  - Metric Collector: polls `/metrics` (Prometheus) and `/health` into SQLite `data/observability.db` (`metrics` and `health_checks` tables).
  - Background collector daemon thread polling every 5s.
  - Log Reader (`get_logs`): reads `logs/*.log`, indexes into `log_index` table, filters by `service`, `since`, `level`, and `trace_id`.
  - Dependency Graph (`get_deps`): queries bidirectional upstream/downstream dependencies from `config/dependencies.yaml`.
  - Change Query (`get_changes`): queries recent events from `data/changes.jsonl` filtered by `since` and `service`.
  - Derived Metrics: `error_rate(svc, window)` and `p95_latency(svc, window)` calculating SLO threshold breaches against `config/slo.yaml`, plus `check_health()` / `get_health()`.
  - Stable Evidence IDs: every item tagged with stable citations (`LOG-<id>`, `MET-<id>`, `CHG-<id>`), with lookup tool `get_evidence(id)`.
  - Full CLI interface: `python sre/observability.py health|deps|changes|logs|metrics|derived|poll|collect|evidence`.
- Tests & Verification:
  - `.venv/bin/pytest -v` -> 43 passed (11 LLM + 11 services + 4 supervisor + 9 chaos + 8 observability tests).
  - `.venv/bin/python tests/verify_phase3.py` -> live end-to-end execution of all 11 verification stages (traffic generation, metric polling, log reading, trace lookup, failure injection, change log query, derived metric breach detection, evidence ID lookups, and clean recovery).

## Changes made by the last agent
- Implemented `sre/observability.py`: `Observability`, `MetricCollector`, `DerivedMetricResult`, module-level tools (`get_logs`, `get_metrics`, `get_deps`, `get_changes`, `get_health`, `check_health`, `error_rate`, `p95_latency`, `get_evidence`), and CLI interface.
- Updated `services/common/metrics.py`: added `process_resident_memory_bytes` and `process_cpu_seconds_total` Gauges for process memory and CPU tracking.
- Updated `run.py`: added `--obs` flag for observability status and integrated background metric collector with `--services` / default run.
- Implemented `tests/test_observability.py` (8 pytest unit tests).
- Implemented `tests/verify_phase3.py` (11-stage live multi-process verification script).

## Tests and results
- `pytest -v`: 43 passed.
- `verify_phase3.py`: All 11 steps passed.

## Known issues / blockers
- None.

## Exact next step
Implement Phase 4: Incident Detector (`sre/detector.py`):
- Plain code detection rules reading `config/slo.yaml`:
  - 3 consecutive failed health checks
  - Error rate > 5% over 30s
  - p95 latency > 2x baseline or > 2000ms
  - Memory or CPU growth trend above threshold
- Incident opening: record incident ID, T0 (earliest anomaly timestamp, not detection timestamp), affected services, trigger rule.
- Incident deduplication: further breaches during open incident attach to it.
- Auto-close when healthy for 60 seconds.
- Persist incidents in SQLite (`data/observability.db`).
