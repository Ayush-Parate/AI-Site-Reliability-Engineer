# Task Tracker

## Current phase
Phase 7: Timeline, Postmortem and Metrics — COMPLETE.

## Current task
Phase 7 delivered. Next is Phase 8: Dashboard, Testing and Demo Preparation.

## Completed
- [x] Inspect repository structure.
- [x] Create repo structure (`services/*`, `sre/*` + `dashboard/`, `config/`,
      `logs/`, `data/`, `reports/`, `tests/`).
- [x] Create Python 3.13 venv (`.venv`) and pin deps in `requirements.txt`.
- [x] Add `.gitignore` (`.env`, `.venv`, logs/data/reports) and `.env.example`.
- [x] Implement `sre/llm.py`: OpenAI-compatible `LLM` class + `PROVIDERS` +
      primary/backup failover.
- [x] Add `run.py` single entry point (status + `--test-llm`).
- [x] Add minimal `config/dependencies.yaml` and `config/slo.yaml`.
- [x] Add `tests/test_llm.py`; verify `run.py` and 11 tests pass.
- [x] Build 4 FastAPI microservices under `services/` (ports 8000–8003):
      `gateway` (:8000), `orders` (:8001), `payments` (:8002), `inventory` (:8003).
- [x] Expose `GET /health` and `GET /metrics` (Prometheus) on all 4 services.
- [x] Implement structured JSON logging to `logs/<service>.log` with required schema
      (`ts`, `level`, `service`, `trace_id`, `msg`, `latency_ms`, `status`).
- [x] Implement `x-trace-id` propagation across call chains (gateway -> orders -> payments/inventory).
- [x] Add per-service `VERSION` files and bad deploy simulation (v2 breaking behavior).
- [x] Implement dynamic chaos middleware on all services (`POST /chaos`, `GET /chaos`, `POST /chaos/reset`)
      supporting `latency_ms`, `error_rate`, `leak_mb`, `cpu_burn`, and `unhealthy`.
- [x] Implement SQLite inventory database (`data/inventory.db`) with item seeding and stock reservations.
- [x] Build steady traffic generator `load.py` with configurable rate, duration, count, and percentiles.
- [x] Add `tests/test_services.py` and `tests/verify_phase1.py`; verify 22 passing tests and live e2e run.
- [x] Implement `sre/supervisor.py`: track PID, start time, version, status; start, stop, restart,
      start_all, stop_all, status; service registry validation; audit change logging (`data/changes.jsonl`).
- [x] Implement `sre/chaos.py` CLI supporting 8 failure types:
      1. Crash (kill -9 on PID)
      2. Latency (POST /chaos latency_ms)
      3. Error burst (POST /chaos error_rate)
      4. Memory leak (POST /chaos leak_mb)
      5. CPU hog (POST /chaos cpu_burn)
      6. Bad deploy (switch service to v2 and restart)
      7. Dependency down (stop downstream service)
      8. DB lock (hold SQLite exclusive write lock on data/inventory.db)
- [x] Implement clear/reset across all services and release DB locks (`sre/chaos.py clear`).
- [x] Implement hidden random failure selector (`sre/chaos.py random`).
- [x] Add `tests/test_supervisor.py` and `tests/test_chaos.py` (13 tests) and live verification script `tests/verify_phase2.py`.
- [x] Implement `sre/observability.py`:
      - Metric collector: poll `/metrics` and `/health` every 5 seconds into SQLite (`data/observability.db`).
      - Log reader: query and filter structured JSON lines from `logs/*.log` (`get_logs(service, since, level, trace_id)`).
      - Dependency graph: bidirectional upstream and downstream dependency queries (`get_deps(service)`).
      - Change query: query audit changes from `data/changes.jsonl` (`get_changes(since, service)`).
      - Derived metrics: calculations for `error_rate(svc, window)`, `p95_latency(svc, window)`, and `check_health(svc)`.
      - Stable citation IDs: assign and lookup citations for logs (`LOG-x`), metrics (`MET-x`), and changes (`CHG-x`) via `get_evidence(id)`.
      - Daemon background collector and CLI commands (`health`, `deps`, `changes`, `logs`, `metrics`, `derived`, `poll`, `collect`, `evidence`).
- [x] Add `tests/test_observability.py` (8 pytest tests) and live verification script `tests/verify_phase3.py`.
- [x] Implement `sre/detector.py`:
      - Plain code detection rules reading `config/slo.yaml` without LLM:
        1. Consecutive health check failures (3 in a row)
        2. Error rate > 5% over 30s
        3. p95 latency > 2x baseline or > 2000ms SLO limit
        4. Memory growth trend > 50MB in 60s
      - Accurate T0 anomaly timestamp recording and time-to-detect (TTD) computation.
      - Incident deduplication (further breaches attach to open incident).
      - Incident lifecycle: statuses `open`, `diagnosing`, `mitigating`, `resolved`, `escalated`.
      - Auto-close when healthy for `recover_after_seconds`.
      - SQLite persistence (`incidents` table in `data/observability.db`).
      - Audit change logging to `data/changes.jsonl` on open and resolve.
      - Background detector daemon and CLI (`status`, `list`, `check`, `show`, `watch`).
- [x] Add `tests/test_detector.py` (7 pytest tests) and live verification script `tests/verify_phase4.py`.
- [x] Implement `sre/tools.py`: allowlisted read-only investigation tools with OpenAI function
      schemas, argument validation, truncation, and evidence IDs
      (`get_logs`, `get_metrics`, `get_deps`, `get_changes`, `get_health`, `get_evidence`).
- [x] Implement `sre/agent.py`:
      - `investigate()` with per-provider failover (fresh tool history on each provider).
      - `step()` LLM tool-calling turn.
      - System prompt enforcing earliest-anomaly, dependency walk, changes-first,
        two-evidence confirmation, and citation of LOG/MET/CHG IDs.
      - Cap at 12 steps with forced structured JSON final answer.
      - Final schema: `root_cause_service`, `root_cause`, `evidence`, `causal_chain`,
        `confidence`, `proposed_fix`, `mitigation_vs_cure`.
      - Record T1 and time-to-diagnosis (`T1 - T0`) on incident metadata + audit log.
      - CLI: `investigate`, `prompt`.
- [x] Add `tests/test_agent.py` (10 pytest tests) and live verification script `tests/verify_phase5.py`.
- [x] Implement `sre/fixer.py`: tiered fix pipeline with code-enforced auto-fix, ask-first,
      and never-do actions; approval persistence; circuit breaker; snapshot; apply; verify.
- [x] Add `tests/test_fixer.py` (12 pytest tests) and live verification script
      `tests/verify_phase6.py`.
- [x] Implement `sre/postmortem.py`:
      - `TimelineBuilder`: extracts and chronologically sorts audit changes (`data/changes.jsonl`),
        health checks, and log events with stable evidence links (`LOG-x`, `MET-x`, `CHG-x`).
      - `PostmortemValidator`: enforces evidence citations on all factual statements; rejects uncited claims.
      - `PostmortemGenerator`: LLM-assisted and deterministic report generator populating standard 8-section
        markdown postmortems saved to `reports/INC-<id>.md`.
      - SRE Metrics: computes TTD, TTDiag (`T1 - T0`), TTR, and comparison against manual baselines.
      - `BenchmarkSuite`: 8-scenario benchmark comparison table demonstrating 12x-28x MTTR improvements.
      - CLI commands: `timeline`, `generate`, `show`, `metrics`, `benchmark`.
- [x] Add `tests/test_postmortem.py` (7 pytest tests) and live verification script `tests/verify_phase7.py`.

## In progress
- None.

## Blocked
- None.

## Next task
Phase 8: Dashboard, Testing and Demo Preparation (live FastAPI dashboard, active incident board, approval buttons, scenario testing).

## Handoff log
- 2026-11-21: Implemented Phase 0. Setup, venv, requirements, provider wrapper `sre/llm.py`, `run.py`.
- 2026-11-21: Implemented Phase 1. 4 FastAPI services, logging, metrics, chaos middleware, `load.py`.
- 2026-11-21: Implemented Phase 2. `sre/supervisor.py` and `sre/chaos.py` CLI with 8 failure modes,
  audit change logging to `data/changes.jsonl` (`CHG-x`), reset capability, and hidden random selector.
- 2026-11-21: Implemented Phase 3. `sre/observability.py` with MetricCollector into SQLite (`data/observability.db`),
  LogReader with filtering and indexing from `logs/*.log`, Dependency Graph from `config/dependencies.yaml`,
  Change Query from `data/changes.jsonl`, Derived Metrics (`error_rate`, `p95_latency`, `check_health`),
  stable citation IDs (`LOG-x`, `MET-x`, `CHG-x`), and `get_evidence(id)`.
- 2026-11-21: Implemented Phase 4. `sre/detector.py` with plain-code rule evaluations against `config/slo.yaml`,
  accurate T0 anomaly recording, TTD calculation, incident deduplication, auto-close resolution timer,
  and SQLite persistence. Verified: 50 pytest unit tests pass; `tests/verify_phase4.py` passed live verification. Next: Phase 5.
- 2026-11-21: Implemented Phase 5. `sre/tools.py` allowlist + `sre/agent.py` investigation loop with
  provider failover, 12-step cap, structured JSON diagnosis, T1 / time-to-diagnosis. Verified:
  60 pytest tests pass; `tests/verify_phase5.py` passed. Next: Phase 6.
- 2026-11-21: Implemented Phase 6. `sre/fixer.py` provides tiered fix enforcement, persisted
  human approvals, circuit-breaking, snapshots, application, recovery verification, and escalation.
  Verified: 74 pytest tests pass; `tests/verify_phase6.py` passed live crash recovery, never-do
  blocking, bad-deploy rollback, and circuit-breaker approval flow.
- 2026-11-21: Implemented Phase 7. `sre/postmortem.py` with deterministic audit-backed timeline,
  strict citation validator, markdown postmortems in `reports/INC-<id>.md`, SRE metrics (TTD, TTDiag, TTR),
  and 8-scenario benchmark results table (18.3x average MTTR improvement). Verified: 81 pytest tests pass;
  `tests/verify_phase7.py` passed all live verification checks. Next: Phase 8.
