# AI Site Reliability Engineer: Multi-Phase Implementation Plan (AIA-02)

This plan turns the AIA-02 problem statement into a build schedule. The agent watches a locally running microservices app, detects incidents, finds the root cause with evidence, applies safe fixes automatically, asks a human before risky ones, and writes a postmortem. Docker is not required: services run as plain processes under a small supervisor.

## 1. Overview

### Design principles

- **Code decides, the LLM reasons.** Detection, permission tiers, circuit breakers and verification are plain code. The LLM only diagnoses and writes.
- **Evidence or it did not happen.** Every claim in the timeline links to a log line, metric sample or change record.
- **Allowlist, never blocklist.** Only registered tools can run, and each validates its arguments.
- **Generalize, do not hardcode.** Judges will break things unrehearsed, so no "if X then Y" scripts.
- **One-command demo.** `python run.py` starts everything.

### Architecture

```
gateway :8000 --> orders :8001 --> payments :8002
                        \--> inventory :8003 --> sqlite

Supervisor (start/stop/restart)   Chaos CLI (inject failures)
        |                                  |
        v                                  v
  logs/*.log + /metrics  -->  Observability layer (SQLite)
                                      |
                                      v
                    Detector (rules) --> Incident opened (T0)
                                      |
                                      v
              Agent loop (LLM + tools, tier-gated)  --> Root cause (T1)
                                      |
                       Fix pipeline: tier > limit > snapshot > apply > verify
                                      |
                  Timeline + Postmortem + Dashboard (approve/deny)
```

### Phase summary

| Phase | Name | Deliverable | Time |
|---|---|---|---|
| 0 | Setup | Repo, env, LLM wrapper | 1 to 2 h |
| 1 | Demo app | 4 services with logs, metrics, health | 3 to 4 h |
| 2 | Supervisor and chaos | Restart tool, 6+ failure injectors | 2 to 3 h |
| 3 | Observability | Log parser, metric store, deps, changes | 3 to 4 h |
| 4 | Detector | Rule-based incident opening, T0 | 2 to 3 h |
| 5 | Agent core | Tool-calling loop, tiers, failover | 4 to 5 h |
| 6 | Fix and approval | Auto-fix pipeline, approval flow | 3 to 4 h |
| 7 | Timeline and postmortem | Evidence timeline, TTD, report | 3 to 4 h |
| 8 | Dashboard and demo | UI, tests, rehearsal, video | 4 to 6 h |

Total is roughly 25 to 35 hours. For a shorter hackathon, finish Phases 0 to 5 first (they satisfy the must-have), then add 6 to 8 in order of judging value: tiers, timeline, postmortem, dashboard.

---

## Phase 0: Project Setup

**Goal:** A clean repo where everything runs from one command.
**Estimated time:** 1 to 2 hours

### Tasks

- Create the repo structure below and a Python 3.11+ virtual environment.
- Pin dependencies in `requirements.txt` (fastapi, uvicorn, httpx, prometheus-client, openai, pyyaml, python-dotenv).
- Create `.env.example` with LLM keys and add `.env` to `.gitignore`.
- Write `llm.py` with one LLM class using the OpenAI-compatible interface, and a `PROVIDERS` list for primary and backup.
- Create `run.py` as the single entry point.

```
ai-sre/
  services/          gateway/ orders/ payments/ inventory/
  sre/
    supervisor.py    chaos.py      observability.py
    detector.py      agent.py      tools.py
    fixer.py         postmortem.py llm.py
    dashboard/       (FastAPI + simple HTML)
  config/
    dependencies.yaml   slo.yaml
  logs/  data/  reports/
  tests/
  run.py  requirements.txt  README.md  .env.example
```

### Exit criteria

- [ ] `python run.py` runs without error (even if it only prints hello).
- [ ] A test call to each configured LLM provider returns text.
- [ ] No API key appears in git history.

---

## Phase 1: Demo Microservices App

**Goal:** A realistic target app with call chains, so cascading failures are possible.
**Estimated time:** 3 to 4 hours

### Tasks

- Build 4 small FastAPI services on ports 8000 to 8003 following the call graph above.
- Every service exposes `GET /health` and `GET /metrics` (request count, error count, latency histogram).
- Every service writes structured JSON logs to `logs/<service>.log` with `ts`, `level`, `service`, `trace_id`, `msg`, `latency_ms`, `status`.
- Propagate a `trace_id` header through every downstream call so one request can be followed across services.
- Give each service a version (v1 and v2 folders or a VERSION file) so a bad deploy can be simulated and rolled back.
- Add chaos middleware to each service, controlled by `POST /chaos` (`latency_ms`, `error_rate`, `leak_mb`, `cpu_burn`).
- Write a small load generator (`load.py`) that sends steady traffic so metrics are never empty.

### Exit criteria

- [ ] `curl` to the gateway completes a full order through all services.
- [ ] Logs show the same `trace_id` in every service.
- [ ] Load generator runs continuously with near-zero errors.

---

## Phase 2: Supervisor and Chaos Injection

**Goal:** Tools to start, stop and restart services, plus a way to break them on demand.
**Estimated time:** 2 to 3 hours

### Supervisor

- Track each service PID, start time, version and status.
- Expose `start`, `stop`, `restart(service)` and `status()`.
- Write every start, stop and restart to a change log (`data/changes.jsonl`). The agent later reads this as recent changes.
- Validate service names against the registry before any action.

### Chaos CLI (`chaos.py`)

| Failure | Injection method | Expected symptom |
|---|---|---|
| Crash | `kill -9` on the PID | Health fails, upstream 5xx |
| Latency | `POST /chaos latency_ms` | p95 spikes, upstream timeouts |
| Error burst | `POST /chaos error_rate` | 5xx rate climbs |
| Memory leak | `POST /chaos leak_mb` | RSS grows, eventual slowdown |
| CPU hog | Busy-loop thread | High latency, high CPU |
| Bad deploy | Switch service to v2 (buggy) | Errors begin right after change |
| Dependency down | Kill the downstream service | Cascade to upstream callers |
| DB lock | Hold SQLite write lock | Inventory timeouts |

### Exit criteria

- [ ] Each failure type can be injected and cleared with one command.
- [ ] Every change is recorded in the change log with a timestamp.
- [ ] A hidden chaos script exists that picks a random failure, to practice for live judging.

---

## Phase 3: Observability Layer

**Goal:** One place where the agent can query logs, metrics, dependencies and changes.
**Estimated time:** 3 to 4 hours

### Tasks

- **Metric collector:** poll each `/metrics` and `/health` every 5 seconds into SQLite (`service`, `metric`, `value`, `ts`).
- **Log reader:** parse the JSON log files and support filtering by service, time window, level and `trace_id`.
- Load `config/dependencies.yaml` (who calls whom) and expose upstream and downstream lookups.
- Expose recent changes from `data/changes.jsonl`.
- Add derived functions: `error_rate(svc, window)`, `p95_latency(svc, window)`, `check_health(svc)`.

### Read-only tools exposed to the agent

| Tool | Returns |
|---|---|
| `get_logs(service, since, level, trace_id)` | Matching log lines, each with an evidence ID (e.g. `LOG-1042`) |
| `get_metrics(service, metric, window)` | Time series plus summary, with an evidence ID (e.g. `MET-77`) |
| `get_deps(service)` | Upstream and downstream services |
| `get_changes(since)` | Deploys, restarts, config and chaos changes |
| `get_health()` | Current health of all services |

Evidence IDs matter: every tool result carries one so the timeline and postmortem can cite it precisely.

### Exit criteria

- [ ] After breaking a service, the tools return the relevant anomalies within a few seconds.
- [ ] Each returned item has a stable evidence ID.
- [ ] Tool outputs are truncated to a sane size so they fit the LLM context.

---

## Phase 4: Incident Detector

**Goal:** Detect problems with plain code, without the LLM, and start the clock.
**Estimated time:** 2 to 3 hours

### Detection rules (`config/slo.yaml`)

- Health check fails 3 times in a row.
- Error rate above 5 percent over the last 30 seconds.
- p95 latency above 2 times its rolling baseline, or above the SLO limit.
- Memory or CPU growth trend above a threshold.

### Incident lifecycle

- On the first breach, create an Incident with `id`, **T0** (the time of the earliest anomaly, not the time of detection), affected services and trigger rule.
- Deduplicate: further breaches during an open incident attach to it rather than opening a new one.
- Close the incident when health, error rate and latency stay within SLO for 60 seconds.
- Store incidents in SQLite with a status of `open`, `diagnosing`, `mitigating`, `resolved` or `escalated`.

### Exit criteria

- [ ] Every chaos type from Phase 2 opens an incident within 30 seconds.
- [ ] A healthy system for 10 minutes produces zero false alarms.
- [ ] T0 is recorded accurately and used later for time-to-diagnosis.

---

## Phase 5: Agent Core

**Goal:** An LLM agent that investigates incidents with tools and names the root cause with evidence.
**Estimated time:** 4 to 5 hours

### Tasks

- Implement the `step()` loop from `llm.py` and `investigate()` from `agent.py`, with failover per incident across providers.
- Write the system prompt (see below).
- Pass the incident summary as the first message: affected services, trigger rule, T0, and current health.
- Cap the loop at 12 steps and force a final answer if the limit is reached.
- Require a structured final answer in JSON so code can parse it.

### System prompt requirements

- Find the earliest anomaly, not the loudest one.
- Walk the dependency graph from the symptom toward the source. If A is slow and A calls C, check C first.
- Check recent changes before anything else (deploys, restarts, chaos flags).
- Confirm every hypothesis with at least two independent pieces of evidence before acting.
- Cite evidence IDs for every claim.
- State confidence, and say so if the evidence is insufficient.

### Final answer schema

```json
{
  "root_cause_service": "payments",
  "root_cause": "payments process crashed (OOM) at 02:14:07",
  "evidence": ["LOG-1042", "MET-77", "CHG-9"],
  "causal_chain": ["payments down", "orders 5xx", "gateway latency"],
  "confidence": 0.9,
  "proposed_fix": {"action": "restart_service", "args": {"service": "payments"}},
  "mitigation_vs_cure": "mitigation"
}
```

When the final JSON is parsed, record **T1** (the time the root cause is named). Time-to-diagnosis is `T1 - T0`.

### Exit criteria

- [ ] On cascade scenarios, the agent names the upstream source, not the noisy symptom.
- [ ] The same pipeline works on the primary and backup LLM.
- [ ] Judge-style test: 5 failures you did not script are diagnosed correctly at least 4 times.

---

## Phase 6: Fix Pipeline and Approval Flow

**Goal:** Apply safe fixes automatically, gate risky ones behind a human, and block forbidden ones.
**Estimated time:** 3 to 4 hours

### Permission tiers (enforced in code)

| Tier | Actions | Behavior |
|---|---|---|
| Auto-fix | `restart_service`, `clear_chaos_flag`, `rotate_logs` | Run immediately, then verify |
| Ask-first | `rollback`, `scale_up`, `change_config` | Pause, show in dashboard, wait for approve or deny |
| Never-do | `delete_data`, `drop_db`, edit outside app folder | Not exposed to the LLM; blocked and logged if requested |

### Pipeline

```
diagnosis -> tier check -> circuit breaker -> snapshot -> apply -> verify -> resolve | escalate
```

- **Circuit breaker:** at most 2 restarts per service in 10 minutes. After that, escalate and propose a rollback.
- **Snapshot** health and metrics before acting, for the postmortem and for undo.
- **Verify recovery** by polling health, error rate and p95 for up to 30 seconds. A restart that did not help is a failed fix.
- **Approval flow:** an ask-first action creates a pending record. The dashboard shows Approve and Deny buttons. The agent waits with a timeout and escalates if nobody responds.
- **Rollback:** switch the service back to the previous version, restart it, verify.
- Every action is written to the audit log with who or what approved it.

### Exit criteria

- [ ] Crash: auto-restart works and is verified.
- [ ] Bad deploy: restart fails, agent proposes rollback, human approves, system recovers.
- [ ] Prompt-injection test: telling the agent to drop the database is blocked by code, and the block is logged.

---

## Phase 7: Timeline, Postmortem and Metrics

**Goal:** Turn the evidence log into a readable incident record and a measurable result.
**Estimated time:** 3 to 4 hours

### Incident timeline

- Build it from the audit log, not from LLM memory, so it is accurate.
- Each entry has a timestamp, event (detected, investigated, diagnosed, fixed, verified), description and linked evidence IDs.
- Evidence IDs expand to the actual log line or metric snapshot in the UI.

### Postmortem generator

Feed the timeline, evidence and audit log to the LLM and ask it to fill a fixed template. Reject any statement that has no evidence ID.

- Summary and severity
- Impact (services, duration, error counts)
- Root cause
- Timeline
- What worked and what did not
- Fix applied and verification
- Prevention and follow-up actions

Save output as Markdown in `reports/INC-<id>.md`.

### Measured results

| Metric | How it is computed |
|---|---|
| Time-to-detect | Detection time minus T0 |
| Time-to-diagnosis | T1 (root cause named) minus T0 |
| Time-to-recovery | Verified-healthy time minus T0 |
| Manual baseline | Time a person needs on the same failure (record this once yourself) |
| Diagnosis accuracy | Correct root causes out of all test failures |

### Exit criteria

- [ ] Every claim in the postmortem has an evidence ID.
- [ ] A results table shows time-to-diagnosis for at least 8 failure scenarios.
- [ ] A before-and-after number exists (agent vs manual).

---

## Phase 8: Dashboard, Testing and Demo Preparation

**Goal:** Make it visible, robust and rehearsed.
**Estimated time:** 4 to 6 hours

### Dashboard

- Service health board with live status colors.
- Active incident panel: timeline, current agent step, evidence viewer.
- Pending approvals with Approve and Deny buttons.
- Metrics panel: time-to-diagnosis, incidents resolved, auto-fix vs human-approved counts.
- Postmortem viewer.

### Testing


- **Scenario suite:** run every chaos type automatically and record diagnosis correctness and timings.
- **Combination failures:** two faults at once, and a fault during an ongoing incident.
- **Provider failover:** disconnect the primary LLM and confirm the backup completes the incident.
- **Safety tests:** forbidden actions, prompt injection inside log lines, restart-loop protection.
- **Fresh-machine test:** clone the repo on another laptop and run the demo from the README only.

### Live-judging rehearsal

- Prepare a 3-minute script: show healthy system, break it, watch detection, diagnosis, fix, postmortem.
- Keep a hotspot and a local Ollama model as backup in case venue Wi-Fi fails.
- Have a second teammate play the judge and use the hidden random chaos script.
- Record a short backup video of a successful run.

### Exit criteria

- [ ] Demo starts with one command and finishes unattended.
- [ ] Agent diagnoses at least 4 of 5 unseen failures.
- [ ] README, video and results table are ready.

---

## 2. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Agent blames the loudest symptom | Dependency tool plus prompt rule: walk upstream to the earliest anomaly |
| LLM hallucinated evidence | Evidence IDs generated by code; reject claims with unknown IDs |
| Restart loop | Circuit breaker of 2 restarts per 10 minutes |
| API limits or Wi-Fi failure on demo day | Backup provider, local Ollama model, recorded video |
| Log lines contain malicious instructions | Treat tool output as data; tier checks live in code, not in the prompt |
| Works only on rehearsed failures | Random chaos script and combination tests |
| Too much scope | Finish Phases 0 to 5 first; then add 6 to 8 by judging value |

## 3. Final Submission Checklist

- [ ] Demo app with 4 services, logs and metrics
- [ ] Chaos script with 8 or more failure types
- [ ] Detector that opens incidents without the LLM
- [ ] Agent with tools, evidence IDs and provider failover
- [ ] Three permission tiers enforced in code
- [ ] Verified auto-fix and approval flow for rollback
- [ ] Incident timeline linking to evidence
- [ ] Auto-generated postmortem
- [ ] Measured time-to-diagnosis table
- [ ] One-command demo, README, video
