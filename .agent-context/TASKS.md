# Task Tracker

## Current phase
Phase 2: Supervisor and Chaos Injection — COMPLETE.

## Current task
Phase 2 delivered. Next is Phase 3: Observability Layer.

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

## In progress
- None.

## Blocked
- None.

## Next task
Phase 3: Observability Layer (`sre/observability.py`: metric collector into SQLite, log parser,
dependency lookups, recent change lookups, derived metrics error_rate/p95, and evidence IDs like `LOG-1042` and `MET-77`).

## Handoff log
- 2026-11-21: Implemented Phase 0. Setup, venv, requirements, provider wrapper `sre/llm.py`, `run.py`.
- 2026-11-21: Implemented Phase 1. 4 FastAPI services, logging, metrics, chaos middleware, `load.py`.
- 2026-11-21: Implemented Phase 2. `sre/supervisor.py` and `sre/chaos.py` CLI with 8 failure modes,
  audit change logging to `data/changes.jsonl` (`CHG-x`), reset capability, and hidden random selector.
  Verified: 35 pytest unit tests pass; `tests/verify_phase2.py` verified all 8 injectors, supervisor state,
  change log records, and recovery. Next: Phase 3.
