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
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
def api_status() -> dict[str, Any]:
    deps = get_services()
    sup: Supervisor = deps["sup"]
    obs: Observability = deps["obs"]
    detector: IncidentDetector = deps["detector"]
    fixer: Fixer = deps["fixer"]

    # Run auto-detection step on poll so active incidents and approvals are created in real time
    try:
        detector.auto_remediate = True
        detector.run_once(poll_metrics=False)
    except Exception:
        pass

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
    recent_incidents = detector.list_incidents(limit=15)

    return {
        "services": service_cards,
        "active_incident": active_inc.to_dict() if active_inc else None,
        "recent_incidents": [inc.to_dict() for inc in recent_incidents],
        "pending_approvals_count": len(pending_approvals),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


@app.get("/api/incidents", summary="Active and recent incidents")
def api_incidents() -> dict[str, Any]:
    deps = get_services()
    detector: IncidentDetector = deps["detector"]
    active = detector.get_active_incident()
    recent = detector.list_incidents(limit=15)
    active_dict = active.to_dict() if active else None
    recent_dicts = [inc.to_dict() for inc in recent]
    all_incs = ([active_dict] if active_dict else []) + [i for i in recent_dicts if not active_dict or i.get("id") != active_dict.get("id")]
    return {
        "active": active_dict,
        "recent": recent_dicts,
        "incidents": all_incs,
    }


@app.get("/api/incidents/{incident_id}/timeline", summary="Incident timeline with evidence citations")
def api_incident_timeline(incident_id: str) -> dict[str, Any]:
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
def api_evidence(evidence_id: str) -> dict[str, Any]:
    obs = get_observability()
    rec = obs.get_evidence(evidence_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Evidence ID '{evidence_id}' not found in database or logs")
    return rec


@app.get("/api/approvals", summary="List pending and decided fix approvals")
def api_approvals() -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    pending = fixer.list_approvals(status="pending")
    history = fixer.list_approvals(status=None, limit=20)
    return {
        "pending": [asdict(a) for a in pending],
        "approvals": [asdict(a) for a in pending],
        "history": [asdict(a) for a in history if a.status != "pending"],
        "count": len(pending),
    }


@app.post("/api/approvals/{approval_id}/approve", summary="Human approves a pending fix")
def api_approve(approval_id: str, req: ApprovalDecisionRequest) -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    appr = fixer.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")

    decided_by = req.decided_by or "human_operator"
    # Execute approved fix immediately without blocking the web event loop on 30s verify
    res = fixer.execute_approved(approval_id, decided_by=decided_by, skip_verify=True)

    # Run verification asynchronously in background thread so incident auto-resolves when healthy
    def _background_verify() -> None:
        try:
            svc = res.service
            if svc:
                v_res = fixer.verify_recovery(svc, timeout=10.0)
                if v_res.get("recovered"):
                    inc_id = appr.incident_id
                    if inc_id and deps["detector"]:
                        deps["detector"].update_incident_status(inc_id, "resolved", metadata_update={"fix_result": res.to_dict()})
        except Exception:
            pass

    import threading
    threading.Thread(target=_background_verify, daemon=True, name=f"verify_{approval_id}").start()

    updated_appr = fixer.get_approval(approval_id)
    return {
        "approval": asdict(updated_appr) if updated_appr else {"id": approval_id, "status": "approved", "decided_by": decided_by},
        "result": res.to_dict(),
    }


@app.post("/api/approvals/{approval_id}/deny", summary="Human denies a pending fix")
def api_deny(approval_id: str, req: ApprovalDecisionRequest) -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    appr = fixer.get_approval(approval_id)
    if not appr:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")

    decided = fixer.deny(approval_id, decided_by=req.decided_by or "human_operator", reason=req.reason or "Denied by human operator")
    return {"approval": asdict(decided)}


@app.post("/api/approvals/simulate", summary="Generate a simulated pending ask-first rollback approval for testing")
async def api_simulate_approval() -> dict[str, Any]:
    deps = get_services()
    fixer: Fixer = deps["fixer"]
    res = fixer.apply_fix(
        {"action": "rollback", "args": {"service": "payments", "to_version": "1.0.0"}},
        incident="INC-1",
    )
    return {
        "ok": True,
        "approval_id": res.approval_id,
        "status": res.status,
    }


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


@app.get("/api/reports", summary="List generated postmortem reports with pagination and filtering")
async def api_reports(
    page: int = 1,
    page_size: int = 9,
    status: str | None = None,
    severity: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    return service.list_reports(
        page=page,
        page_size=page_size,
        status=status,
        severity=severity,
        search=search,
    )


@app.get("/api/reports/{incident_id}", summary="Get modular postmortem report metadata, sections index, and markdown")
async def api_get_report(incident_id: str) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    report = service.get_report(clean_id)
    if not report:
        legacy = storage.get_full_markdown(clean_id)
        if legacy:
            return {
                "incident_id": clean_id,
                "markdown": legacy,
                "metadata": storage.get_report_metadata(clean_id) or {},
                "sections": storage.get_sections_index(clean_id),
            }
        raise HTTPException(status_code=404, detail=f"Postmortem report '{clean_id}' not found")
    return report


@app.get("/api/reports/{incident_id}/sections", summary="Get available report sections metadata")
async def api_get_report_sections(incident_id: str) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    sections = service.get_sections_index(clean_id)
    return {"incident_id": clean_id, "sections": sections}


@app.get("/api/reports/{incident_id}/sections/{section_id}", summary="Get individual report section content")
async def api_get_report_section(incident_id: str, section_id: str) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    section = service.get_section(clean_id, section_id)
    if not section:
        raise HTTPException(status_code=404, detail=f"Section '{section_id}' not found for incident '{clean_id}'")
    return section


@app.get("/api/reports/{incident_id}/export", summary="Export full modular postmortem as formatted markdown")
async def api_export_report(incident_id: str) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    markdown, filename = service.get_export_markdown(clean_id)
    return {
        "incident_id": clean_id,
        "filename": filename,
        "markdown": markdown,
    }


@app.get("/api/reports/{incident_id}/download", summary="Download full modular postmortem as .md attachment")
async def api_download_report(incident_id: str) -> Response:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    markdown, filename = service.get_export_markdown(clean_id)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/reports/{incident_id}/sections/{section_id}/export", summary="Export individual report section markdown")
async def api_export_report_section(incident_id: str, section_id: str) -> dict[str, Any]:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    markdown, filename = service.get_section_markdown(clean_id, section_id)
    return {
        "incident_id": clean_id,
        "section_id": section_id,
        "filename": filename,
        "markdown": markdown,
    }


@app.get("/api/reports/{incident_id}/sections/{section_id}/download", summary="Download individual report section as .md")
async def api_download_report_section(incident_id: str, section_id: str) -> Response:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    markdown, filename = service.get_section_markdown(clean_id, section_id)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/reports/{incident_id}/zip", summary="Download all 8 report sections bundled as a ZIP archive")
async def api_download_report_zip(incident_id: str) -> Response:
    from sre.reports import ReportService, ReportStorage
    clean_id = incident_id.replace(" ", "_").replace(".md", "").strip()
    storage = ReportStorage(reports_dir=REPORTS_DIR)
    service = ReportService(storage=storage)
    zip_bytes, zip_filename = service.get_zip_archive(clean_id)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


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

        # Immediately trigger detector & auto-remediation so incident and approval cards appear instantly
        try:
            detector: IncidentDetector = deps["detector"]
            detector.auto_remediate = True
            detector.run_once(poll_metrics=False)
        except Exception:
            pass

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


@app.post("/api/services/{service_name}/restart", summary="Restart a microservice")
async def api_restart_service(service_name: str) -> dict[str, Any]:
    deps = get_services()
    sup: Supervisor = deps["sup"]
    try:
        info = sup.restart_service(service_name)
        return {"ok": True, "service": service_name, "info": asdict(info)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/services/restart-all", summary="Restart all microservices")
async def api_restart_all() -> dict[str, Any]:
    deps = get_services()
    sup: Supervisor = deps["sup"]
    try:
        results = sup.start_all()
        return {"ok": True, "services": {k: asdict(v) for k, v in results.items()}}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
      <button onclick="restartAllServices()" class="px-3.5 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg shadow transition flex items-center gap-1.5 cursor-pointer">
        <i class="fa-solid fa-arrows-rotate"></i> Restart All
      </button>
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

    <!-- Human Approvals Section -->
    <div id="approvals-section" class="bg-slate-900/60 border border-slate-800 rounded-xl p-5 shadow-sm">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center space-x-3">
          <div class="h-8 w-8 rounded-full bg-amber-500/20 text-amber-400 flex items-center justify-center font-bold">
            <i class="fa-solid fa-user-shield"></i>
          </div>
          <div>
            <h2 class="text-base font-bold text-amber-300" id="approvals-title">Human Approval Queue (Tier: Ask-First)</h2>
            <p class="text-xs text-slate-400">Autonomous code blocks high-risk actions (rollback, config) until explicit operator authorization.</p>
          </div>
        </div>
        <div class="flex items-center space-x-2">
          <button onclick="simulateApproval()" class="text-xs px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition flex items-center gap-1.5" title="Generate a pending rollback approval to test the approval button">
            <i class="fa-solid fa-vial"></i> Test Approval Flow
          </button>
          <span class="text-xs bg-slate-800 text-slate-300 px-2.5 py-1 rounded-full border border-slate-700 font-medium" id="pending-count">0 Pending</span>
        </div>
      </div>
      <div id="approvals-cards" class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="col-span-full p-4 bg-slate-950/60 border border-slate-800 rounded-lg text-xs text-slate-400 flex items-center justify-between">
          <div class="flex items-center space-x-2.5">
            <i class="fa-solid fa-shield-check text-emerald-400 text-base"></i>
            <span>Zero pending actions. Safe fixes (restart, clear chaos) auto-execute autonomously; risky actions pause here for human approval.</span>
          </div>
          <button onclick="simulateApproval()" class="text-xs text-indigo-400 hover:text-indigo-300 underline font-semibold">
            Simulate Pending Rollback
          </button>
        </div>
      </div>
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
          <div class="flex items-center space-x-2">
            <label class="text-[11px] text-slate-400">Incident:</label>
            <select id="incident-select" onchange="onSelectIncident(this.value)" class="bg-slate-950 border border-slate-700 text-slate-200 text-xs rounded px-2 py-1 outline-none cursor-pointer">
              <option value="">Zero Incidents</option>
            </select>
            <span class="text-xs text-slate-400 font-semibold" id="timeline-incident-label">Baseline Normal</span>
          </div>
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
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2.5">
          <i class="fa-solid fa-folder-tree text-indigo-400"></i>
          <div>
            <h3 class="font-bold text-white text-sm">Categorized Incident Reports & Postmortems</h3>
            <p class="text-[11px] text-slate-400">Validated Incident Postmortems & Modular Reports with strictly enforced evidence citations</p>
          </div>
        </div>

        <!-- Filters & Search Toolbar -->
        <div class="flex flex-wrap items-center gap-2">
          <!-- Search input -->
          <div class="relative">
            <i class="fa-solid fa-magnifying-glass absolute left-3 top-2.5 text-slate-500 text-xs"></i>
            <input type="text" id="report-search-input" oninput="onReportSearchChange()" placeholder="Search incidents, services..."
              class="bg-slate-950 border border-slate-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500 w-48 md:w-56" />
          </div>

          <!-- Status filter -->
          <select id="report-filter-status" onchange="onReportFilterChange()" class="bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-indigo-500">
            <option value="all">All Statuses</option>
            <option value="resolved">Resolved</option>
            <option value="open">Open</option>
            <option value="escalated">Escalated</option>
          </select>

          <!-- Severity filter -->
          <select id="report-filter-severity" onchange="onReportFilterChange()" class="bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-indigo-500">
            <option value="all">All Severities</option>
            <option value="SEV-1">SEV-1 (Critical)</option>
            <option value="SEV-2">SEV-2 (Major)</option>
          </select>

          <button onclick="fetchReports(1)" title="Refresh Reports" class="p-1.5 bg-slate-800 hover:bg-slate-700 rounded-lg text-slate-300 text-xs border border-slate-700">
            <i class="fa-solid fa-rotate-right"></i>
          </button>
        </div>
      </div>

      <!-- Report Cards Grid -->
      <div id="reports-list" class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <!-- Reports cards inserted via JS -->
      </div>

      <!-- Pagination Toolbar -->
      <div id="reports-pagination" class="flex items-center justify-between border-t border-slate-800 pt-3 text-xs text-slate-400">
        <span id="reports-page-info">Showing 0 reports</span>
        <div class="flex items-center space-x-2">
          <button id="reports-prev-btn" onclick="changeReportsPage(-1)" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed">
            <i class="fa-solid fa-chevron-left mr-1"></i> Prev
          </button>
          <span id="reports-page-curr" class="font-bold text-slate-200">1</span>
          <button id="reports-next-btn" onclick="changeReportsPage(1)" class="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed">
            Next <i class="fa-solid fa-chevron-right ml-1"></i>
          </button>
        </div>
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

  <!-- Modular Categorized Report Viewer Modal -->
  <div id="postmortem-modal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden flex items-center justify-center p-3 md:p-6">
    <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-6xl w-full h-[90vh] shadow-2xl flex flex-col overflow-hidden">
      <!-- Modal Top Header -->
      <div class="bg-slate-950 px-6 py-3.5 border-b border-slate-800 flex items-center justify-between shrink-0">
        <div class="flex items-center space-x-3">
          <div class="flex items-center space-x-2 text-xs text-slate-400">
            <span class="hover:text-slate-200 cursor-pointer" onclick="closePostmortemModal()">Reports</span>
            <span>/</span>
            <span class="font-bold mono text-indigo-400" id="mod-rep-inc-id">INC-1</span>
            <span>/</span>
            <span class="text-white font-medium" id="mod-rep-breadcrumb-cat">Executive Summary</span>
          </div>
          <span id="mod-rep-sev-pill" class="text-[10px] font-bold px-2 py-0.5 rounded border">SEV-1</span>
          <span id="mod-rep-status-pill" class="text-[10px] font-bold px-2 py-0.5 rounded border">RESOLVED</span>
        </div>

        <div class="flex items-center space-x-2">
          <!-- Existing Download .md button (unchanged) -->
          <button onclick="downloadMarkdownReport()" id="mod-rep-download-btn" class="px-3 py-1 bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-200 text-xs font-semibold rounded border border-indigo-500/40 transition flex items-center gap-1.5" title="Download complete postmortem as .md">
            <i class="fa-solid fa-file-arrow-down"></i> Download .md
          </button>
          <!-- New Download All (ZIP) button -->
          <button onclick="downloadAllZip()" id="mod-rep-zip-btn" class="px-3 py-1 bg-purple-600/30 hover:bg-purple-600/50 text-purple-200 text-xs font-semibold rounded border border-purple-500/40 transition flex items-center gap-1.5" title="Download all eight individual Markdown files in one ZIP archive">
            <i class="fa-solid fa-file-zipper"></i> Download All (ZIP)
          </button>
          <button onclick="toggleRawMarkdownView()" id="mod-rep-raw-btn" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded border border-slate-700 transition flex items-center gap-1.5">
            <i class="fa-solid fa-file-code"></i> Full Markdown
          </button>
          <button onclick="closePostmortemModal()" class="text-slate-400 hover:text-white p-1">
            <i class="fa-solid fa-xmark text-lg"></i>
          </button>
        </div>
      </div>

      <!-- Modal Body: Split Navigation and Content -->
      <div class="flex-1 flex overflow-hidden">
        <!-- Left Sidebar: Category Navigation -->
        <aside class="w-64 bg-slate-950/80 border-r border-slate-800 p-3 flex flex-col justify-between shrink-0 overflow-y-auto">
          <div class="space-y-1" id="mod-rep-nav-items">
            <!-- Nav buttons injected via JS -->
          </div>

          <!-- Bottom helper / stats -->
          <div class="p-3 bg-slate-900/60 border border-slate-800/80 rounded-lg text-[11px] text-slate-400 space-y-1.5 mt-4">
            <div class="flex items-center justify-between text-slate-300">
              <span>Evidence Cited:</span>
              <span id="mod-rep-sidebar-ev-count" class="font-bold mono text-indigo-400">0</span>
            </div>
            <div class="flex items-center justify-between text-slate-300">
              <span>Speedup vs Manual:</span>
              <span id="mod-rep-sidebar-speedup" class="font-bold mono text-emerald-400">-</span>
            </div>
          </div>
        </aside>

        <!-- Main Content Area: Category Display -->
        <main class="flex-1 bg-slate-900/50 p-6 overflow-y-auto" id="mod-rep-main-panel">
          <!-- Loading skeleton -->
          <div id="mod-rep-loading" class="hidden flex flex-col items-center justify-center h-64 space-y-3">
            <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-400"></div>
            <span class="text-xs text-slate-400">Loading section telemetry...</span>
          </div>

          <!-- Active Category Container -->
          <div id="mod-rep-content" class="space-y-6">
            <!-- Dynamic content per category injected via JS -->
          </div>

          <!-- Full Raw Markdown Container (initially hidden) -->
          <div id="mod-rep-raw-markdown" class="hidden space-y-3">
            <div class="flex items-center justify-between">
              <span class="text-xs text-slate-400">Full Raw Markdown Report (Exportable)</span>
              <button onclick="copyRawMarkdown()" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs rounded border border-slate-700 flex items-center gap-1.5">
                <i class="fa-solid fa-copy"></i> Copy Text
              </button>
            </div>
            <pre id="pm-modal-body" class="p-4 bg-slate-950 border border-slate-800 rounded-xl text-xs text-slate-200 mono whitespace-pre-wrap leading-relaxed overflow-x-auto"></pre>
          </div>
        </main>
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
    let selectedIncidentId = null;
    let userManuallySelected = false;

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderServices(data.services);
        renderIncidents(data.active_incident, data.recent_incidents || []);
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

            <div class="mt-3 pt-2.5 border-t border-slate-800 flex justify-between items-center">
              <span class="text-[10px] text-slate-400 font-mono">Process: :${s.port}</span>
              <button onclick="restartService('${s.service}')" class="px-3 py-1 text-xs font-bold bg-indigo-600 hover:bg-indigo-500 text-white rounded-md shadow transition flex items-center gap-1.5 cursor-pointer">
                <i class="fa-solid fa-rotate-right text-[11px]"></i> Restart
              </button>
            </div>
          </div>
        `;
      });
    }

    let currentTimelineIncId = null;
    let currentTimelineStatus = null;

    function onSelectIncident(incId) {
      userManuallySelected = true;
      selectedIncidentId = incId;
      if (incId) {
        currentTimelineIncId = null; // Force timeline reload
        fetchTimeline(incId);
      }
    }

    function renderIncidents(activeInc, recentIncidents) {
      const banner = document.getElementById('active-incident-banner');
      const timelineLabel = document.getElementById('timeline-incident-label');
      const select = document.getElementById('incident-select');

      // 1. Active Incident Banner
      if (!activeInc) {
        banner.className = 'hidden';
        activeIncidentId = null;
      } else {
        activeIncidentId = activeInc.id;
        banner.className = 'bg-rose-950/40 border border-rose-500/50 rounded-xl p-4 text-rose-200 flex items-center justify-between pulse-glow';
        banner.innerHTML = `
          <div class="flex items-center space-x-3">
            <span class="text-rose-400 text-xl"><i class="fa-solid fa-triangle-exclamation"></i></span>
            <div>
              <h4 class="font-bold text-white text-sm flex items-center gap-2">
                ACTIVE INCIDENT: ${activeInc.id}
                <span class="text-[10px] px-2 py-0.5 rounded bg-rose-900/60 border border-rose-700 text-rose-300 uppercase">${activeInc.status}</span>
              </h4>
              <p class="text-xs text-rose-300">
                Trigger: <code class="text-white bg-rose-900/40 px-1 py-0.5 rounded">${activeInc.trigger_rule}</code> | 
                Affected: ${activeInc.affected_services.join(', ')} | 
                Detected at: ${(activeInc.created_at && activeInc.created_at.includes('T')) ? activeInc.created_at.split('T')[1].slice(0,8) : (activeInc.created_at || '')} (TTD: ${activeInc.ttd_seconds.toFixed(1)}s)
              </p>
            </div>
          </div>
          <div class="flex items-center space-x-2">
            <button onclick="viewEvidence('${activeInc.evidence[0]}')" class="px-3 py-1.5 bg-rose-900/50 hover:bg-rose-800 text-rose-200 text-xs font-semibold rounded-lg border border-rose-700 transition">
              <i class="fa-solid fa-magnifying-glass"></i> Inspect Evidence
            </button>
          </div>
        `;
      }

      // 2. Build full incident options
      const allIncidents = [];
      if (activeInc) {
        allIncidents.push(activeInc);
      }
      for (const r of recentIncidents) {
        if (!allIncidents.some(i => i.id === r.id)) {
          allIncidents.push(r);
        }
      }

      // 3. Update dropdown options
      if (allIncidents.length === 0) {
        select.innerHTML = '<option value="">Zero Incidents</option>';
      } else {
        const optionsHtml = allIncidents.map(inc => {
          const isActive = activeInc && activeInc.id === inc.id;
          const prefix = isActive ? '🔥 [ACTIVE] ' : '';
          return `<option value="${inc.id}">${prefix}${inc.id} (${inc.status.toUpperCase()}) - ${inc.trigger_rule}</option>`;
        }).join('');
        if (select.innerHTML !== optionsHtml) {
          select.innerHTML = optionsHtml;
        }
      }

      // 4. Determine which incident to display in the timeline
      let targetId = null;
      if (activeInc && !userManuallySelected) {
        targetId = activeInc.id;
      } else if (userManuallySelected && allIncidents.some(i => i.id === selectedIncidentId)) {
        targetId = selectedIncidentId;
      } else if (allIncidents.length > 0) {
        targetId = allIncidents[0].id;
      }

      if (targetId) {
        select.value = targetId;
        selectedIncidentId = targetId;
        const incObj = allIncidents.find(i => i.id === targetId);
        const incStatus = incObj ? incObj.status : '';
        timelineLabel.innerText = incObj ? `${incObj.id} (${incObj.status.toUpperCase()})` : targetId;

        // Fetch only if target or its status changed, avoiding DOM thrashing on every poll
        if (targetId !== currentTimelineIncId || incStatus !== currentTimelineStatus) {
          currentTimelineIncId = targetId;
          currentTimelineStatus = incStatus;
          fetchTimeline(targetId);
        }
      } else {
        currentTimelineIncId = null;
        currentTimelineStatus = null;
        timelineLabel.innerText = 'Baseline Normal';
        document.getElementById('timeline-container').innerHTML = `
          <div class="text-center py-10 text-slate-500 text-xs">
            <i class="fa-solid fa-circle-check text-2xl text-emerald-500/40 mb-2 block"></i>
            All 4 microservices operating within SLO thresholds. Zero active breaches.<br>
            <span class="text-slate-400 mt-2 block">Click <b>Trigger Failure</b> above or run a chaos command to see live incident timeline.</span>
          </div>
        `;
      }
    }

    async function fetchTimeline(incId) {
      if (!incId) return;
      try {
        const res = await fetch(`/api/incidents/${incId}/timeline`);
        if (!res.ok) return;
        const data = await res.json();
        renderIncidentTimeline(data.timeline, incId);
      } catch (err) {
        console.error("Timeline fetch error", err);
      }
    }

    function renderIncidentTimeline(timeline, incId) {
      const container = document.getElementById('timeline-container');
      if (!timeline || timeline.length === 0) {
        container.innerHTML = `
          <div class="text-center py-8 text-slate-500 text-xs">
            <i class="fa-solid fa-clock-rotate-left text-2xl text-slate-600 mb-2 block"></i>
            Timeline for ${incId} is generating or no events recorded yet.
          </div>
        `;
        return;
      }
      container.innerHTML = '';

      timeline.forEach(t => {
        let badgeColor = 'bg-slate-800 text-slate-300';
        if (t.event === 'detected') badgeColor = 'bg-rose-900/60 text-rose-300 border border-rose-700';
        if (t.event === 'diagnosed') badgeColor = 'bg-indigo-900/60 text-indigo-300 border border-indigo-700';
        if (t.event === 'mitigated') badgeColor = 'bg-amber-900/60 text-amber-300 border border-amber-700';
        if (t.event === 'resolved') badgeColor = 'bg-emerald-900/60 text-emerald-300 border border-emerald-700';

        const evLinks = (t.evidence_ids || []).map(eid => `
          <button onclick="viewEvidence('${eid}')" class="text-emerald-400 hover:text-emerald-300 underline font-semibold mr-1.5 mono cursor-pointer">
            ${eid}
          </button>
        `).join('');

        const timeStr = (t.ts && t.ts.includes('T')) ? t.ts.split('T')[1].slice(0,8) : (t.ts || '--:--:--');
        container.innerHTML += `
          <div class="flex items-start space-x-3 text-xs border-b border-slate-800/60 pb-2.5">
            <span class="mono text-slate-500 pt-0.5 text-[11px]">${timeStr}</span>
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
        const title = document.getElementById('approvals-title');

        const pendingList = data.pending || data.approvals || [];

        if (pendingList.length === 0) {
          section.className = 'bg-slate-900/60 border border-slate-800 rounded-xl p-5 shadow-sm';
          count.className = 'text-xs bg-slate-800 text-slate-300 px-2.5 py-1 rounded-full border border-slate-700 font-medium';
          count.innerText = '0 Pending';
          if (title) title.innerText = 'Human Approval Queue (Tier: Ask-First)';
          cards.innerHTML = `
            <div class="col-span-full p-4 bg-slate-950/60 border border-slate-800 rounded-lg text-xs text-slate-400 flex items-center justify-between">
              <div class="flex items-center space-x-2.5">
                <i class="fa-solid fa-shield-check text-emerald-400 text-base"></i>
                <span>Zero pending actions. Safe fixes (restart, clear chaos) auto-execute autonomously; risky actions pause here for human authorization.</span>
              </div>
              <button onclick="simulateApproval()" class="text-xs text-indigo-400 hover:text-indigo-300 underline font-semibold">
                Simulate Rollback Approval
              </button>
            </div>
          `;
          return;
        }

        section.className = 'bg-amber-950/30 border-2 border-amber-500/50 rounded-xl p-5 shadow-lg';
        count.className = 'text-xs bg-amber-900/80 text-amber-300 px-2.5 py-1 rounded-full border border-amber-600 font-bold';
        count.innerText = `${pendingList.length} Action Pending`;
        if (title) title.innerText = '⚠️ Human Approval Required (Tier: Ask-First)';
        cards.innerHTML = '';

        pendingList.forEach(a => {
          const svcName = a.service || (a.args && a.args.service) || 'system';
          cards.innerHTML += `
            <div class="bg-slate-900 border border-amber-500/40 rounded-lg p-4 space-y-3 shadow-md">
              <div class="flex justify-between items-start">
                <div>
                  <span class="text-xs font-bold text-amber-400 uppercase mono">${a.id}</span>
                  <h4 class="text-sm font-bold text-white mt-0.5">Proposed Action: <code class="text-amber-300 font-bold">${a.action}</code> on <code>${svcName}</code></h4>
                </div>
                <span class="text-[10px] bg-amber-950 text-amber-300 border border-amber-800 px-2 py-0.5 rounded uppercase font-semibold">Tier: Ask-First</span>
              </div>
              <p class="text-xs text-slate-300">Target Reason: ${a.reason || 'Remediating failure with safe verification.'}</p>
              <div class="flex space-x-2 pt-1">
                <button onclick="approveAction('${a.id}')" class="flex-1 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs rounded transition flex items-center justify-center gap-1.5 shadow">
                  <i class="fa-solid fa-check"></i> Approve & Apply
                </button>
                <button onclick="denyAction('${a.id}')" class="px-4 py-1.5 bg-rose-900/60 hover:bg-rose-800 text-rose-300 font-bold text-xs rounded transition flex items-center justify-center gap-1.5 border border-rose-700">
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

    async function simulateApproval() {
      try {
        await fetch('/api/approvals/simulate', {method: 'POST'});
        fetchApprovals();
      } catch (err) {
        alert("Failed to simulate approval: " + err);
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

    // --- Categorized Modular Reports & Viewer ---
    let currentReportPage = 1;
    let currentReportTotalPages = 1;
    let currentReportIncId = null;
    let currentReportActiveSection = 'executive_summary';
    let currentReportRawMarkdown = '';
    const reportSectionsCache = {};
    const reportMetadataCache = {};

    const SECTION_DEFS = [
      { id: 'executive_summary', title: 'Executive Summary', icon: 'fa-clipboard-check' },
      { id: 'impact', title: 'Incident Impact', icon: 'fa-chart-pie' },
      { id: 'root_cause', title: 'Root Cause Analysis', icon: 'fa-magnifying-glass-chart' },
      { id: 'evidence', title: 'Evidence & Observability', icon: 'fa-database' },
      { id: 'timeline', title: 'Incident Timeline', icon: 'fa-clock-rotate-left' },
      { id: 'remediation', title: 'Remediation Actions', icon: 'fa-wrench' },
      { id: 'verification', title: 'Recovery Verification', icon: 'fa-shield-halved' },
      { id: 'prevention', title: 'Prevention & Recommendations', icon: 'fa-lightbulb' }
    ];

    function formatEvidencePill(eid) {
      if (!eid) return '';
      return `<button onclick="viewEvidence('${eid}')" class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-mono bg-indigo-950/90 hover:bg-indigo-900 text-indigo-300 border border-indigo-700/60 transition cursor-pointer gap-1 mx-0.5" title="Inspect evidence ${eid}">
        <i class="fa-solid fa-link text-[9px] text-indigo-400"></i>${eid}
      </button>`;
    }

    function linkifyEvidenceText(text) {
      if (!text) return '';
      return String(text).replace(/\\b(LOG-\\d+|MET-\\d+|CHG-\\d+)\\b/g, (match) => formatEvidencePill(match));
    }

    let reportSearchTimeout = null;
    function onReportSearchChange() {
      clearTimeout(reportSearchTimeout);
      reportSearchTimeout = setTimeout(() => {
        fetchReports(1);
      }, 250);
    }

    function onReportFilterChange() {
      fetchReports(1);
    }

    function changeReportsPage(delta) {
      const target = currentReportPage + delta;
      if (target >= 1 && target <= currentReportTotalPages) {
        fetchReports(target);
      }
    }

    async function fetchReports(page = 1) {
      currentReportPage = page;
      const status = document.getElementById('report-filter-status')?.value || 'all';
      const severity = document.getElementById('report-filter-severity')?.value || 'all';
      const search = document.getElementById('report-search-input')?.value.trim() || '';

      const params = new URLSearchParams({ page: String(page), page_size: '6' });
      if (status && status !== 'all') params.append('status', status);
      if (severity && severity !== 'all') params.append('severity', severity);
      if (search) params.append('search', search);

      try {
        const res = await fetch(`/api/reports?${params.toString()}`);
        const data = await res.json();
        const container = document.getElementById('reports-list');
        if (!container) return;
        container.innerHTML = '';

        const reports = data.items || data.reports || [];
        const total = data.total !== undefined ? data.total : reports.length;
        currentReportTotalPages = data.total_pages || Math.max(1, Math.ceil(total / (data.page_size || 6)));

        // Update pagination UI
        const pageInfo = document.getElementById('reports-page-info');
        if (pageInfo) {
          if (total === 0) {
            pageInfo.innerText = 'No matching reports';
          } else {
            const start = (page - 1) * (data.page_size || 6) + 1;
            const end = Math.min(page * (data.page_size || 6), total);
            pageInfo.innerText = `Showing ${start}-${end} of ${total} reports`;
          }
        }
        const pageCurr = document.getElementById('reports-page-curr');
        if (pageCurr) pageCurr.innerText = String(page);
        const prevBtn = document.getElementById('reports-prev-btn');
        if (prevBtn) prevBtn.disabled = page <= 1;
        const nextBtn = document.getElementById('reports-next-btn');
        if (nextBtn) nextBtn.disabled = page >= currentReportTotalPages;

        if (reports.length === 0) {
          container.innerHTML = `
            <div class="col-span-3 text-center py-10 bg-slate-950/40 rounded-xl border border-slate-800/80">
              <i class="fa-solid fa-inbox text-slate-600 text-2xl mb-2"></i>
              <p class="text-xs text-slate-400">No incident reports found matching your criteria.</p>
            </div>`;
          return;
        }

        reports.forEach(r => {
          const incId = r.incident_id || r.name?.replace('.md', '') || 'INC';
          const title = r.title || `Incident ${incId}`;
          const statusText = (r.status || 'RESOLVED').toUpperCase();
          const statusColor = statusText === 'RESOLVED' ? 'bg-emerald-950/80 text-emerald-400 border-emerald-800' :
                              (statusText === 'OPEN' ? 'bg-rose-950/80 text-rose-400 border-rose-800' : 'bg-amber-950/80 text-amber-400 border-amber-800');
          const sev = r.severity || 'SEV-1';
          const sevColor = sev === 'SEV-1' ? 'bg-rose-950/80 text-rose-400 border-rose-800' : 'bg-amber-950/80 text-amber-400 border-amber-800';
          const affected = (r.affected_services || []).join(', ') || 'None';
          const dateStr = r.created_at ? new Date(r.created_at).toLocaleString() : (r.modified ? r.modified.slice(0, 16).replace('T', ' ') : 'Recently');
          const speedup = r.speedup_factor ? `${r.speedup_factor}x` : '-';
          const evCount = r.evidence_count !== undefined ? r.evidence_count : 0;

          container.innerHTML += `
            <div class="bg-slate-950/60 border border-slate-800 p-4 rounded-xl flex flex-col justify-between hover:border-slate-700 transition shadow-sm space-y-3">
              <div class="flex items-start justify-between gap-2">
                <div>
                  <div class="flex items-center gap-1.5 flex-wrap">
                    <span class="text-xs font-bold text-indigo-400 mono">${incId}</span>
                    <span class="text-[10px] font-bold px-1.5 py-0.5 rounded border ${sevColor}">${sev}</span>
                    <span class="text-[10px] font-bold px-1.5 py-0.5 rounded border ${statusColor}">${statusText}</span>
                  </div>
                  <h4 class="text-xs font-semibold text-white mt-1.5 line-clamp-1" title="${title}">${title}</h4>
                </div>
                <span class="text-[10px] text-slate-500 shrink-0 mono">${dateStr}</span>
              </div>

              <div class="grid grid-cols-2 gap-2 text-[11px] bg-slate-900/50 p-2 rounded-lg border border-slate-800/60">
                <div>
                  <span class="text-slate-500 block text-[10px]">Affected Services</span>
                  <span class="text-slate-300 mono truncate block">${affected}</span>
                </div>
                <div>
                  <span class="text-slate-500 block text-[10px]">Speedup vs Human</span>
                  <span class="text-emerald-400 font-bold mono">${speedup}</span>
                </div>
              </div>

              <div class="flex items-center justify-between pt-1">
                <span class="text-[10px] text-slate-400 mono flex items-center gap-1">
                  <i class="fa-solid fa-link text-[9px] text-indigo-400"></i> ${evCount} Evidence Cited
                </span>
                <button onclick="openModularReport('${incId}')" class="px-3 py-1.5 bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-semibold rounded-lg border border-indigo-500/30 transition flex items-center gap-1.5">
                  <i class="fa-solid fa-eye"></i> View Report
                </button>
              </div>
            </div>
          `;
        });
      } catch (err) {
        console.error("Reports fetch error", err);
      }
    }

    async function openModularReport(incId, initialSection = 'executive_summary') {
      currentReportIncId = incId;
      currentReportActiveSection = initialSection;

      document.getElementById('mod-rep-inc-id').innerText = incId;
      document.getElementById('postmortem-modal').classList.remove('hidden');

      // Pre-populate left navigation so sidebar is immediately populated
      const navContainer = document.getElementById('mod-rep-nav-items');
      navContainer.innerHTML = '';
      SECTION_DEFS.forEach((def, idx) => {
        navContainer.innerHTML += `
          <div class="flex items-center justify-between group rounded-lg transition hover:bg-slate-900 pr-1.5" id="nav-item-wrap-${def.id}">
            <button id="nav-btn-${def.id}" onclick="switchReportCategory('${def.id}')"
              class="flex-1 text-left px-3 py-2 text-xs font-medium transition flex items-center gap-2.5 truncate text-slate-300">
              <i class="fa-solid ${def.icon || 'fa-circle'} text-[12px] text-slate-400 w-4 text-center"></i>
              <span class="truncate">${idx + 1}. ${def.title}</span>
            </button>
            <button onclick="downloadCategoryMarkdown(event, '${def.id}')"
              id="nav-dl-${def.id}"
              class="p-1.5 text-slate-500 hover:text-indigo-300 hover:bg-indigo-950/80 rounded text-[11px] transition shrink-0"
              title="Download ${def.title} (.md)">
              <i class="fa-solid fa-download"></i>
            </button>
          </div>
        `;
      });

      if (!reportSectionsCache[incId]) {
        reportSectionsCache[incId] = {};
      }

      try {
        const res = await fetch(`/api/reports/${incId}`);
        if (!res.ok) throw new Error("Failed to load report metadata");
        const data = await res.json();
        const meta = data.metadata || {};
        reportMetadataCache[incId] = meta;
        currentReportRawMarkdown = data.markdown || '';
        document.getElementById('pm-modal-body').innerText = currentReportRawMarkdown;

        // Header pills
        const sevPill = document.getElementById('mod-rep-sev-pill');
        const sev = meta.severity || 'SEV-1';
        sevPill.innerText = sev;
        sevPill.className = `text-[10px] font-bold px-2 py-0.5 rounded border ${
          sev === 'SEV-1' ? 'bg-rose-950/80 text-rose-400 border-rose-800' : 'bg-amber-950/80 text-amber-400 border-amber-800'
        }`;

        const statusPill = document.getElementById('mod-rep-status-pill');
        const status = (meta.status || 'RESOLVED').toUpperCase();
        statusPill.innerText = status;
        statusPill.className = `text-[10px] font-bold px-2 py-0.5 rounded border ${
          status === 'RESOLVED' ? 'bg-emerald-950/80 text-emerald-400 border-emerald-800' :
          (status === 'OPEN' ? 'bg-rose-950/80 text-rose-400 border-rose-800' : 'bg-amber-950/80 text-amber-400 border-amber-800')
        }`;

        // Sidebar footer stats
        document.getElementById('mod-rep-sidebar-ev-count').innerText = meta.evidence_count || '0';
        document.getElementById('mod-rep-sidebar-speedup').innerText = meta.speedup_factor ? `${meta.speedup_factor}x` : '-';

        // Switch to initial section
        await switchReportCategory(initialSection);
      } catch (err) {
        document.getElementById('mod-rep-content').innerHTML = `
          <div class="p-6 bg-rose-950/40 border border-rose-800/80 rounded-xl text-rose-300 text-xs">
            <i class="fa-solid fa-triangle-exclamation mr-2"></i> Failed to load report details: ${err.message}
          </div>`;
      }
    }

    async function switchReportCategory(sectionId) {
      currentReportActiveSection = sectionId;
      const incId = currentReportIncId;
      if (!incId) return;

      // Reset raw view if active
      document.getElementById('mod-rep-raw-markdown').classList.add('hidden');
      document.getElementById('mod-rep-content').classList.remove('hidden');
      document.getElementById('mod-rep-raw-btn').innerHTML = '<i class="fa-solid fa-file-code"></i> Full Markdown';

      // Update nav active states
      SECTION_DEFS.forEach(def => {
        const wrap = document.getElementById(`nav-item-wrap-${def.id}`);
        const btn = document.getElementById(`nav-btn-${def.id}`);
        if (wrap) {
          if (def.id === sectionId) {
            wrap.className = "flex items-center justify-between group rounded-lg transition bg-indigo-600/20 text-indigo-300 border border-indigo-500/30 pr-1.5";
            if (btn) btn.className = "flex-1 text-left px-3 py-2 text-xs font-semibold flex items-center gap-2.5 truncate text-indigo-300";
          } else {
            wrap.className = "flex items-center justify-between group rounded-lg transition hover:bg-slate-900 pr-1.5";
            if (btn) btn.className = "flex-1 text-left px-3 py-2 text-xs font-medium flex items-center gap-2.5 truncate text-slate-400 hover:text-white";
          }
        }
      });

      const secDef = SECTION_DEFS.find(d => d.id === sectionId) || { title: sectionId };
      document.getElementById('mod-rep-breadcrumb-cat').innerText = secDef.title;

      const contentBox = document.getElementById('mod-rep-content');
      const loading = document.getElementById('mod-rep-loading');

      // Check cache first
      if (reportSectionsCache[incId] && reportSectionsCache[incId][sectionId]) {
        renderSectionContent(sectionId, reportSectionsCache[incId][sectionId]);
        return;
      }

      // Lazy load section
      loading.classList.remove('hidden');
      contentBox.classList.add('hidden');

      try {
        const res = await fetch(`/api/reports/${incId}/sections/${sectionId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const sectionData = await res.json();
        reportSectionsCache[incId][sectionId] = sectionData;
        loading.classList.add('hidden');
        contentBox.classList.remove('hidden');
        renderSectionContent(sectionId, sectionData);
      } catch (err) {
        loading.classList.add('hidden');
        contentBox.classList.remove('hidden');
        contentBox.innerHTML = `
          <div class="p-6 bg-slate-950 border border-slate-800 rounded-xl space-y-3">
            <h4 class="text-sm font-bold text-white">${secDef.title}</h4>
            <p class="text-xs text-slate-400">Section details not recorded or not available for this incident.</p>
          </div>`;
      }
    }

    function renderSectionContent(sectionId, data) {
      const container = document.getElementById('mod-rep-content');
      if (!container) return;

      switch (sectionId) {
        case 'executive_summary':
          renderExecutiveSummary(container, data);
          break;
        case 'impact':
          renderImpact(container, data);
          break;
        case 'root_cause':
          renderRootCause(container, data);
          break;
        case 'evidence':
          renderEvidence(container, data);
          break;
        case 'timeline':
          renderReportTimeline(container, data);
          break;
        case 'remediation':
          renderRemediation(container, data);
          break;
        case 'verification':
          renderVerification(container, data);
          break;
        case 'prevention':
          renderPrevention(container, data);
          break;
        default:
          container.innerHTML = `<pre class="text-xs text-slate-300 mono whitespace-pre-wrap">${JSON.stringify(data, null, 2)}</pre>`;
      }
    }

    // 1. Executive Summary
    function renderExecutiveSummary(c, d) {
      const incId = d.incident_id || 'Not available';
      const sev = d.severity || 'SEV-1';
      const status = (d.status || 'RESOLVED').toUpperCase();
      const rule = d.trigger_rule || 'Not available';
      const detTime = d.detected_at || 'Not available';
      const resTime = d.resolved_at || 'Not resolved';
      const services = (d.affected_services || []).map(s => `<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-200 mono text-xs font-medium border border-slate-700">${s}</span>`).join(' ') || '<span class="text-slate-400 text-xs">Not recorded</span>';

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-clipboard-check text-indigo-400"></i> Executive Summary
            </h3>
            <p class="text-xs text-slate-400 mt-1">High-level overview, severity classification, and key duration metrics.</p>
          </div>

          <!-- Metadata KPI Grid -->
          <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">INCIDENT ID</span>
              <span class="text-xs font-bold text-indigo-400 mono">${incId}</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">SEVERITY / STATUS</span>
              <div class="flex items-center gap-1.5 mt-0.5">
                <span class="text-[10px] font-bold px-1.5 py-0.2 rounded border ${sev === 'SEV-1' ? 'bg-rose-950/80 text-rose-400 border-rose-800' : 'bg-amber-950/80 text-amber-400 border-amber-800'}">${sev}</span>
                <span class="text-[10px] font-bold px-1.5 py-0.2 rounded border bg-emerald-950/80 text-emerald-400 border-emerald-800">${status}</span>
              </div>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">TRIGGER RULE</span>
              <span class="text-xs font-semibold text-slate-200 truncate block mono">${rule}</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">DETECTION TIME</span>
              <span class="text-xs text-slate-300 mono truncate block">${detTime}</span>
            </div>
          </div>

          <!-- Time Metrics Grid -->
          <div class="grid grid-cols-2 md:grid-cols-4 gap-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800">
            <div>
              <span class="text-[10px] text-slate-500 block">TIME TO DETECT (TTD)</span>
              <span class="text-sm font-bold text-white mono">${d.ttd_seconds !== undefined ? `${d.ttd_seconds}s` : 'Not available'}</span>
            </div>
            <div>
              <span class="text-[10px] text-slate-500 block">TIME TO DIAGNOSE (TTDiag)</span>
              <span class="text-sm font-bold text-indigo-400 mono">${d.ttdiag_seconds !== undefined ? `${d.ttdiag_seconds}s` : 'Not available'}</span>
            </div>
            <div>
              <span class="text-[10px] text-slate-500 block">TIME TO RESOLVE (TTR)</span>
              <span class="text-sm font-bold text-emerald-400 mono">${d.ttr_seconds !== undefined ? `${d.ttr_seconds}s` : 'Not available'}</span>
            </div>
            <div>
              <span class="text-[10px] text-slate-500 block">SPEEDUP VS MANUAL</span>
              <span class="text-sm font-bold text-emerald-400 mono">${d.speedup_factor ? `${d.speedup_factor}x faster` : 'Not available'}</span>
              <span class="text-[9px] text-slate-500 block">Baseline: ${d.manual_baseline_seconds || 300}s</span>
            </div>
          </div>

          <!-- Affected Services -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
            <span class="text-xs font-semibold text-slate-300 block">Affected Microservices</span>
            <div class="flex flex-wrap gap-2">${services}</div>
          </div>

          <!-- Summary Narrative -->
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800 space-y-2">
            <span class="text-xs font-semibold text-slate-300 block">Executive Summary & Narrative</span>
            <div class="text-xs text-slate-300 leading-relaxed space-y-2">
              ${linkifyEvidenceText(d.summary_text || 'No summary text available.')}
            </div>
          </div>
        </div>
      `;
    }

    // 2. Incident Impact
    function renderImpact(c, d) {
      const errRows = (d.error_rate_breaches || []).map(b => `
        <tr class="border-b border-slate-800/60">
          <td class="py-2 px-3 mono text-white font-medium">${b.service || 'unknown'}</td>
          <td class="py-2 px-3 mono text-rose-400 font-bold">${b.rate !== undefined ? `${(b.rate * 100).toFixed(1)}%` : 'Breached'}</td>
          <td class="py-2 px-3 mono text-slate-400">${b.threshold || '5%'}</td>
          <td class="py-2 px-3">${(b.evidence_ids || []).map(e => formatEvidencePill(e)).join(' ') || '-'}</td>
        </tr>
      `).join('');

      const latRows = (d.latency_breaches || []).map(b => `
        <tr class="border-b border-slate-800/60">
          <td class="py-2 px-3 mono text-white font-medium">${b.service || 'unknown'}</td>
          <td class="py-2 px-3 mono text-amber-400 font-bold">${b.latency_ms !== undefined ? `${b.latency_ms}ms` : 'Breached'}</td>
          <td class="py-2 px-3 mono text-slate-400">${b.threshold || '2000ms'}</td>
          <td class="py-2 px-3">${(b.evidence_ids || []).map(e => formatEvidencePill(e)).join(' ') || '-'}</td>
        </tr>
      `).join('');

      const hcRows = (d.health_check_failures || []).map(b => `
        <tr class="border-b border-slate-800/60">
          <td class="py-2 px-3 mono text-white font-medium">${b.service || 'unknown'}</td>
          <td class="py-2 px-3"><span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-950 text-rose-400 border border-rose-800">FAILED</span></td>
          <td class="py-2 px-3 text-slate-300">${b.error || 'Health check timeout/error'}</td>
          <td class="py-2 px-3">${(b.evidence_ids || []).map(e => formatEvidencePill(e)).join(' ') || '-'}</td>
        </tr>
      `).join('');

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-chart-pie text-indigo-400"></i> Incident Impact Analysis
            </h3>
            <p class="text-xs text-slate-400 mt-1">SLO violations, error rate spikes, latency breaches, and health degradation.</p>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">INCIDENT DURATION</span>
              <span class="text-sm font-bold text-white mono">${d.incident_duration_seconds !== undefined ? `${d.incident_duration_seconds}s` : 'Not available'}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">SLO VIOLATIONS RECORDED</span>
              <span class="text-sm font-bold text-rose-400 mono">${d.slo_violations_count || 0}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">REQUEST / USER IMPACT</span>
              <span class="text-xs font-medium text-slate-300 block truncate">${d.request_impact || 'Degraded service availability'}</span>
            </div>
          </div>

          <!-- Error Rate Breaches -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <span class="text-xs font-semibold text-slate-200 block">Error Rate Breaches</span>
            ${errRows ? `
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead class="text-slate-500 border-b border-slate-800 text-[10px] uppercase">
                    <tr><th class="py-1 px-3">Service</th><th class="py-1 px-3">Observed Rate</th><th class="py-1 px-3">Threshold</th><th class="py-1 px-3">Evidence</th></tr>
                  </thead>
                  <tbody>${errRows}</tbody>
                </table>
              </div>
            ` : '<p class="text-xs text-slate-500">No error rate breaches recorded.</p>'}
          </div>

          <!-- Latency Breaches -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <span class="text-xs font-semibold text-slate-200 block">Latency Breaches (p95)</span>
            ${latRows ? `
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead class="text-slate-500 border-b border-slate-800 text-[10px] uppercase">
                    <tr><th class="py-1 px-3">Service</th><th class="py-1 px-3">Observed p95</th><th class="py-1 px-3">Threshold</th><th class="py-1 px-3">Evidence</th></tr>
                  </thead>
                  <tbody>${latRows}</tbody>
                </table>
              </div>
            ` : '<p class="text-xs text-slate-500">No latency breaches recorded.</p>'}
          </div>

          <!-- Health Check Failures -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <span class="text-xs font-semibold text-slate-200 block">Health Check Failures</span>
            ${hcRows ? `
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead class="text-slate-500 border-b border-slate-800 text-[10px] uppercase">
                    <tr><th class="py-1 px-3">Service</th><th class="py-1 px-3">Status</th><th class="py-1 px-3">Error</th><th class="py-1 px-3">Evidence</th></tr>
                  </thead>
                  <tbody>${hcRows}</tbody>
                </table>
              </div>
            ` : '<p class="text-xs text-slate-500">No health check failures recorded.</p>'}
          </div>

          ${d.details_text ? `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <span class="text-xs font-semibold text-slate-300 block">Impact Details</span>
              <p class="text-xs text-slate-300 leading-relaxed">${linkifyEvidenceText(d.details_text)}</p>
            </div>
          ` : ''}
        </div>
      `;
    }

    // 3. Root Cause Analysis
    function renderRootCause(c, d) {
      const rootSvc = d.root_cause_service || 'unknown';
      const conf = d.confidence !== undefined ? (typeof d.confidence === 'number' ? `${(d.confidence * 100).toFixed(0)}%` : d.confidence) : 'Not recorded';
      const evPills = (d.supporting_evidence || []).map(e => formatEvidencePill(e)).join(' ') || '<span class="text-slate-500 text-xs">None cited</span>';

      const causalChainHtml = (d.causal_chain || []).map((step, idx) => `
        <div class="flex items-center gap-3">
          <div class="w-6 h-6 rounded-full bg-indigo-950 text-indigo-400 border border-indigo-800 flex items-center justify-center text-xs font-bold shrink-0">
            ${idx + 1}
          </div>
          <div class="bg-slate-900/80 p-3 rounded-lg border border-slate-800/80 flex-1 text-xs text-slate-200">
            ${linkifyEvidenceText(step)}
          </div>
        </div>
      `).join('<div class="pl-3 py-1"><i class="fa-solid fa-arrow-down text-slate-600 text-xs"></i></div>') || '<p class="text-xs text-slate-500">No causal chain recorded.</p>';

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-magnifying-glass-chart text-indigo-400"></i> Root Cause Analysis
            </h3>
            <p class="text-xs text-slate-400 mt-1">Identified root cause microservice, causal chain propagation, and cited evidence.</p>
          </div>

          <!-- Root Cause Banner -->
          <div class="bg-gradient-to-r from-indigo-950/40 to-slate-950 border border-indigo-800/40 p-4 rounded-xl flex items-center justify-between">
            <div>
              <span class="text-[10px] text-indigo-400 font-bold uppercase tracking-wider block">IDENTIFIED ROOT CAUSE SERVICE</span>
              <span class="text-base font-bold text-white mono">${rootSvc}</span>
            </div>
            <div class="text-right">
              <span class="text-[10px] text-slate-400 block">CONFIDENCE LEVEL</span>
              <span class="text-sm font-bold text-emerald-400 mono">${conf}</span>
            </div>
          </div>

          <!-- Causal Chain -->
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800 space-y-3">
            <span class="text-xs font-semibold text-slate-200 block">Causal Failure Chain</span>
            <div class="space-y-1">${causalChainHtml}</div>
          </div>

          <!-- Supporting Evidence References -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
            <span class="text-xs font-semibold text-slate-300 block">Supporting Evidence Citations</span>
            <div class="flex flex-wrap gap-1.5">${evPills}</div>
          </div>

          <!-- Analysis Narrative -->
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800 space-y-2">
            <span class="text-xs font-semibold text-slate-300 block">Detailed Diagnostic Explanation</span>
            <div class="text-xs text-slate-300 leading-relaxed whitespace-pre-wrap">${linkifyEvidenceText(d.analysis_text || d.root_cause || 'No explanation available.')}</div>
          </div>
        </div>
      `;
    }

    // 4. Evidence & Observability
    function renderEvidence(c, d) {
      const records = d.evidence_records || [];
      const recRows = records.map(r => {
        const typeColor = r.type === 'log' ? 'bg-sky-950 text-sky-400 border-sky-800' :
                          (r.type === 'metric' ? 'bg-purple-950 text-purple-400 border-purple-800' : 'bg-amber-950 text-amber-400 border-amber-800');
        const desc = r.msg || r.description || r.details || JSON.stringify(r.raw || '');
        return `
          <tr class="border-b border-slate-800/60 hover:bg-slate-900/50 transition">
            <td class="py-2.5 px-3 whitespace-nowrap">${formatEvidencePill(r.id)}</td>
            <td class="py-2.5 px-3"><span class="px-1.5 py-0.5 rounded text-[10px] font-bold border ${typeColor}">${(r.type || 'EVIDENCE').toUpperCase()}</span></td>
            <td class="py-2.5 px-3 mono text-slate-300 font-medium">${r.service || '-'}</td>
            <td class="py-2.5 px-3 text-slate-300 max-w-md truncate mono text-[11px]" title="${desc}">${desc}</td>
            <td class="py-2.5 px-3 text-right">
              <button onclick="viewEvidence('${r.id}')" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 text-[10px] font-semibold rounded border border-slate-700 transition">
                Inspect
              </button>
            </td>
          </tr>
        `;
      }).join('');

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-database text-indigo-400"></i> Evidence & Observability Citations
            </h3>
            <p class="text-xs text-slate-400 mt-1">Audit changes (CHG-x), structured logs (LOG-x), and metric breaches (MET-x) citing actual ground truth.</p>
          </div>

          <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">TOTAL EVIDENCE CITED</span>
              <span class="text-sm font-bold text-indigo-400 mono">${d.total_evidence_count || records.length}</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">LOG CITATIONS (LOG-x)</span>
              <span class="text-sm font-bold text-sky-400 mono">${(d.log_evidence_ids || []).length}</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">METRIC SAMPLES (MET-x)</span>
              <span class="text-sm font-bold text-purple-400 mono">${(d.metric_evidence_ids || []).length}</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">CHANGE AUDITS (CHG-x)</span>
              <span class="text-sm font-bold text-amber-400 mono">${(d.change_record_ids || []).length}</span>
            </div>
          </div>

          <!-- Evidence Records Table -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-slate-200">Cited Evidence Index</span>
              <span class="text-[11px] text-slate-500">Click any row or button to view exact telemetry payload</span>
            </div>

            ${recRows ? `
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead class="text-slate-500 border-b border-slate-800 text-[10px] uppercase">
                    <tr>
                      <th class="py-1 px-3">Evidence ID</th>
                      <th class="py-1 px-3">Type</th>
                      <th class="py-1 px-3">Service</th>
                      <th class="py-1 px-3">Details / Observed Telemetry</th>
                      <th class="py-1 px-3 text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody>${recRows}</tbody>
                </table>
              </div>
            ` : '<p class="text-xs text-slate-500">No explicit evidence records indexed.</p>'}
          </div>

          ${d.summary_text ? `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <span class="text-xs font-semibold text-slate-300 block">Observability Assessment</span>
              <p class="text-xs text-slate-300 leading-relaxed">${linkifyEvidenceText(d.summary_text)}</p>
            </div>
          ` : ''}
        </div>
      `;
    }

    // 5. Incident Timeline (Modular Report Section)
    function renderReportTimeline(c, d) {
      const events = d.events || [];
      const evItems = events.map(ev => {
        const typeColor = ev.event === 'anomaly' ? 'bg-rose-950 text-rose-400 border-rose-800' :
                          (ev.event === 'breach' ? 'bg-amber-950 text-amber-400 border-amber-800' :
                          (ev.event === 'change' ? 'bg-sky-950 text-sky-400 border-sky-800' :
                          (ev.event === 'fix' ? 'bg-indigo-950 text-indigo-400 border-indigo-800' : 'bg-emerald-950 text-emerald-400 border-emerald-800')));
        const offset = ev.offset_seconds !== undefined ? `+${ev.offset_seconds}s` : '';
        const evPills = (ev.evidence_ids || []).map(e => formatEvidencePill(e)).join(' ');

        return `
          <div class="relative pl-6 pb-6 border-l border-slate-800 last:border-0 last:pb-0">
            <div class="absolute -left-1.5 top-0 w-3 h-3 rounded-full bg-slate-800 border-2 border-indigo-500"></div>
            <div class="flex items-center justify-between gap-2 flex-wrap">
              <div class="flex items-center gap-2">
                <span class="text-xs font-bold text-slate-300 mono">${ev.ts || 'Unknown'}</span>
                ${offset ? `<span class="text-[10px] text-slate-500 mono font-semibold">(${offset})</span>` : ''}
                <span class="text-[10px] font-bold px-1.5 py-0.2 rounded border ${typeColor}">${(ev.event || 'EVENT').toUpperCase()}</span>
                <span class="text-xs text-indigo-400 mono font-semibold">${ev.service || ''}</span>
              </div>
              <div class="flex items-center gap-1">${evPills}</div>
            </div>
            <p class="text-xs text-slate-300 mt-1 leading-relaxed">${linkifyEvidenceText(ev.description || '')}</p>
          </div>
        `;
      }).join('');

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-clock-rotate-left text-indigo-400"></i> Chronological Incident Timeline
            </h3>
            <p class="text-xs text-slate-400 mt-1">Audit-backed event progression from earliest anomaly (T0) to verification and resolution.</p>
          </div>

          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 flex items-center justify-between">
            <div>
              <span class="text-[10px] text-slate-500 font-medium block">EARLIEST ANOMALY (T0)</span>
              <span class="text-xs font-bold text-rose-400 mono">${d.t0 || 'Not available'}</span>
            </div>
            <div class="text-right">
              <span class="text-[10px] text-slate-500 block">TOTAL RECORDED EVENTS</span>
              <span class="text-xs font-bold text-indigo-400 mono">${d.total_events || events.length}</span>
            </div>
          </div>

          <!-- Chronological event list -->
          <div class="bg-slate-950 p-5 rounded-xl border border-slate-800">
            ${evItems ? `<div class="space-y-1">${evItems}</div>` : '<p class="text-xs text-slate-500">No timeline events recorded.</p>'}
          </div>
        </div>
      `;
    }

    // 6. Remediation Actions
    function renderRemediation(c, d) {
      const appr = d.approval_info || {};
      const tier = d.permission_tier || 'auto_fix';
      const tierColor = tier === 'auto_fix' ? 'bg-emerald-950 text-emerald-400 border-emerald-800' :
                        (tier === 'ask_first' ? 'bg-amber-950 text-amber-400 border-amber-800' : 'bg-rose-950 text-rose-400 border-rose-800');

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-wrench text-indigo-400"></i> Remediation Actions & Safeguards
            </h3>
            <p class="text-xs text-slate-400 mt-1">Autonomous action execution, safety tier authorization, and human approval trail.</p>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">TARGET SERVICE</span>
              <span class="text-xs font-bold text-white mono">${d.target_service || 'unknown'}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">PERMISSION TIER</span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded border ${tierColor} inline-block mt-0.5">${tier.toUpperCase()}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">ACTION RESULT</span>
              <span class="text-xs font-semibold text-emerald-400 block">${d.action_result || 'Success'}</span>
            </div>
          </div>

          <!-- Executed Action Card -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <span class="text-xs font-semibold text-slate-200 block">Proposed vs Executed Fix</span>
            <div class="space-y-2 text-xs">
              <div class="bg-slate-900/60 p-3 rounded-lg border border-slate-800">
                <span class="text-[10px] text-slate-500 block">Proposed Action:</span>
                <span class="text-slate-300 font-medium">${d.proposed_action || 'Not available'}</span>
              </div>
              <div class="bg-slate-900/60 p-3 rounded-lg border border-slate-800">
                <span class="text-[10px] text-slate-500 block">Action Executed:</span>
                <span class="text-emerald-400 font-bold mono">${d.action_executed || 'Not recorded'}</span>
              </div>
            </div>
          </div>

          <!-- Human Approval Block -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-slate-200">Human Approval / Gate Information</span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded border ${appr.required ? 'bg-amber-950 text-amber-400 border-amber-800' : 'bg-slate-800 text-slate-400 border-slate-700'}">
                ${appr.required ? 'REQUIRED' : 'AUTONOMOUS / NOT REQUIRED'}
              </span>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
              <div><span class="text-slate-500 text-[10px] block">Status:</span><span class="text-slate-200 mono">${appr.status || 'auto_approved'}</span></div>
              <div><span class="text-slate-500 text-[10px] block">Decided By:</span><span class="text-slate-200 mono">${appr.decided_by || 'agent'}</span></div>
              <div><span class="text-slate-500 text-[10px] block">Decision Time:</span><span class="text-slate-300 mono truncate block">${appr.decision_time || 'Immediate'}</span></div>
              <div><span class="text-slate-500 text-[10px] block">Reason:</span><span class="text-slate-300 truncate block">${appr.reason || 'None'}</span></div>
            </div>
          </div>

          ${d.failure_or_escalation_details && d.failure_or_escalation_details !== 'None' ? `
            <div class="bg-rose-950/30 p-4 rounded-xl border border-rose-800/60 space-y-2">
              <span class="text-xs font-semibold text-rose-300 block">Failure or Escalation Details</span>
              <p class="text-xs text-rose-300">${d.failure_or_escalation_details}</p>
            </div>
          ` : ''}

          ${d.summary_text ? `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <span class="text-xs font-semibold text-slate-300 block">Remediation Narrative</span>
              <p class="text-xs text-slate-300 leading-relaxed">${linkifyEvidenceText(d.summary_text)}</p>
            </div>
          ` : ''}
        </div>
      `;
    }

    // 7. Recovery Verification
    function renderVerification(c, d) {
      const recovered = d.is_verified_recovered !== false;
      const evPills = (d.supporting_evidence || []).map(e => formatEvidencePill(e)).join(' ') || '<span class="text-slate-500 text-xs">None cited</span>';

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-shield-halved text-indigo-400"></i> Recovery Verification
            </h3>
            <p class="text-xs text-slate-400 mt-1">Telemetry-backed proof confirming health status, error rates, and latency normalization.</p>
          </div>

          <!-- Status Banner -->
          <div class="p-4 rounded-xl border flex items-center justify-between ${recovered ? 'bg-emerald-950/40 border-emerald-800/80' : 'bg-rose-950/40 border-rose-800/80'}">
            <div class="flex items-center space-x-3">
              <i class="fa-solid ${recovered ? 'fa-circle-check text-emerald-400' : 'fa-circle-xmark text-rose-400'} text-2xl"></i>
              <div>
                <h4 class="text-xs font-bold ${recovered ? 'text-emerald-300' : 'text-rose-300'}">
                  ${recovered ? 'RECOVERY SUCCESSFULLY VERIFIED' : 'RECOVERY VERIFICATION PENDING OR FAILED'}
                </h4>
                <p class="text-[11px] text-slate-400">Verification timestamp: ${d.verification_timestamp || 'Not recorded'}</p>
              </div>
            </div>
            <div class="text-right">
              <span class="text-[10px] text-slate-400 block">VERIFICATION WINDOW</span>
              <span class="text-xs font-bold text-white mono">${d.verification_duration_seconds !== undefined ? `${d.verification_duration_seconds}s` : 'Not available'}</span>
            </div>
          </div>

          <!-- Metric Status Cards -->
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">HEALTH CHECK RESULT</span>
              <span class="text-xs font-bold text-emerald-400 mono">${d.health_check_result || '200 OK'}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">ERROR RATE STATUS</span>
              <span class="text-xs font-bold text-emerald-400 mono">${d.error_rate_status || '0.0% (<5% SLO)'}</span>
            </div>
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800">
              <span class="text-[10px] text-slate-500 font-medium block">p95 LATENCY STATUS</span>
              <span class="text-xs font-bold text-emerald-400 mono">${d.p95_latency_status || 'Normal (<2000ms)'}</span>
            </div>
          </div>

          <!-- Evidence pills -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
            <span class="text-xs font-semibold text-slate-300 block">Supporting Verification Citations</span>
            <div class="flex flex-wrap gap-1.5">${evPills}</div>
          </div>

          ${d.summary_text ? `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <span class="text-xs font-semibold text-slate-300 block">Verification Assessment</span>
              <p class="text-xs text-slate-300 leading-relaxed">${linkifyEvidenceText(d.summary_text)}</p>
            </div>
          ` : ''}
        </div>
      `;
    }

    // 8. Prevention & Recommendations
    function renderPrevention(c, d) {
      const renderList = (items, emptyMsg) => {
        if (!items || items.length === 0) return `<p class="text-xs text-slate-500">${emptyMsg}</p>`;
        return `<ul class="space-y-1.5">${items.map(i => `
          <li class="flex items-start gap-2 text-xs text-slate-300">
            <i class="fa-solid fa-check text-indigo-400 text-[10px] mt-1 shrink-0"></i>
            <span>${linkifyEvidenceText(i)}</span>
          </li>
        `).join('')}</ul>`;
      };

      c.innerHTML = `
        <div class="space-y-6">
          <div class="border-b border-slate-800 pb-4">
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <i class="fa-solid fa-lightbulb text-indigo-400"></i> Prevention & Architectural Recommendations
            </h3>
            <p class="text-xs text-slate-400 mt-1">Actionable safeguards, monitoring adjustments, and preventive steps to eliminate recurrence.</p>
          </div>

          <!-- Preventative Actions -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2.5">
            <span class="text-xs font-semibold text-slate-200 block">Preventive Actions</span>
            ${renderList(d.preventive_actions, 'No preventative actions specified.')}
          </div>

          <!-- Configuration Recommendations -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2.5">
            <span class="text-xs font-semibold text-slate-200 block">Configuration & Resilience Recommendations</span>
            ${renderList(d.configuration_recommendations, 'No configuration changes specified.')}
          </div>

          <!-- Monitoring Improvements -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2.5">
            <span class="text-xs font-semibold text-slate-200 block">Monitoring & Alerting Improvements</span>
            ${renderList(d.monitoring_improvements, 'No monitoring enhancements specified.')}
          </div>

          <!-- Follow-up Actions -->
          <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2.5">
            <span class="text-xs font-semibold text-slate-200 block">Follow-up Action Items</span>
            ${renderList(d.follow_up_actions, 'No follow-up action items specified.')}
          </div>

          ${d.summary_text ? `
            <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
              <span class="text-xs font-semibold text-slate-300 block">Summary & Outlook</span>
              <p class="text-xs text-slate-300 leading-relaxed">${linkifyEvidenceText(d.summary_text)}</p>
            </div>
          ` : ''}
        </div>
      `;
    }

    function toggleRawMarkdownView() {
      const content = document.getElementById('mod-rep-content');
      const raw = document.getElementById('mod-rep-raw-markdown');
      const btn = document.getElementById('mod-rep-raw-btn');
      const isRaw = !raw.classList.contains('hidden');

      if (isRaw) {
        raw.classList.add('hidden');
        content.classList.remove('hidden');
        btn.innerHTML = '<i class="fa-solid fa-file-code"></i> Full Markdown';
      } else {
        content.classList.add('hidden');
        raw.classList.remove('hidden');
        btn.innerHTML = '<i class="fa-solid fa-table-columns"></i> Modular View';
      }
    }

    function copyRawMarkdown() {
      const text = document.getElementById('pm-modal-body').innerText;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => {
          alert("Full Markdown postmortem copied to clipboard!");
        }).catch(() => {
          prompt("Copy markdown below:", text);
        });
      } else {
        prompt("Copy markdown below:", text);
      }
    }

    async function downloadMarkdownReport() {
      const incId = currentReportIncId;
      if (!incId) return;

      const btn = document.getElementById('mod-rep-download-btn');
      const originalHtml = btn ? btn.innerHTML : '';
      if (btn) {
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Exporting...';
        btn.disabled = true;
      }

      try {
        const res = await fetch(`/api/reports/${incId}/export`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const markdown = data.markdown || '';
        const filename = data.filename || `${incId}_Postmortem_${new Date().toISOString().slice(0, 10)}.md`;

        // Create genuine Markdown blob with UTF-8 encoding
        const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);

        // Revoke temporary object URL to free memory
        setTimeout(() => {
          URL.revokeObjectURL(url);
        }, 1500);

        if (btn) {
          btn.innerHTML = '<i class="fa-solid fa-check text-emerald-400"></i> Downloaded!';
          setTimeout(() => {
            btn.innerHTML = originalHtml;
            btn.disabled = false;
          }, 2000);
        }
      } catch (err) {
        console.error("Export error:", err);
        alert("Failed to download markdown report: " + err.message);
        if (btn) {
          btn.innerHTML = originalHtml;
          btn.disabled = false;
        }
      }
    }

    async function downloadCategoryMarkdown(event, sectionId) {
      if (event && event.stopPropagation) event.stopPropagation();
      const incId = currentReportIncId;
      if (!incId) return;

      const navBtn = document.getElementById(`nav-dl-${sectionId}`);
      const origNavHtml = navBtn ? navBtn.innerHTML : '';
      if (navBtn) {
        navBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin text-indigo-400"></i>';
      }

      try {
        const res = await fetch(`/api/reports/${incId}/sections/${sectionId}/export`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const markdown = data.markdown || '';
        const filename = data.filename || `${incId}_${sectionId}.md`;

        const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);

        setTimeout(() => {
          URL.revokeObjectURL(url);
        }, 1500);

        if (navBtn) {
          navBtn.innerHTML = '<i class="fa-solid fa-check text-emerald-400"></i>';
          setTimeout(() => {
            navBtn.innerHTML = origNavHtml;
          }, 2000);
        }
      } catch (err) {
        console.error("Category download error:", err);
        alert(`Failed to download category ${sectionId}: ` + err.message);
        if (navBtn) navBtn.innerHTML = origNavHtml;
      }
    }

    async function downloadAllZip() {
      const incId = currentReportIncId;
      if (!incId) return;

      const btn = document.getElementById('mod-rep-zip-btn');
      const originalHtml = btn ? btn.innerHTML : '';
      if (btn) {
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Bundling ZIP...';
        btn.disabled = true;
      }

      try {
        const res = await fetch(`/api/reports/${incId}/zip`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();

        const contentDisp = res.headers.get('Content-Disposition') || '';
        let filename = `${incId}_Postmortem_All_Sections.zip`;
        const match = contentDisp.match(/filename="?([^"]+)"?/);
        if (match && match[1]) {
          filename = match[1];
        }

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);

        setTimeout(() => {
          URL.revokeObjectURL(url);
        }, 1500);

        if (btn) {
          btn.innerHTML = '<i class="fa-solid fa-check text-purple-400"></i> Downloaded ZIP!';
          setTimeout(() => {
            btn.innerHTML = originalHtml;
            btn.disabled = false;
          }, 2000);
        }
      } catch (err) {
        console.error("ZIP download error:", err);
        alert("Failed to download ZIP archive: " + err.message);
        if (btn) {
          btn.innerHTML = originalHtml;
          btn.disabled = false;
        }
      }
    }

    function closePostmortemModal() {
      document.getElementById('postmortem-modal').classList.add('hidden');
    }

    // Backward compatibility alias for any existing callers
    window.viewPostmortem = openModularReport;

    async function viewEvidence(eid) {
      if (!eid) return;
      document.getElementById('modal-ev-id').innerText = eid;
      document.getElementById('modal-ev-content').innerText = 'Loading telemetry...';
      document.getElementById('evidence-modal').classList.remove('hidden');

      try {
        const res = await fetch(`/api/evidence/${encodeURIComponent(eid)}`);
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          document.getElementById('modal-ev-content').innerText = errData.detail || `HTTP ${res.status}: Evidence ${eid} not found`;
          return;
        }
        const data = await res.json();
        document.getElementById('modal-ev-content').innerText = JSON.stringify(data, null, 2);
      } catch (err) {
        document.getElementById('modal-ev-content').innerText = 'Failed to load evidence: ' + err.message;
      }
    }

    function closeEvidenceModal() {
      document.getElementById('evidence-modal').classList.add('hidden');
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

    async function restartService(serviceName) {
      try {
        await fetch(`/api/services/${serviceName}/restart`, {method: 'POST'});
        fetchStatus();
      } catch (err) {
        alert("Failed to restart service: " + err);
      }
    }

    async function restartAllServices() {
      try {
        await fetch('/api/services/restart-all', {method: 'POST'});
        fetchStatus();
      } catch (err) {
        alert("Failed to restart services: " + err);
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

    // Auto-refresh loop (2-second interval for real-time reactivity with zero UI lag)
    fetchStatus();
    fetchApprovals();
    fetchReports();
    setInterval(fetchStatus, 2000);
    setInterval(fetchApprovals, 2000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, summary="Interactive Dashboard UI")
async def dashboard_index() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


def main() -> int:
    import os
    import uvicorn
    env_port = os.getenv("DASHBOARD_PORT")
    port = int(env_port) if env_port else None
    if port is None:
        import socket
        for p in (8080, 9000, 8090, 8088):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", p))
                    port = p
                    break
                except OSError:
                    continue
        if port is None:
            port = 8080
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    print(f"Starting AI-SRE Dashboard on http://{host}:{port}")
    uvicorn.run("sre.dashboard.app:app", host=host, port=port, log_level="info", reload=True, reload_dirs=["sre", "config"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
