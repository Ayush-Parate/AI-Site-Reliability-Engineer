# Technical Decisions

Record confirmed decisions only. Include date, decision, reason, and consequences.

## Decisions
- 2026-11-21: Use Python 3.13 (Homebrew `python@3.13`) for the `.venv`.
  Reason: System Python was 3.9.6, project requires 3.11+.
  Consequence: all commands use `.venv/bin/python`; CI/other machines should use 3.11+.
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
- 2026-11-21: Use dedicated `CollectorRegistry` per service in `ServiceMetrics`.
  Reason: prevent duplicate metric registration collisions when multiple services
  are tested or run in the same Python process.
  Consequence: clean metric isolation per service instance.
- 2026-11-21: Microservice versions controlled via `services/<service>/VERSION` file
  with environment variable override (`<SERVICE>_VERSION`).
  Reason: allows file-based version bumps for bad deploy simulation and rollbacks
  as specified in Phase 1 & 2 plans.
  Consequence: supervisor and rollback pipeline can simulate bad deploy by writing
  to `VERSION` or setting the env var.
- 2026-11-21: Use strict SQLite connection timeout (1.0s) in `inventory`.
  Reason: Phase 2 requires simulating database locks (`BEGIN EXCLUSIVE`) which must
  fail fast with `OperationalError: database is locked` rather than hanging indefinitely.
  Consequence: inventory accurately raises 500 when database lock chaos is injected.
- 2026-11-21: Change log format in `data/changes.jsonl` with sequential `CHG-<n>` evidence IDs.
  Reason: Phase 3 observability and Phase 5 agent need citeable change IDs for hypotheses.
  Consequence: all lifecycle actions from supervisor and failure injectors from chaos append
  with standard schema (`id, ts, action, service, author, details`).
- 2026-11-21: Background process holding `BEGIN EXCLUSIVE;` on SQLite for DB lock chaos.
  Reason: keeps SQLite write lock held across separate processes until duration expires or
  released via `release_db_lock()`.
  Consequence: provides reliable, repeatable DB lock testing for live judging and rehearsal.
- 2026-11-21: Supervisor terminates unhealthy processes prior to restarting/starting on the same port.
  Reason: prevents port binding collisions ([Errno 48] Address already in use) when attempting to start an unhealthy service whose PID is still alive.
  Consequence: clean process recovery during chaos clearing and automated restarts.
- 2026-11-21: Observability storage schema in SQLite (`data/observability.db`).
  Reason: fast indexed queries for time series metrics (`metrics`), health check history (`health_checks`),
  and deduplicated log indexing (`log_index` with UNIQUE(service, file_line)).
  Consequence: enables sub-millisecond lookups for derived metrics, window filtering, and exact citation lookups.
- 2026-11-21: Unified Stable Evidence ID format (`LOG-<id>`, `MET-<id>`, `CHG-<id>`).
  Reason: AIA-02 requires evidence-backed claims in reasoning and postmortem.
  Consequence: every log line, metric sample, and audit change has a unique, deterministic citation ID
  queryable via `get_evidence(id)`.
- 2026-11-21: Derived metrics combine log ground truth and Prometheus counters.
  Reason: structured logs provide immediate per-request status and latency, while Prometheus counters
  provide cumulative rates.
  Consequence: derived metrics `error_rate` and `p95_latency` reflect anomalies immediately without polling lag.
- 2026-11-21: Incident T0 computed from earliest anomaly event rather than detection time.
  Reason: AIA-02 benchmark measures Time-to-Detect (TTD = T_detect - T0) and Time-to-Diagnosis (T_diag - T0).
  Consequence: T0 captures the exact timestamp of the first failed health check, 5xx error log, or latency spike.
- 2026-11-21: Incident deduplication by attaching cascading breaches to active incident.
  Reason: a single root cause (e.g. database lock or crashed dependency) triggers cascading failures across callers.
  Consequence: all affected services and evidence IDs aggregate into one incident, preventing noisy alarm storms.

## Pending decisions
- Which concrete primary/backup LLM providers to target for the demo (depends on
  available API keys; default in code is openai primary, ollama backup).
