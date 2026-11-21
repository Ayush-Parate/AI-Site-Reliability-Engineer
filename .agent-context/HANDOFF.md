# Current Handoff

## Last updated
2026-11-21

## Current task
Phase 4 (Incident Detector) complete. Next: Phase 5 (Agent Core).

## Verified state
- Python 3.14 venv at `.venv` with all runtime and test dependencies installed.
- Microservices (`gateway`, `orders`, `payments`, `inventory` on ports 8000–8003) operational with health, metrics, logging, and chaos middleware.
- Supervisor (`sre/supervisor.py`) managing process lifecycle and changes log.
- Chaos Engine (`sre/chaos.py`) with 8 failure modes, clear/reset, and random selector.
- Observability Layer (`sre/observability.py`):
  - Metric Collector: polls `/metrics` (Prometheus) and `/health` into SQLite `data/observability.db`.
  - Log Reader (`get_logs`): reads `logs/*.log` with `service`, `since`, `level`, and `trace_id` filters.
  - Dependency Graph (`get_deps`): bidirectional upstream/downstream lookups.
  - Change Query (`get_changes`): queries `data/changes.jsonl`.
  - Derived Metrics: `error_rate(svc, window)` and `p95_latency(svc, window)`.
  - Stable Evidence IDs: citations for `LOG-x`, `MET-x`, `CHG-x` via `get_evidence(id)`.
- Incident Detector (`sre/detector.py`):
  - Plain-code detection rules against `config/slo.yaml`:
    1. Consecutive health check failures (3 in a row)
    2. Error rate > 5% over 30s
    3. p95 latency > 2x baseline or > 2000ms
    4. Memory growth trend > 50MB in 60s
  - Accurate T0 recording: timestamp of earliest anomaly, not detection timestamp.
  - Time-to-detect (TTD) calculation (`created_at - T0`).
  - Incident deduplication: further breaches attach to existing incident rather than opening new ones.
  - Status lifecycle: `open` -> `diagnosing` -> `mitigating` -> `resolved` / `escalated`.
  - Auto-close resolution: closes incident when healthy for `recover_after_seconds`.
  - SQLite persistence: `incidents` table in `data/observability.db`.
  - Full CLI: `status`, `list`, `check`, `show`, `watch`.
- Tests & Verification:
  - `.venv/bin/pytest -v` -> 50 passed (11 LLM + 11 services + 4 supervisor + 9 chaos + 8 observability + 7 detector tests).
  - `.venv/bin/python tests/verify_phase4.py` -> live end-to-end verification of zero false alarms on healthy baseline, incident opening on failure, deduplication on cascading failure, status updates, auto-close on recovery, and SQLite persistence.

## Changes made by the last agent
- Implemented `sre/detector.py`: `Breach`, `Incident`, `IncidentDetector` with rules, T0/TTD calculations, deduplication, auto-resolution, SQLite persistence, and CLI.
- Implemented `tests/test_detector.py` (7 pytest unit tests).
- Implemented `tests/verify_phase4.py` (live multi-process verification script).

## Tests and results
- `pytest -v`: 50 passed.
- `verify_phase4.py`: Passed all stages.

## Known issues / blockers
- None.

## Exact next step
Implement Phase 5: Agent Core (`sre/agent.py`):
- `step()` loop using OpenAI-compatible client wrapper from `sre/llm.py` with provider failover.
- System prompt enforcing AIA-02 principles:
  - Earliest anomaly (walk upstream from symptom).
  - Check changes first.
  - Two independent pieces of evidence.
  - Cite evidence IDs (`LOG-x`, `MET-x`, `CHG-x`).
- Structured JSON final answer schema: `root_cause_service`, `root_cause`, `evidence`, `causal_chain`, `confidence`, `proposed_fix`, `mitigation_vs_cure`.
- Cap at 12 steps.
- Record T1 (diagnosis time) and calculate Time-to-Diagnosis (`T1 - T0`).
