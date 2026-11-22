# Task Tracker

## Current phase
Phase 8: SRE Operational Dashboard, Testing and Demo Preparation — COMPLETE.

## Current task
All 8 Phases implemented and verified. Project is ready for live demonstration and judging.

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
- [x] Implement `sre/dashboard/app.py`:
      - Real-time dark-mode single-page UI running on FastAPI / Uvicorn at `http://localhost:8080/`.
      - Live service health cards for all 4 microservices with error rates, p95 latencies, versions, and PIDs.
      - Active incident panel with trigger rules, T0 anomaly timestamp, and live timeline with evidence links.
      - Interactive human approval flow with one-click Approve / Deny buttons (`POST /api/approvals/{id}/approve` and `/deny`).
      - Evidence drawer/modal displaying full telemetry citations (`LOG-x`, `MET-x`, `CHG-x`).
      - SRE Metrics panel with MTTR and 8-scenario benchmark comparison table.
      - Postmortem report viewer serving generated markdown reports from `reports/INC-*.md`.
      - Chaos injection control board and reset controls on the UI for live demo and judging.
- [x] Add `tests/test_dashboard.py` (9 unit tests) and `tests/verify_phase8.py` (end-to-end multi-process verification script).
- [x] Implement Telegram Incident & Approval Bot (`sre/telegram_bot.py`):
      - `python-telegram-bot` v20+ with `asyncio`.
      - Background proactive polling worker alerting on-call chat of new incidents and approvals.
      - Interactive inline keyboard buttons: `[ ✅ Approve ]` and `[ ❌ Deny ]` with in-place message updates.
      - Slash commands: `/status`, `/incidents`, `/approvals`, `/postmortem <id>`, `/restart <svc>`, `/help`.
      - Integrated CLI in `run.py` (`--telegram-bot` or automatic launch on `TELEGRAM_BOT_TOKEN`).
- [x] Add `tests/test_telegram_bot.py` (17 unit tests).
- [x] Full test suite (107 tests across 11 test modules) passing.
- [x] Modular, Categorized Incident Reporting System:
      - 8 standardized report sections (`sre/reports/models.py`): Executive Summary, Incident Impact, Root Cause Analysis, Evidence & Observability, Incident Timeline, Remediation Actions, Recovery Verification, Prevention & Recommendations.
      - Modular report storage & on-the-fly migration (`sre/reports/storage.py`): `reports/<INC-ID>/` with `metadata.json`, `<section>.json`, and backward-compatible `reports/<INC-ID>.md`.
      - Business service (`sre/reports/service.py`) for report generation, evidence validation, and category retrieval.
      - REST API endpoints:
        - `GET /api/reports`: lightweight paginated list with `status`, `severity`, and `search` filters.
        - `GET /api/reports/{id}`: metadata, sections index, and markdown.
        - `GET /api/reports/{id}/sections`: available section metadata index.
        - `GET /api/reports/{id}/sections/{section_id}`: isolated section payload.
      - Dashboard UI:
        - Lightweight report card list with search bar, status filter, severity filter, and pagination.
        - Split-layout Modular Report Viewer modal with category navigation sidebar, breadcrumb, lazy section loading, client-side caching, raw markdown toggle, and export.
        - Clickable evidence citations (`LOG-x`, `MET-x`, `CHG-x`) triggering telemetry inspection modal.
      - Full unit test suite (`tests/test_modular_reports.py` - 8 tests).
      - Multi-modal Markdown report download suite:
        - 1. Combined "Download .md": downloads full postmortem across all 8 categories (unchanged).
        - 2. Individual Category Downloads: dedicated download buttons in category navigation and section views downloading `{incident_id}_{category}_{date}.md` for each of the 8 sections independently via `/api/reports/{id}/sections/{sec_id}/download` and `/export`.
        - 3. "Download All (ZIP)": bundles all 8 individual category Markdown files plus the full postmortem into `{incident_id}_Postmortem_All_Sections_{date}.zip` via `/api/reports/{id}/zip`.
      - All 115 pytest unit tests pass cleanly.

## In progress
- None.

## Blocked
- None.

## Next task
- Live presentation / demo execution.

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
- 2026-11-21: Implemented Phase 8. `sre/dashboard/app.py` delivers interactive SRE operational web dashboard
  at `http://localhost:8080/` with live service health cards, active incident panel, human approval flow,
  evidence drawer, SRE metrics benchmarks, postmortem viewer, and chaos controls. Verified: 90 pytest tests
  pass. Live single-command runner updated (`python run.py`). All phases complete.
- 2026-11-22: Refactored Incident Postmortems into a Modular, Categorized Reporting System. Created
  `sre/reports/` with models, storage, and service. Added paginated, filtered report listing and lazy section
  loading to `/api/reports` backend. Integrated split-layout category viewer and evidence linking in the dashboard UI.
- 2026-11-22: Resolved evidence modal hanging and report modal rendering:
  - Eliminated rogue orphan background monitor processes (3x `observability.py`, 3x `detector.py`) causing SQLite database lock contention and hanging frontend `fetch()` calls.
  - Added startup cleanup in `run.py` to kill any stale orphan background monitors prior to starting services.
  - Hardened `viewEvidence()` in `sre/dashboard/app.py`: added `AbortController` timeout (8s), error message reporting, and URI encoding so inspection modals never hang indefinitely on "Loading telemetry...".
  - Hardened `openModularReport()` in `sre/dashboard/app.py`: immediately pre-populates category navigation items and displays active loading spinners so the report modal body never opens as a blank dark screen.
  - Verified 117 passing unit tests across 12 test modules.
