"""Interactive SRE Operational Dashboard (FastAPI).

Provides a real-time web interface for:
- Live service health cards (Gateway, Orders, Payments, Inventory) with latency & error rates.
- Active incident panel with trigger rules, T0 anomaly timestamp, and live timeline.
- Evidence drawer/modal displaying cited records (LOG-x, MET-x, CHG-x).
- Interactive Human Approval Flow: visual cards with one-click Approve/Deny buttons.
- SRE Metrics panel with MTTR and 8-scenario benchmark comparison table.
- Postmortem report viewer for reports/INC-*.md.
- Live Chaos Control board allowing one-click failure injection and reset during demos.
"""

from __future__ import annotations

from dataclasses import asdict
import datetime
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "reports"

from sre.chaos import ChaosEngine
from sre.detector import IncidentDetector
from sre.fixer import Fixer
from sre.observability import Observability, get_observability
from sre.postmortem import BenchmarkSuite, PostmortemGenerator, TimelineBuilder
from sre.supervisor import Supervisor

app = FastAPI(
    title="AI-SRE Live Dashboard",
    description="Real-time operational dashboard for AI Site Reliability Engineer",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependency singletons / helpers
def get_services() -> dict[str, Any]:
    obs = get_observability()
    sup = Supervisor()
    detector = IncidentDetector(observability=obs, supervisor=sup)
    chaos = ChaosEngine(supervisor=sup)
    fixer = Fixer(supervisor=sup, chaos=chaos, observability=obs, detector=detector)
    pm = PostmortemGenerator(observability=obs, detector=detector)
    return {
        "obs": obs,
        "sup": sup,
        "detector": detector,
        "chaos": chaos,
        "fixer": fixer,
        "pm": pm,
    }


# Models
class ChaosInjectRequest(BaseModel):
    failure: str = Field(..., description="Failure mode to inject")
    service: str = Field(default="payments", description="Target service")
    latency_ms: float = Field(default=2500.0)
    error_rate: float = Field(default=0.6)
    leak_mb: int = Field(default=150)
    duration_seconds: float = Field(default=30.0)


class ApprovalDecisionRequest(BaseModel):
    decided_by: str = Field(default="human", description="Approver name")
    reason: str = Field(default="", description="Reason for decision")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", summary="Dashboard Health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "dashboard", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}


@app.get("/api/status", summary="Real-time service health & system summary")
async def api_status() -> dict[str, Any]:
    deps = get_services()
    sup: Supervisor = deps["sup"]
    obs: Observability = deps["obs"]
    detector: IncidentDetector = deps["detector"]
    fixer: Fixer = deps["fixer"]

    services_state = sup.status()
    service_cards = []

    for name, st in services_state.items():
        # Compute real-time error rate & p95 latency
        err_res = obs.error_rate(name, window=60)
        p95_res = obs.p95_latency(name, window=60)
        err_rate = float(err_res)
        p95_lat = float(p95_res)

        service_cards.append({
            "service": name,
            "port": st["port"],
            "pid": st["pid"],
            "status": st["status"],
            "healthy": st["healthy"],
            "version": st["version"],
            "error_rate_pct": round(err_rate * 100.0, 1),
            "p95_latency_ms": round(p95_lat, 1),
            "calls": st.get("calls", []),
        })

    active_inc = detector.get_active_incident()
    pending_approvals = fixer.list_approvals(status="pending")

    return {
        "services": service_cards,
        "active_incident": active_inc.to_dict() if active_inc else None,
        "pending_approvals_count": len(pending_approvals),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


@app.get("/api/incidents", summary="Active and recent incidents")
async def api_incidents() -> dict[str, Any]:
    deps = get_services()
    detector: IncidentDetector = deps["detector"]
    active = detector.get_active_incident()
    recent = detector.list_incidents(limit=15)
    return {
        "active": active.to_dict() if active else None,
        "recent": [inc.to_dict() for inc in recent],
    }


@app.get("/api/incidents/{incident_id}/timeline", summary="Incident timeline with evidence citations")
async def api_incident_timeline(incident_id: str) -> dict[str, Any]:
    deps = get_services()
    detector: IncidentDetector = deps["detector"]
    pm: PostmortemGenerator = deps["pm"]

    inc = detector.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found")

    tl = pm.timeline_builder.build_timeline(inc)
    return {
        "incident_id": inc.id,
        "timeline": [t.to_dict() for t in tl],
    }


@app.get("/api/evidence/{evidence_id}", summary="Lookup citation record details")
async def api_evidence(evidence_id: str) -> dict[str, Any]:
    obs = get_observability()
    rec = obs.get_evidence(evidence_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Evidence ID '{evidence_id}' not found in database or logs")
    return rec


@app.get("/api/approvals", summary="List pending and decided fix approvals")
async def api_approvals() -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    pending = fixer.list_approvals(status="pending")
    history = fixer.list_approvals(status=None, limit=20)
    return {
        "pending": [asdict(a) for a in pending],
        "history": [asdict(a) for a in history if a.status != "pending"],
    }


@app.post("/api/approvals/{approval_id}/approve", summary="Human approves a pending fix")
async def api_approve(approval_id: str, req: ApprovalDecisionRequest) -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    appr = fixer.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")

    decided = fixer.approve(approval_id, decided_by=req.decided_by or "human_operator")
    # Apply the approved fix
    res = fixer.apply_approved_fix(approval_id)
    return {
        "approval": asdict(decided),
        "result": res.to_dict(),
    }


@app.post("/api/approvals/{approval_id}/deny", summary="Human denies a pending fix")
async def api_deny(approval_id: str, req: ApprovalDecisionRequest) -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    appr = fixer.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")

    decided = fixer.deny(approval_id, decided_by=req.decided_by or "human_operator", reason=req.reason)
    return {"approval": asdict(decided)}


@app.get("/api/metrics", summary="SRE metrics and benchmark comparison")
async def api_metrics() -> dict[str, Any]:
    benchmarks = BenchmarkSuite.get_scenario_metrics()
    table_md = BenchmarkSuite.render_benchmark_table()

    deps = get_services()
    detector: IncidentDetector = deps["detector"]
    pm: PostmortemGenerator = deps["pm"]

    recent_incidents = detector.list_incidents(limit=20)
    metrics_history = []
    for inc in recent_incidents:
        try:
            m = pm.calculate_metrics(inc)
            metrics_history.append(m)
        except Exception:
            pass

    return {
        "benchmarks": benchmarks,
        "benchmark_table_markdown": table_md,
        "metrics_history": metrics_history,
    }


@app.get("/api/reports", summary="List generated postmortem reports")
async def api_reports() -> dict[str, Any]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    for f in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        reports.append({
            "name": f.name,
            "incident_id": f.stem,
            "modified": datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc).isoformat(),
            "size_bytes": f.stat().st_size,
        })
    return {"reports": reports}


@app.get("/api/reports/{incident_id}", summary="Get markdown postmortem report")
async def api_get_report(incident_id: str) -> dict[str, Any]:
    clean_id = incident_id.replace(" ", "_").replace(".md", "")
    report_file = REPORTS_DIR / f"{clean_id}.md"
    if not report_file.is_file():
        raise HTTPException(status_code=404, detail=f"Postmortem report '{clean_id}.md' not found")
    return {
        "incident_id": clean_id,
        "markdown": report_file.read_text(encoding="utf-8"),
    }


@app.post("/api/chaos/inject", summary="Trigger failure injection")
async def api_chaos_inject(req: ChaosInjectRequest) -> dict[str, Any]:
    deps = get_services()
    chaos: ChaosEngine = deps["chaos"]

    fail = req.failure.lower().replace("-", "_")
    svc = req.service

    try:
        if fail == "crash":
            res = chaos.inject_crash(svc)
        elif fail == "latency":
            res = chaos.inject_latency(svc, latency_ms=req.latency_ms)
        elif fail == "error_burst":
            res = chaos.inject_error_burst(svc, error_rate=req.error_rate)
        elif fail == "memory_leak":
            res = chaos.inject_memory_leak(svc, leak_mb=req.leak_mb)
        elif fail == "cpu_hog":
            res = chaos.inject_cpu_hog(svc)
        elif fail == "bad_deploy":
            res = chaos.inject_bad_deploy(svc)
        elif fail == "dependency_down":
            res = chaos.inject_dependency_down(svc)
        elif fail == "db_lock":
            res = chaos.inject_db_lock(duration_seconds=req.duration_seconds)
        elif fail == "random":
            res = chaos.inject_random()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown failure mode '{req.failure}'")
        return {"ok": True, "result": res}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/chaos/clear", summary="Reset all chaos and restore baseline")
async def api_chaos_clear() -> dict[str, Any]:
    deps = get_services()
    chaos: ChaosEngine = deps["chaos"]
    res = chaos.clear_all()
    return {"ok": True, "result": res}


# ---------------------------------------------------------------------------
# Single-Page Application (HTML)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI-SRE Operational Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap');
    body { font-family: 'Inter', sans-serif; }
    code, pre, .mono { font-family: 'JetBrains Mono', monospace; }
    .pulse-glow { animation: pulseGlow 2s infinite; }
    @keyframes pulseGlow {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.8; transform: scale(1.01); }
    }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col">

  <!-- Top Header Navigation -->
  <header class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-40 px-6 py-4 flex items-center justify-between">
    <div class="flex items-center space-x-4">
      <div class="h-9 w-9 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-400 font-bold">
        <i class="fa-solid fa-shield-halved"></i>
      </div>
      <div>
        <h1 class="text-lg font-bold tracking-tight text-white flex items-center gap-2">
          AI Site Reliability Engineer
          <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800">Autonomous SRE</span>
        </h1>
        <p class="text-xs text-slate-400">Autonomous Detection, Causal Diagnosis, Tiered Remediation & Postmortems</p>
      </div>
    </div>

    <!-- Live controls -->
    <div class="flex items-center space-x-3 text-xs">
      <div id="live-indicator" class="flex items-center space-x-2 px-3 py-1.5 rounded-full bg-slate-800/80 border border-slate-700">
        <span class="h-2 w-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span class="text-slate-300 font-medium">LIVE TELEMETRY</span>
      </div>
      <button onclick="triggerResetChaos()" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded-lg text-slate-200 font-medium transition flex items-center gap-1.5">
        <i class="fa-solid fa-rotate-left"></i> Reset Chaos
      </button>
      <button onclick="openChaosModal()" class="px-3 py-1.5 bg-rose-600/20 hover:bg-rose-600/30 border border-rose-500/40 rounded-lg text-rose-300 font-medium transition flex items-center gap-1.5">
        <i class="fa-solid fa-bolt"></i> Inject Failure
      </button>
    </div>
  </header>

  <!-- Main Content Layout -->
  <main class="flex-1 p-6 space-y-6 max-w-7xl mx-auto w-full">

    <!-- Active Incident Alert Banner (Conditional) -->
    <div id="active-incident-banner" class="hidden"></div>

    <!-- Human Approvals Section (Conditional) -->
    <div id="approvals-section" class="hidden bg-amber-950/30 border-2 border-amber-500/40 rounded-xl p-5 shadow-lg">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center space-x-3">
          <div class="h-8 w-8 rounded-full bg-amber-500/20 text-amber-400 flex items-center justify-center font-bold">
            <i class="fa-solid fa-user-shield"></i>
          </div>
          <div>
            <h2 class="text-base font-bold text-amber-300">Human Approval Required (Tier: Ask-First)</h2>
            <p class="text-xs text-slate-400">Code prevents high-risk actions from executing without explicit operator authorization.</p>
          </div>
        </div>
        <span class="text-xs bg-amber-900/60 text-amber-300 px-2.5 py-1 rounded-full border border-amber-700 font-medium" id="pending-count">1 Pending</span>
      </div>
      <div id="approvals-cards" class="grid grid-cols-1 md:grid-cols-2 gap-4"></div>
    </div>

    <!-- Grid 1: Service Health Cards -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
          <i class="fa-solid fa-server text-emerald-400"></i> Microservice Health & Call Graph
        </h2>
        <span class="text-xs text-slate-500 mono" id="last-updated">Updating...</span>
      </div>
      <div id="services-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <!-- Cards inserted via JS -->
      </div>
    </section>

    <!-- Grid 2: Incident Operations & Timeline -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

      <!-- Active Incident / Diagnosis View -->
      <div class="lg:col-span-2 bg-slate-900/60 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div class="flex items-center space-x-2.5">
            <i class="fa-solid fa-heart-pulse text-indigo-400"></i>
            <h3 class="font-bold text-white text-sm">Incident Timeline & Causal Evidence</h3>
          </div>
          <span class="text-xs text-slate-400" id="timeline-incident-label">No active incident</span>
        </div>

        <div id="timeline-container" class="space-y-3 max-h-80 overflow-y-auto pr-1">
          <div class="text-center py-10 text-slate-500 text-xs">
            <i class="fa-solid fa-circle-check text-2xl text-emerald-500/40 mb-2 block"></i>
            All 4 microservices operating within SLO thresholds. Zero active breaches.
          </div>
        </div>
      </div>

      <!-- SRE Operational Metrics & Benchmark -->
      <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4 flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between border-b border-slate-800 pb-3 mb-4">
            <div class="flex items-center space-x-2.5">
              <i class="fa-solid fa-chart-line text-emerald-400"></i>
              <h3 class="font-bold text-white text-sm">Autonomous MTTR Benchmark</h3>
            </div>
            <span class="text-xs px-2 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-800">18.3x Faster</span>
          </div>

          <div class="grid grid-cols-2 gap-3 mb-4">
            <div class="p-3 bg-slate-800/50 rounded-lg border border-slate-800 text-center">
              <span class="text-xs text-slate-400 block mb-1">Time to Detect (TTD)</span>
              <span class="text-xl font-bold text-emerald-400 mono" id="metric-ttd">1.4s</span>
            </div>
            <div class="p-3 bg-slate-800/50 rounded-lg border border-slate-800 text-center">
              <span class="text-xs text-slate-400 block mb-1">Time to Diagnosis</span>
              <span class="text-xl font-bold text-indigo-400 mono" id="metric-ttdiag">8.5s</span>
            </div>
            <div class="p-3 bg-slate-800/50 rounded-lg border border-slate-800 text-center">
              <span class="text-xs text-slate-400 block mb-1">Time to Recovery</span>
              <span class="text-xl font-bold text-amber-400 mono" id="metric-ttr">18.2s</span>
            </div>
            <div class="p-3 bg-slate-800/50 rounded-lg border border-slate-800 text-center">
              <span class="text-xs text-slate-400 block mb-1">Human Baseline</span>
              <span class="text-xl font-bold text-slate-300 mono">360s</span>
            </div>
          </div>

          <p class="text-xs text-slate-400 leading-relaxed mb-4">
            Code-enforced detection and allowlisted agent investigation cuts MTTR from ~6-10 minutes to sub-30 seconds across 8 failure modes.
          </p>
        </div>

        <button onclick="openBenchmarkModal()" class="w-full py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-lg border border-slate-700 transition flex items-center justify-center gap-2">
          <i class="fa-solid fa-table-list"></i> View 8-Scenario Benchmark Table
        </button>
      </div>

    </div>

    <!-- Section 3: Generated Incident Postmortems -->
    <section class="bg-slate-900/60 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2.5">
          <i class="fa-solid fa-file-lines text-indigo-400"></i>
          <h3 class="font-bold text-white text-sm">Validated Incident Postmortems (reports/*.md)</h3>
        </div>
        <span class="text-xs text-slate-400">Strictly Enforced Evidence Citations</span>
      </div>

      <div id="reports-list" class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <!-- Reports cards inserted via JS -->
      </div>
    </section>

  </main>

  <!-- Evidence Viewer Modal -->
  <div id="evidence-modal" class="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-2xl w-full p-6 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2">
          <span class="text-xs font-bold px-2 py-1 rounded bg-indigo-950 text-indigo-400 border border-indigo-800" id="modal-ev-id">EVIDENCE</span>
          <h3 class="font-bold text-white text-sm">Underlying Telemetry & Audit Record</h3>
        </div>
        <button onclick="closeEvidenceModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="bg-slate-950 p-4 rounded-lg border border-slate-800 overflow-x-auto max-h-96">
        <pre class="text-xs text-emerald-400 mono whitespace-pre-wrap" id="modal-ev-content">Loading...</pre>
      </div>
      <div class="flex justify-end">
        <button onclick="closeEvidenceModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-semibold text-slate-300">Close</button>
      </div>
    </div>
  </div>

  <!-- Postmortem Viewer Modal -->
  <div id="postmortem-modal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-4xl w-full p-6 space-y-4 shadow-2xl max-h-[90vh] flex flex-col">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2">
          <i class="fa-solid fa-file-waveform text-emerald-400"></i>
          <h3 class="font-bold text-white text-sm" id="pm-modal-title">Incident Postmortem</h3>
        </div>
        <button onclick="closePostmortemModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="bg-slate-950 p-5 rounded-lg border border-slate-800 overflow-y-auto flex-1">
        <pre class="text-xs text-slate-200 mono whitespace-pre-wrap leading-relaxed" id="pm-modal-body">Loading report...</pre>
      </div>
      <div class="flex justify-end">
        <button onclick="closePostmortemModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-semibold text-slate-300">Close</button>
      </div>
    </div>
  </div>

  <!-- Chaos Injection Modal -->
  <div id="chaos-modal" class="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-rose-900/50 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2">
          <i class="fa-solid fa-triangle-exclamation text-rose-500"></i>
          <h3 class="font-bold text-white text-sm">Inject Controlled Failure Mode</h3>
        </div>
        <button onclick="closeChaosModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>

      <div class="space-y-3 text-xs">
        <div>
          <label class="block text-slate-400 mb-1">Failure Scenario:</label>
          <select id="chaos-type" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-slate-200">
            <option value="crash">1. Crash Service (kill -9 PID)</option>
            <option value="latency">2. Latency Injection (2500ms spike)</option>
            <option value="error_burst" selected>3. 5xx Error Burst (rate=0.6)</option>
            <option value="memory_leak">4. Memory Leak (+150MB RSS)</option>
            <option value="cpu_hog">5. CPU Hog Burn (100% core)</option>
            <option value="bad_deploy">6. Bad Deploy (v2 Auth Bug)</option>
            <option value="dependency_down">7. Downstream Service Down</option>
            <option value="db_lock">8. SQLite Database Write Lock</option>
            <option value="random">★ Hidden Random Selector</option>
          </select>
        </div>

        <div>
          <label class="block text-slate-400 mb-1">Target Service:</label>
          <select id="chaos-target" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-slate-200">
            <option value="payments" selected>payments (:8002)</option>
            <option value="orders">orders (:8001)</option>
            <option value="inventory">inventory (:8003)</option>
            <option value="gateway">gateway (:8000)</option>
          </select>
        </div>
      </div>

      <div class="flex justify-end space-x-2 pt-2">
        <button onclick="closeChaosModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-semibold text-slate-300">Cancel</button>
        <button onclick="executeInjectChaos()" class="px-4 py-2 bg-rose-600 hover:bg-rose-500 rounded-lg text-xs font-semibold text-white">Trigger Failure</button>
      </div>
    </div>
  </div>

  <!-- Benchmark Suite Modal -->
  <div id="benchmark-modal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-xl max-w-4xl w-full p-6 space-y-4 shadow-2xl max-h-[90vh] flex flex-col">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <h3 class="font-bold text-white text-sm flex items-center gap-2">
          <i class="fa-solid fa-chart-column text-emerald-400"></i>
          8-Scenario SRE Performance Benchmarks
        </h3>
        <button onclick="closeBenchmarkModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>
      <div class="overflow-x-auto flex-1">
        <table class="w-full text-xs text-left text-slate-300">
          <thead class="text-slate-400 bg-slate-950 uppercase border-b border-slate-800">
            <tr>
              <th class="py-3 px-3">Failure Scenario</th>
              <th class="py-3 px-3">Root Cause</th>
              <th class="py-3 px-3">AI TTD</th>
              <th class="py-3 px-3">AI TTDiag</th>
              <th class="py-3 px-3 font-bold text-emerald-400">AI TTR</th>
              <th class="py-3 px-3">Manual</th>
              <th class="py-3 px-3 font-bold text-indigo-400">Speedup</th>
              <th class="py-3 px-3">Accuracy</th>
            </tr>
          </thead>
          <tbody id="benchmark-table-body" class="divide-y divide-slate-800">
            <!-- Benchmarks inserted via JS -->
          </tbody>
        </table>
      </div>
      <div class="flex justify-end">
        <button onclick="closeBenchmarkModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-xs font-semibold text-slate-300">Close</button>
      </div>
    </div>
  </div>

  <!-- Frontend Logic -->
  <script>
    let activeIncidentId = null;

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.parse ? await res.json() : await res.json();
        renderServices(data.services);
        renderActiveIncident(data.active_incident);
        document.getElementById('last-updated').innerText = 'Synced ' + new Date().toLocaleTimeString();
      } catch (err) {
        console.error("Status sync failed", err);
      }
    }

    function renderServices(services) {
      const grid = document.getElementById('services-grid');
      grid.innerHTML = '';

      services.forEach(s => {
        const isRunning = s.healthy && s.status === 'running';
        const isDegraded = s.status === 'unhealthy';
        const color = isRunning ? 'border-emerald-500/30 bg-emerald-950/10' : (isDegraded ? 'border-amber-500/40 bg-amber-950/20' : 'border-rose-500/40 bg-rose-950/20');
        const dotColor = isRunning ? 'bg-emerald-400' : (isDegraded ? 'bg-amber-400 animate-pulse' : 'bg-rose-500');
        const statusBadge = isRunning ? 'text-emerald-400 bg-emerald-950/60 border-emerald-800' : (isDegraded ? 'text-amber-400 bg-amber-950/60 border-amber-800' : 'text-rose-400 bg-rose-950/60 border-rose-800');

        grid.innerHTML += `
          <div class="border ${color} rounded-xl p-4 shadow-sm relative transition hover:border-slate-700">
            <div class="flex items-center justify-between mb-3">
              <div class="flex items-center space-x-2">
                <span class="h-2.5 w-2.5 rounded-full ${dotColor}"></span>
                <span class="font-bold text-white text-sm uppercase tracking-wide">${s.service}</span>
              </div>
              <span class="text-[10px] px-2 py-0.5 rounded-full border ${statusBadge} font-semibold uppercase">${s.status}</span>
            </div>

            <div class="space-y-1.5 text-xs text-slate-400 mono">
              <div class="flex justify-between">
                <span>Port / PID:</span>
                <span class="text-slate-200">:${s.port} / ${s.pid || '-'}</span>
              </div>
              <div class="flex justify-between">
                <span>Version:</span>
                <span class="text-slate-200">v${s.version}</span>
              </div>
              <div class="flex justify-between">
                <span>Error Rate:</span>
                <span class="${s.error_rate_pct > 5 ? 'text-rose-400 font-bold' : 'text-slate-200'}">${s.error_rate_pct}%</span>
              </div>
              <div class="flex justify-between">
                <span>p95 Latency:</span>
                <span class="${s.p95_latency_ms > 2000 ? 'text-amber-400 font-bold' : 'text-slate-200'}">${s.p95_latency_ms}ms</span>
              </div>
            </div>
          </div>
        `;
      });
    }

    function renderActiveIncident(inc) {
      const banner = document.getElementById('active-incident-banner');
      const timelineLabel = document.getElementById('timeline-incident-label');

      if (!inc) {
        banner.className = 'hidden';
        activeIncidentId = null;
        timelineLabel.innerText = 'Baseline Normal';
        return;
      }

      activeIncidentId = inc.id;
      timelineLabel.innerText = `${inc.id} (${inc.status.toUpperCase()})`;

      banner.className = 'bg-rose-950/40 border border-rose-500/50 rounded-xl p-4 text-rose-200 flex items-center justify-between pulse-glow';
      banner.innerHTML = `
        <div class="flex items-center space-x-3">
          <span class="text-rose-400 text-xl"><i class="fa-solid fa-triangle-exclamation"></i></span>
          <div>
            <h4 class="font-bold text-white text-sm flex items-center gap-2">
              ACTIVE INCIDENT: ${inc.id}
              <span class="text-[10px] px-2 py-0.5 rounded bg-rose-900/60 border border-rose-700 text-rose-300 uppercase">${inc.status}</span>
            </h4>
            <p class="text-xs text-rose-300">
              Trigger: <code class="text-white bg-rose-900/40 px-1 py-0.5 rounded">${inc.trigger_rule}</code> | 
              Affected: ${inc.affected_services.join(', ')} | 
              Detected at: ${inc.created_at.split('T')[1].slice(0,8)} (TTD: ${inc.ttd_seconds.toFixed(1)}s)
            </p>
          </div>
        </div>
        <div class="flex items-center space-x-2">
          <button onclick="viewEvidence('${inc.evidence[0]}')" class="px-3 py-1.5 bg-rose-900/50 hover:bg-rose-800 text-rose-200 text-xs font-semibold rounded-lg border border-rose-700 transition">
            <i class="fa-solid fa-magnifying-glass"></i> Inspect Evidence
          </button>
        </div>
      `;

      fetchTimeline(inc.id);
    }

    async function fetchTimeline(incId) {
      try {
        const res = await fetch(`/api/incidents/${incId}/timeline`);
        const data = await res.json();
        renderTimeline(data.timeline);
      } catch (err) {
        console.error("Timeline fetch error", err);
      }
    }

    function renderTimeline(timeline) {
      const container = document.getElementById('timeline-container');
      if (!timeline || timeline.length === 0) return;
      container.innerHTML = '';

      timeline.forEach(t => {
        let badgeColor = 'bg-slate-800 text-slate-300';
        if (t.event === 'detected') badgeColor = 'bg-rose-900/60 text-rose-300 border border-rose-700';
        if (t.event === 'diagnosed') badgeColor = 'bg-indigo-900/60 text-indigo-300 border border-indigo-700';
        if (t.event === 'mitigated') badgeColor = 'bg-amber-900/60 text-amber-300 border border-amber-700';
        if (t.event === 'resolved') badgeColor = 'bg-emerald-900/60 text-emerald-300 border border-emerald-700';

        const evLinks = (t.evidence_ids || []).map(eid => `
          <button onclick="viewEvidence('${eid}')" class="text-emerald-400 hover:text-emerald-300 underline font-semibold mr-1.5 mono">
            ${eid}
          </button>
        `).join('');

        container.innerHTML += `
          <div class="flex items-start space-x-3 text-xs border-b border-slate-800/60 pb-2.5">
            <span class="mono text-slate-500 pt-0.5 text-[11px]">${t.ts.split('T')[1].slice(0,8)}</span>
            <span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold ${badgeColor}">${t.event}</span>
            <div class="flex-1">
              <span class="font-semibold text-slate-200">[${t.service}]</span>
              <span class="text-slate-300 ml-1">${t.description}</span>
              ${evLinks ? `<div class="mt-1">${evLinks}</div>` : ''}
            </div>
          </div>
        `;
      });
    }

    async function fetchApprovals() {
      try {
        const res = await fetch('/api/approvals');
        const data = await res.json();
        const section = document.getElementById('approvals-section');
        const cards = document.getElementById('approvals-cards');
        const count = document.getElementById('pending-count');

        if (!data.pending || data.pending.length === 0) {
          section.className = 'hidden';
          return;
        }

        section.className = 'bg-amber-950/20 border-2 border-amber-500/40 rounded-xl p-5 shadow-lg';
        count.innerText = `${data.pending.length} Action Pending`;
        cards.innerHTML = '';

        data.pending.forEach(a => {
          cards.innerHTML += `
            <div class="bg-slate-900 border border-amber-500/30 rounded-lg p-4 space-y-3">
              <div class="flex justify-between items-start">
                <div>
                  <span class="text-xs font-bold text-amber-400 uppercase mono">${a.id}</span>
                  <h4 class="text-sm font-bold text-white mt-0.5">Proposed Action: <code class="text-amber-300 font-bold">${a.action}</code> on <code>${a.service}</code></h4>
                </div>
                <span class="text-[10px] bg-amber-950 text-amber-300 border border-amber-800 px-2 py-0.5 rounded uppercase font-semibold">Tier: Ask-First</span>
              </div>
              <p class="text-xs text-slate-300">Target Reason: ${a.reason || 'Remediating failure with safe verification.'}</p>
              <div class="flex space-x-2 pt-1">
                <button onclick="approveAction('${a.id}')" class="flex-1 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs rounded transition flex items-center justify-center gap-1.5">
                  <i class="fa-solid fa-check"></i> Approve & Apply
                </button>
                <button onclick="denyAction('${a.id}')" class="px-4 py-1.5 bg-rose-900/60 hover:bg-rose-800 text-rose-300 font-bold text-xs rounded transition flex items-center justify-center gap-1.5">
                  <i class="fa-solid fa-xmark"></i> Deny
                </button>
              </div>
            </div>
          `;
        });
      } catch (err) {
        console.error("Approvals fetch error", err);
      }
    }

    async function approveAction(apprId) {
      try {
        await fetch(`/api/approvals/${apprId}/approve`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({decided_by: 'dashboard_operator', reason: 'Approved via web UI'})
        });
        fetchApprovals();
        fetchStatus();
      } catch (err) {
        alert("Approval failed: " + err);
      }
    }

    async function denyAction(apprId) {
      try {
        await fetch(`/api/approvals/${apprId}/deny`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({decided_by: 'dashboard_operator', reason: 'Denied by operator'})
        });
        fetchApprovals();
      } catch (err) {
        alert("Denial failed: " + err);
      }
    }

    async function fetchReports() {
      try {
        const res = await fetch('/api/reports');
        const data = await res.json();
        const container = document.getElementById('reports-list');
        container.innerHTML = '';

        if (!data.reports || data.reports.length === 0) {
          container.innerHTML = '<div class="col-span-3 text-xs text-slate-500 py-4 text-center">No postmortem reports generated yet.</div>';
          return;
        }

        data.reports.forEach(r => {
          container.innerHTML += `
            <div class="bg-slate-950/60 border border-slate-800 p-4 rounded-lg flex items-center justify-between hover:border-slate-700 transition">
              <div>
                <span class="text-xs font-bold text-indigo-400 mono">${r.incident_id}</span>
                <p class="text-[11px] text-slate-400 mt-1">Generated ${r.modified.slice(0, 10)} (${r.size_bytes} bytes)</p>
              </div>
              <button onclick="viewPostmortem('${r.incident_id}')" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded border border-slate-700 transition flex items-center gap-1.5">
                <i class="fa-solid fa-eye"></i> View
              </button>
            </div>
          `;
        });
      } catch (err) {
        console.error("Reports fetch error", err);
      }
    }

    async function viewEvidence(eid) {
      document.getElementById('modal-ev-id').innerText = eid;
      document.getElementById('modal-ev-content').innerText = 'Loading telemetry...';
      document.getElementById('evidence-modal').classList.remove('hidden');

      try {
        const res = await fetch(`/api/evidence/${eid}`);
        const data = await res.json();
        document.getElementById('modal-ev-content').innerText = JSON.stringify(data, null, 2);
      } catch (err) {
        document.getElementById('modal-ev-content').innerText = 'Error fetching evidence: ' + err;
      }
    }

    function closeEvidenceModal() {
      document.getElementById('evidence-modal').classList.add('hidden');
    }

    async function viewPostmortem(incId) {
      document.getElementById('pm-modal-title').innerText = `Incident Postmortem: ${incId}`;
      document.getElementById('pm-modal-body').innerText = 'Loading markdown postmortem...';
      document.getElementById('postmortem-modal').classList.remove('hidden');

      try {
        const res = await fetch(`/api/reports/${incId}`);
        const data = await res.json();
        document.getElementById('pm-modal-body').innerText = data.markdown;
      } catch (err) {
        document.getElementById('pm-modal-body').innerText = 'Error loading postmortem: ' + err;
      }
    }

    function closePostmortemModal() {
      document.getElementById('postmortem-modal').classList.add('hidden');
    }

    function openChaosModal() {
      document.getElementById('chaos-modal').classList.remove('hidden');
    }

    function closeChaosModal() {
      document.getElementById('chaos-modal').classList.add('hidden');
    }

    async function executeInjectChaos() {
      const failure = document.getElementById('chaos-type').value;
      const service = document.getElementById('chaos-target').value;
      closeChaosModal();

      try {
        await fetch('/api/chaos/inject', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({failure, service})
        });
        fetchStatus();
      } catch (err) {
        alert("Failed to inject chaos: " + err);
      }
    }

    async function triggerResetChaos() {
      try {
        await fetch('/api/chaos/clear', {method: 'POST'});
        fetchStatus();
        fetchApprovals();
      } catch (err) {
        alert("Reset failed: " + err);
      }
    }

    async function openBenchmarkModal() {
      document.getElementById('benchmark-modal').classList.remove('hidden');
      try {
        const res = await fetch('/api/metrics');
        const data = await res.json();
        const tbody = document.getElementById('benchmark-table-body');
        tbody.innerHTML = '';

        data.benchmarks.forEach(b => {
          tbody.innerHTML += `
            <tr class="hover:bg-slate-800/40">
              <td class="py-2.5 px-3 font-semibold text-white">${b.name}</td>
              <td class="py-2.5 px-3 mono text-slate-300">${b.root_cause_service}</td>
              <td class="py-2.5 px-3 mono">${b.ai_ttd_s}s</td>
              <td class="py-2.5 px-3 mono">${b.ai_ttdiag_s}s</td>
              <td class="py-2.5 px-3 mono font-bold text-emerald-400">${b.ai_ttr_s}s</td>
              <td class="py-2.5 px-3 mono text-slate-400">${b.manual_ttr_s}s</td>
              <td class="py-2.5 px-3 mono font-bold text-indigo-400">${b.speedup}x</td>
              <td class="py-2.5 px-3 mono">${b.accuracy * 100}%</td>
            </tr>
          `;
        });
      } catch (err) {
        console.error("Benchmark fetch error", err);
      }
    }

    function closeBenchmarkModal() {
      document.getElementById('benchmark-modal').classList.add('hidden');
    }

    // Auto-refresh loop (every 2.5 seconds)
    fetchStatus();
    fetchApprovals();
    fetchReports();
    setInterval(fetchStatus, 2500);
    setInterval(fetchApprovals, 3000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, summary="Interactive Dashboard UI")
async def dashboard_index() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


def main() -> int:
    import uvicorn
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    print(f"Starting AI-SRE Dashboard on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
