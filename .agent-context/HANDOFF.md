# Current Handoff

## Last updated
2026-11-21

## Current task
Phase 2 (Supervisor and Chaos Injection) complete. Next: Phase 3 (Observability Layer).

## Verified state
- Python 3.13 venv at `.venv` with all runtime and test dependencies installed.
- Microservices (`gateway`, `orders`, `payments`, `inventory` on ports 8000–8003) operational with health, metrics, logging, and chaos middleware.
- Supervisor (`sre/supervisor.py`):
  - Service registry validation against `config/dependencies.yaml`.
  - Process lifecycle control: `start_service`, `stop_service`, `restart_service`, `start_all`, `stop_all`, `status`.
  - State tracking in `data/supervisor_state.json`.
  - Audit change logging to `data/changes.jsonl` with sequential `CHG-x` evidence IDs and timestamps.
- Chaos CLI & Engine (`sre/chaos.py`):
  - 8 failure injectors tested and working:
    1. `crash`: `kill -9` on service PID.
    2. `latency`: HTTP `POST /chaos` with `latency_ms`.
    3. `error_burst`: HTTP `POST /chaos` with `error_rate`.
    4. `memory_leak`: HTTP `POST /chaos` with `leak_mb`.
    5. `cpu_hog`: HTTP `POST /chaos` with `cpu_burn`.
    6. `bad_deploy`: Switch `VERSION` to `2.0.0`, restart service, causing upstream 500s.
    7. `dependency_down`: Stop downstream dependency, causing cascades.
    8. `db_lock`: Spawn background process holding SQLite `BEGIN EXCLUSIVE;` on `data/inventory.db`.
  - Clear / reset: `clear_all()` and `clear_service(svc)` restores `VERSION` to 1.0.0, resets chaos HTTP, releases SQLite locks, and restarts stopped services.
  - Hidden random selector: `chaos.py random` picks and executes one of the 8 failures.
- Tests & Verification:
  - `.venv/bin/pytest -v` -> 35 passed (11 LLM + 11 services + 4 supervisor + 9 chaos tests).
  - `.venv/bin/python tests/verify_phase2.py` -> live end-to-end execution of all 8 failure injections, supervisor actions, audit change log verification, and clean reset.

## Changes made by the last agent
- Implemented `sre/supervisor.py`: `Supervisor`, `ServiceInfo`, CLI commands (`status`, `start`, `stop`, `restart`, `start-all`, `stop-all`).
- Implemented `sre/chaos.py`: `ChaosEngine`, CLI commands for all 8 failure injectors, `clear`, and `random`.
- Implemented `tests/test_supervisor.py` and `tests/test_chaos.py` (13 pytest tests).
- Implemented `tests/verify_phase2.py` live process verification script.

## Tests and results
- `pytest -v`: 35 passed.
- `verify_phase2.py`: All 12 steps passed (start all, 8 failure modes, clear all, random failure, audit change log verification, stop all).

## Known issues / blockers
- None.

## Exact next step
Implement Phase 3: Observability Layer:
- `sre/observability.py`:
  - Metric collector: poll `/metrics` and `/health` every 5 seconds into SQLite (`data/observability.db`).
  - Log reader: parse JSON log files from `logs/*.log` with service/time/level/trace_id filters.
  - Dependency lookup: upstream and downstream relationships from `config/dependencies.yaml`.
  - Change reader: query `data/changes.jsonl`.
  - Derived calculations: `error_rate(svc, window)`, `p95_latency(svc, window)`, `check_health()`.
  - Evidence IDs: stable IDs for every returned log (`LOG-x`), metric (`MET-x`), and change (`CHG-x`).
