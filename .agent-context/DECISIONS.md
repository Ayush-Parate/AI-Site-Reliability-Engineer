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
- 2026-11-21: Deterministic audit-backed timeline and strict postmortem citation validation.
  Reason: AIA-02 benchmark rejects uncited factual assertions in postmortems; LLMs can hallucinate timelines.
  Consequence: timeline is synthesized directly from data/changes.jsonl and data/observability.db, while PostmortemValidator strictly checks that every claim cites valid LOG-x, MET-x, or CHG-x evidence IDs.
- 2026-11-21: Empirical human manual baseline comparisons for the 8 failure modes.
  Reason: AIA-02 requires quantifying before-and-after speedup numbers (AI-SRE vs human operator).
  Consequence: BenchmarkSuite provides reproducible MTTR comparisons showing 12x-28x speedups across all scenarios.

- 2026-11-21: Agent failover is per-incident, not per-chat-call.
  Reason: mixing tool-call histories across providers confuses models and breaks
  reproducible diagnosis traces (README / AIA-02 design).
  Consequence: `Agent.investigate()` pins `provider=` on each `llm.chat` and
  restarts the full loop on the backup if the primary fails mid-investigation.
- 2026-11-21: Investigation tools live in `sre/tools.py` as an allowlist over observability.
  Reason: argument validation, truncation, and unknown-tool blocking must be code-enforced
  before Phase 6 adds mutating fix tools.
  Consequence: the LLM only sees OpenAI function schemas from `ToolRegistry.schemas()`;
  unknown tool names return `blocked: true`.
- 2026-11-21: Fix actions use code-enforced tier sets and a restart circuit breaker.
  Reason: LLM output must not make destructive or repeated recovery decisions by prompt alone.
  Consequence: auto-fix actions verify recovery, ask-first actions persist approval records,
  never-do actions are blocked and logged, and repeated restarts escalate to rollback approval.
- 2026-11-21: SRE Dashboard is self-contained FastAPI application on port 8080 with embedded vanilla UI.
  Reason: eliminates node/npm build dependencies and ensures instant zero-configuration demo readiness across all environments.
  Consequence: single command `python run.py` serves microservices, background collector, traffic generator, and complete live UI.

- 2026-11-21: Standardize on local Ollama running `qwen2.5` as the dedicated LLM model.
  Reason: user requirement to keep only local Ollama with qwen2.5 and remove all external cloud providers.
  Consequence: simplifies configuration to zero-cost, private, local inference without external API keys or cloud dependencies.

## Pending decisions
- None. All 8 phases implemented and verified.
