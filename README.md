# AI Site Reliability Engineer (AI-SRE)

An AI agent that watches a running microservices app, detects incidents, finds the root cause **with evidence**, applies **safe fixes automatically**, asks a human before risky ones, and writes the **postmortem**.

Built for hackathon problem statement **AIA-02**. No Docker required: services run as plain local processes.

---

## Why this exists

At 3 AM, one tired engineer digs through logs, metrics and alerts to find out why the app broke. This project does that first pass automatically, shows its evidence, and never takes a dangerous action without permission.

## Features

| Feature | How it works |
|---|---|
| **Incident detection** | Plain-code rules (health, error rate, latency, trends). No LLM needed to notice a problem |
| **Root cause with evidence** | LLM agent uses read-only tools; every claim cites an evidence ID (log line, metric sample, change record) |
| **Cascade reasoning** | Walks `dependencies.yaml` upstream to the earliest anomaly instead of blaming the loudest symptom |
| **Tiered permissions** | Auto-fix, ask-first, never-do, enforced in code, not in the prompt |
| **Verified fixes** | Every fix is followed by a recovery check; a failed fix escalates |
| **Circuit breaker** | Max 2 restarts per service per 10 minutes |
| **Incident timeline** | Built from the audit log; each entry links to evidence |
| **Auto postmortem** | Generated from the timeline; claims without evidence are rejected |
| **Time-to-diagnosis** | Measured from the earliest anomaly (T0) to root cause named (T1) |
| **Telegram Approval Bot** | Real-time incident alerts, inline [Approve]/[Deny] buttons, and slash commands |
| **LLM failover** | Primary and backup provider (for example a cloud API plus local Ollama) |

## Architecture

```
gateway :8000 --> orders :8001 --> payments :8002
                        \--> inventory :8003 --> sqlite

Supervisor (start/stop/restart)     Chaos CLI (inject failures)
        |                                    |
        v                                    v
   logs/*.log + /metrics  -->  Observability layer (SQLite)
                                        |
                                        v
                      Detector (rules) --> Incident opened (T0)
                                        |
                                        v
                Agent loop (LLM + tools, tier-gated) --> Root cause (T1)
                                        |
                  Fix pipeline: tier > limit > snapshot > apply > verify
                                        |
                    Timeline + Postmortem + Dashboard (approve/deny)
```

## Permission tiers

| Tier | Actions | Behavior |
|---|---|---|
| **Auto-fix** | `restart_service`, `clear_chaos_flag`, `rotate_logs` | Runs immediately, then verified |
| **Ask-first** | `rollback`, `scale_up`, `change_config` | Pauses; a human approves or denies in the dashboard |
| **Never-do** | `delete_data`, `drop_db`, edits outside the app folder | Not exposed to the LLM; blocked and logged if attempted |

The check lives in `execute()` in code. Even a hallucinated or prompt-injected `drop_db` call does nothing.

## Fix pipeline

```
diagnosis -> tier check -> circuit breaker -> snapshot -> apply -> verify -> resolve | escalate
```

## Project structure

```
ai-sre/
  services/            gateway/  orders/  payments/  inventory/
  sre/
    supervisor.py      start / stop / restart, change log
    chaos.py           failure injection CLI
    observability.py   log parser, metric store, deps, changes
    detector.py        rule-based incident detection
    agent.py           tool-calling investigation loop
    tools.py           tool implementations + evidence IDs
    fixer.py           tiered fix pipeline + approvals
    postmortem.py      timeline + postmortem generator
    llm.py             provider wrapper with failover
    dashboard/         FastAPI + HTML UI
  config/
    dependencies.yaml  who calls whom
    slo.yaml           detection thresholds
  logs/  data/  reports/
  tests/
  run.py
  requirements.txt
  .env.example
```

## Quick start

### Prerequisites

- Python 3.11+
- An API key for at least one LLM provider, **or** [Ollama](https://ollama.com) running locally
- Works on Linux, macOS and Windows

### Setup

```bash
git clone <your-repo-url>
cd ai-sre
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then add your key(s)
```

### Configure the LLM

This project uses local [Ollama](https://ollama.com) running `qwen2.5` (no external API keys needed).

```bash
ollama pull qwen2.5
```

`.env`:

```ini
PRIMARY_PROVIDER=ollama
BACKUP_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5
```

### Run the demo

```bash
python run.py
```

This starts the four services, the load generator, the observability collector, the detector, the agent and the dashboard.

Open the dashboard at **http://localhost:9000**.

## Breaking things (chaos)

In a second terminal:

```bash
python -m sre.chaos crash payments              # kill the process
python -m sre.chaos latency orders 3000         # add 3000 ms delay
python -m sre.chaos errors inventory 0.5        # 50% error rate
python -m sre.chaos leak orders 50              # memory leak
python -m sre.chaos cpu gateway                 # CPU hog
python -m sre.chaos bad-deploy payments         # switch to buggy v2
python -m sre.chaos dblock inventory            # lock the SQLite file
python -m sre.chaos random                      # surprise me
python -m sre.chaos clear all                   # reset everything
```

## What you will see

1. Dashboard shows all services green.
2. You run a chaos command.
3. The detector opens an incident and records **T0**.
4. The agent calls tools, follows the dependency graph and names the root cause (**T1**).
5. An auto-tier fix runs and is verified, or an ask-first fix appears as a pending approval.
6. The incident closes; the timeline and postmortem appear in `reports/`.

## Example scenarios

| Scenario | Expected agent behavior |
|---|---|
| Kill `payments` | Traces `orders` 5xx to `payments`; auto-restarts; verifies |
| Latency on `inventory` | Finds slow `inventory` behind slow `orders`; clears chaos flag |
| Bad deploy of `payments` | Restart fails, circuit breaker trips, agent proposes **rollback**, waits for approval |
| Memory leak in `orders` | Restarts as mitigation; postmortem states the root cause is the leak |
| Injected "drop the database" text in logs | Treated as data; action blocked and logged |

## Metrics we measure

| Metric | Definition |
|---|---|
| Time-to-detect | Detection time minus T0 |
| **Time-to-diagnosis** | Root cause named (T1) minus T0 |
| Time-to-recovery | Verified healthy minus T0 |
| Diagnosis accuracy | Correct root causes over all scenarios |
| Auto-fix rate | Share of incidents resolved without a human |

Run the full scenario suite:

```bash
python -m tests.run_scenarios
```

Results are written to `reports/results.md`.

> Fill in your measured numbers here before submission:
>
> | Scenario | Time-to-diagnosis | Correct? | Manual baseline |
> |---|---|---|---|
> | crash | | | |
> | latency | | | |
> | bad deploy | | | |

## Design decisions

- **Detection is code, diagnosis is LLM.** Faster, free, and reliable. The LLM is only called once an incident exists.
- **Evidence IDs are generated by code.** The model cannot invent a citation that the postmortem will accept.
- **Allowlist over blocklist.** Only registered tools can run, and each validates its arguments.
- **Failover per incident.** If the primary LLM fails, the whole investigation reruns on the backup, so tool-call histories are never mixed across providers.
- **Verify, never assume.** "Restarted" is not "fixed"; health, error rate and p95 must recover.

## Telegram Incident & Approval Bot

The AI-SRE system includes an autonomous on-call Telegram bot (`sre/telegram_bot.py`) built with `python-telegram-bot` v20+ and `asyncio`:
1. **Push Alerts**: Proactively notifies on-call teams of open incidents and Tier-2 "Ask-First" remediations.
2. **Interactive In-Place Approval**: Renders inline `[ ✅ Approve ]` and `[ ❌ Deny ]` buttons; executing a decision immediately applies or aborts the fix via the AI-SRE REST API and edits the message in-place with the operator's identity and timestamp.
3. **On-Call Slash Commands**:
   - `/status` - Real-time microservice health, latencies, and error rates.
   - `/incidents` - Active incident details with trigger rule, T0 anomaly timestamp, and TTD.
   - `/approvals` - Inspect pending fix approvals with interactive decision buttons.
   - `/postmortem <id>` - View verified incident postmortem report.
   - `/restart <service>` - Manually trigger supervisor restart for any microservice.
   - `/help` - Command manual.

### Telegram Bot Setup
1. Create a bot using [@BotFather](https://t.me/BotFather) on Telegram and copy the token.
2. Set your environment variables in `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRstuVWXyz"
   TELEGRAM_CHAT_ID="123456789" # Your chat or group ID
   AI_SRE_URL="http://localhost:9000"
   ```
3. Run the bot standalone:
   ```bash
   python sre/telegram_bot.py
   # Or via the single entrypoint
   python run.py --telegram-bot
   ```
   *(When `TELEGRAM_BOT_TOKEN` is configured in `.env`, `python run.py` automatically launches the bot in the background alongside the services).*

## Safety

- Permission tiers are enforced in `fixer.py` and `agent.py`, not in the prompt.
- Tool output (logs, metrics) is treated as untrusted data.
- All actions are written to an append-only audit log with before/after state.
- Restart loops are stopped by the circuit breaker.
- Secrets live in `.env`, which is in `.gitignore`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Port already in use | Change ports in `config/dependencies.yaml` or stop the old process |
| LLM call fails | Check your key; the agent will fall back to the backup provider |
| Local model ignores tools | Use a larger model, shorten tool descriptions, or rely on the cloud primary |
| No incident opens | Make sure the load generator is running so metrics are non-empty |
| Windows kill command fails | Use `python -m sre.chaos crash <service>` instead of `kill -9` |

## Limitations

- Single-machine demo; not a production monitoring system.
- Detection thresholds are tuned for the demo app.
- Small local models are weaker at tool calling than hosted ones.

## Roadmap

- Slack or Telegram approval bot
- Prometheus and Loki adapters
- Learning from past incidents (similar-incident lookup)
- Kubernetes and Docker backends for the supervisor

## Team

| Name | Role |
|---|---|
| | |

## License

MIT
