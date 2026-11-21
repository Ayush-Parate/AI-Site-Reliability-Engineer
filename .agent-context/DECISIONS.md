# Technical Decisions

Record confirmed decisions only. Include date, decision, reason, and consequences.

## Decisions
- 2026-11-21: Use Python 3.12 (Homebrew `python@3.12`) for the `.venv`.
  Reason: Phase 0 requires 3.11+ but the machine only had system Python 3.9.6.
  Consequence: all commands use `.venv/bin/python`; CI/other machines should use
  3.11+.
- 2026-11-21: Model the LLM layer on the OpenAI-compatible chat-completions
  interface via the `openai` SDK with a custom `base_url` per provider.
  Reason: one code path covers openai/gemini/groq/openrouter/ollama; matches the
  plan's "OpenAI-compatible interface" and simplifies failover.
  Consequence: providers needing non-standard params may need per-provider
  tweaks later; failover re-runs the whole request on the next provider.
- 2026-11-21: Runtime artifacts (`logs/`, `data/`, `reports/`, `*.db`) are
  gitignored; only `.env.example` is committed.
  Reason: secrets and generated state must never enter git history (Phase 0 exit
  criterion).
  Consequence: fresh clones start with empty runtime dirs (created by code/run.py
  in later phases).

## Pending decisions
- Which concrete primary/backup LLM providers to target for the demo (depends on
  available API keys; default in code is openai primary, ollama backup).
- Database engine details for observability (SQLite assumed by the plan).