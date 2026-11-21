# Task Tracker

## Current phase
Phase 0: Project Setup — COMPLETE.

## Current task
Phase 0 delivered. Next is Phase 1: Demo Microservices App.

## Completed
- [x] Inspect repository structure.
- [x] Create repo structure (`services/*`, `sre/*` + `dashboard/`, `config/`,
      `logs/`, `data/`, `reports/`, `tests/`).
- [x] Create Python 3.12 venv (`.venv`) and pin deps in `requirements.txt`.
- [x] Add `.gitignore` (`.env`, `.venv`, logs/data/reports) and `.env.example`.
- [x] Implement `sre/llm.py`: OpenAI-compatible `LLM` class + `PROVIDERS` +
      primary/backup failover.
- [x] Add `run.py` single entry point (status + `--test-llm`).
- [x] Add minimal `config/dependencies.yaml` and `config/slo.yaml`.
- [x] Add `tests/test_llm.py`; verify `run.py` and 11 tests pass.

## In progress
- None recorded.

## Blocked
- Live `--test-llm` against external live endpoints needs credentials in `.env` (e.g. `GEMINI_API_KEY` or `OPENAI_API_KEY`) or local Ollama running. (Unit tests mock provider calls and all 11 pass).

## Next task
Phase 1: build 4 FastAPI services (ports 8000-8003) with `/health`, `/metrics`,
structured JSON logs, trace_id propagation, per-service versions, chaos
middleware (`POST /chaos`), and `load.py`.

## Handoff log
- 2026-11-21: Implemented Phase 0. Files: `.gitignore`, `.env.example`,
  `requirements.txt`, `run.py`, `sre/__init__.py`, `sre/llm.py`,
  `config/dependencies.yaml`, `config/slo.yaml`, `tests/__init__.py`,
  `tests/test_llm.py`, service/dashboard/logs/data/reports `.gitkeep` dirs. Verified:
  `.venv/bin/python run.py` exits 0; `pytest -q` -> 11 passed; no secrets in
  tracked files; `.venv`/`.env`/logs/data/reports gitignored. Next: Phase 1.