# Current Handoff

## Last updated
2026-11-21

## Current task
Phase 6 (Fix Pipeline and Approval Flow) complete. Next: Phase 7 (Postmortem and Dashboard).

## Verified state
- Python 3.12/3.13 venv at `.venv` with all runtime and test dependencies installed.
- Microservices (`gateway`, `orders`, `payments`, `inventory` on ports 8000–8003) operational with health, metrics, logging, and chaos middleware.
- Supervisor (`sre/supervisor.py`) managing process lifecycle and changes log.
- Chaos Engine (`sre/chaos.py`) with 8 failure modes, clear/reset, and random selector.
- Observability Layer (`sre/observability.py`) with metrics/logs/deps/changes/evidence IDs.
- Incident Detector (`sre/detector.py`) with plain-code SLO rules, T0/TTD, dedup, auto-close.
- Agent Core (`sre/agent.py` + `sre/tools.py`):
  - Allowlisted read-only tools: `get_logs`, `get_metrics`, `get_deps`, `get_changes`,
    `get_health`, `get_evidence` (argument validation + truncation).
  - `investigate()` / `step()` LLM tool-calling loop capped at 12 steps.
  - System prompt: earliest anomaly, dependency walk upstream, changes-first,
    ≥2 independent evidence citations (LOG/MET/CHG), confidence honesty.
  - Structured JSON final answer: `root_cause_service`, `root_cause`, `evidence`,
    `causal_chain`, `confidence`, `proposed_fix`, `mitigation_vs_cure`.
  - Per-incident provider failover (fresh history on backup; no mixed tool traces).
  - Records T1 and `time_to_diagnosis_seconds = T1 - T0` on incident metadata +
    `agent_diagnosis` audit change.
  - CLI: `python -m sre.agent investigate [INC-id]` / `prompt`.
- Fix Pipeline (`sre/fixer.py`):
  - Code-enforced `auto_fix`, `ask_first`, `never_do`, and `unknown` tiers.
  - Persistent approval records in SQLite with approve, deny, timeout, and cancel paths.
  - ≤2 restarts per service in 10 minutes; circuit trips propose rollback approval.
  - Pre-fix snapshots and post-fix health/error-rate/p95 verification; recovery resolves incidents.
  - Never-do prompt-injection actions and out-of-app edits are blocked and audit logged.
- Tests & Verification:
  - `.venv/bin/pytest -q` -> 74 passed.
  - `.venv/bin/python tests/verify_phase5.py` -> cascade crash diagnosed as payments,
    T1 recorded, failover to backup provider verified.
  - `.venv/bin/python tests/verify_phase6.py` -> crash restart, never-do blocks,
    bad-deploy rollback approval, and circuit-breaker approval passed.

## Changes made by the last agent
- Implemented `sre/tools.py`: ToolRegistry allowlist with OpenAI function schemas.
- Implemented `sre/agent.py`: Agent, Diagnosis, investigate/step loop, T1 recording, CLI.
- Implemented `tests/test_agent.py` (10 unit tests with mocked LLM).
- Implemented `tests/verify_phase5.py` (live multi-process verification with scripted LLM).
- Implemented `sre/fixer.py`: tier checks, persisted approvals, snapshots, apply actions,
  recovery verification, incident resolution/escalation, and circuit breaker.
- Implemented `tests/test_fixer.py` (12 unit tests).
- Implemented `tests/verify_phase6.py` (live Phase 6 verification).

## Tests and results
- `pytest -q`: 74 passed.
- `verify_phase5.py`: Passed all stages (cascade root cause, T1, failover).
- `verify_phase6.py`: Passed all stages (crash, never-do block, rollback, circuit breaker).

## Known issues / blockers
- None. Live LLM diagnosis against a real primary/backup provider still depends on
  configured API keys / Ollama; unit + verify cover the loop with mocks.

## Exact next step
Implement Phase 7: Postmortem and Dashboard:
- Generate evidence-backed incident postmortems.
- Build the dashboard view over incidents, evidence, fixes, approvals, and timelines.
