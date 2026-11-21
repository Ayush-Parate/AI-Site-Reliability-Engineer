"""Tests for Phase 5: Agent Core (sre/agent.py) and tools (sre/tools.py).

Uses a mocked LLM — no network required.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sre.agent import (
    FINAL_ANSWER_KEYS,
    MAX_STEPS,
    SYSTEM_PROMPT,
    Agent,
    AgentError,
    Diagnosis,
)
from sre.detector import Incident, IncidentDetector
from sre.llm import LLM
from sre.observability import Observability, format_iso
from sre.supervisor import Supervisor
from sre.tools import ToolRegistry


FINAL_JSON = {
    "root_cause_service": "payments",
    "root_cause": "payments process crashed (OOM) at 02:14:07",
    "evidence": ["LOG-1042", "MET-77", "CHG-9"],
    "causal_chain": ["payments down", "orders 5xx", "gateway latency"],
    "confidence": 0.9,
    "proposed_fix": {"action": "restart_service", "args": {"service": "payments"}},
    "mitigation_vs_cure": "mitigation",
}


@pytest.fixture
def agent_env(tmp_path: Path, monkeypatch):
    """Isolated observability + detector + tools for agent tests."""
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("BACKUP_PROVIDER", "ollama")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    db_file = tmp_path / "obs.db"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    changes_file = tmp_path / "changes.jsonl"
    deps_file = tmp_path / "dependencies.yaml"
    slo_file = tmp_path / "slo.yaml"

    deps_file.write_text(
        """
services:
  gateway:
    port: 8000
    calls: [orders]
  orders:
    port: 8001
    calls: [payments, inventory]
  payments:
    port: 8002
    calls: []
  inventory:
    port: 8003
    calls: []
""",
        encoding="utf-8",
    )
    slo_file.write_text(
        """
health_check_failures: 3
error_rate_threshold: 0.05
error_rate_window_seconds: 30
latency_p95_slo_ms: 2000
recover_after_seconds: 1.0
""",
        encoding="utf-8",
    )

    obs = Observability(
        db_path=db_file,
        logs_dir=logs_dir,
        changes_path=changes_file,
        deps_path=deps_file,
        slo_path=slo_file,
        repo_root=tmp_path,
    )
    # Seed a log line with evidence ID
    (logs_dir / "payments.log").write_text(
        json.dumps(
            {
                "ts": format_iso(time.time() - 10),
                "level": "ERROR",
                "service": "payments",
                "trace_id": "abc",
                "msg": "process crashed OOM",
                "latency_ms": 0,
                "status": 500,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Seed a change
    changes_file.write_text(
        json.dumps(
            {
                "id": "CHG-1",
                "ts": format_iso(time.time() - 30),
                "action": "chaos_crash",
                "service": "payments",
                "author": "chaos",
                "details": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sup = Supervisor(repo_root=tmp_path, changes_log=changes_file)
    detector = IncidentDetector(
        observability=obs,
        supervisor=sup,
        slo_path=slo_file,
        deps_path=deps_file,
        db_path=db_file,
    )
    tools = ToolRegistry(obs)
    llm = LLM()
    agent = Agent(llm=llm, tools=tools, detector=detector, supervisor=sup, max_steps=12)

    t0_unix = time.time() - 20
    incident = Incident(
        id="INC-1",
        status="open",
        trigger_rule="error_rate_threshold",
        t0=format_iso(t0_unix),
        t0_unix=t0_unix,
        created_at=format_iso(time.time()),
        created_at_unix=time.time(),
        ttd_seconds=20.0,
        affected_services=["orders", "gateway"],
        evidence=["LOG-1"],
        breaches=[{"rule": "error_rate_threshold", "service": "orders"}],
    )
    detector.save_incident(incident)

    yield {
        "agent": agent,
        "tools": tools,
        "obs": obs,
        "detector": detector,
        "supervisor": sup,
        "incident": incident,
        "llm": llm,
        "tmp_path": tmp_path,
    }


def _chat_response(text: str = "", tool_calls: list | None = None, provider: str = "openai"):
    return {
        "text": text,
        "tool_calls": tool_calls or [],
        "provider": provider,
        "model": "mock-model",
        "message": None,
    }


def test_system_prompt_enforces_evidence_and_deps():
    assert "earliest anomaly" in SYSTEM_PROMPT.lower() or "earliest anomaly" in SYSTEM_PROMPT
    assert "dependency" in SYSTEM_PROMPT.lower()
    assert "get_changes" in SYSTEM_PROMPT
    assert "LOG-x" in SYSTEM_PROMPT
    assert "confidence" in SYSTEM_PROMPT.lower()
    for key in FINAL_ANSWER_KEYS:
        assert key in SYSTEM_PROMPT
    assert MAX_STEPS == 12


def test_tool_registry_schemas_and_allowlist(agent_env):
    tools: ToolRegistry = agent_env["tools"]
    schemas = tools.schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == {
        "get_logs",
        "get_metrics",
        "get_deps",
        "get_changes",
        "get_health",
        "get_evidence",
    }

    blocked = json.loads(tools.call("drop_database", {}))
    assert blocked.get("blocked") is True

    deps = json.loads(tools.call("get_deps", {"service": "orders"}))
    assert "payments" in deps.get("downstream", deps.get("calls", [])) or "payments" in str(deps)

    changes = json.loads(tools.call("get_changes", {"since": 3600}))
    assert changes["count"] >= 1
    assert "CHG-1" in changes["evidence_ids"]

    logs = json.loads(tools.call("get_logs", {"service": "payments", "level": "ERROR"}))
    assert logs["count"] >= 1
    assert logs["evidence_ids"]


def test_tool_rejects_unknown_service(agent_env):
    tools: ToolRegistry = agent_env["tools"]
    result = json.loads(tools.call("get_logs", {"service": "not-a-service"}))
    assert "error" in result


def test_parse_final_answer_from_raw_and_fenced():
    raw = Agent._parse_final_answer(json.dumps(FINAL_JSON))
    assert raw is not None
    assert raw["root_cause_service"] == "payments"
    assert raw["confidence"] == 0.9

    fenced = Agent._parse_final_answer(
        "Here is my answer:\n```json\n" + json.dumps(FINAL_JSON) + "\n```\n"
    )
    assert fenced is not None
    assert fenced["evidence"] == FINAL_JSON["evidence"]

    assert Agent._parse_final_answer("not json") is None
    assert Agent._parse_final_answer('{"root_cause": "x"}') is None  # missing keys


def test_investigate_records_t1_and_diagnosis(agent_env):
    agent: Agent = agent_env["agent"]
    incident: Incident = agent_env["incident"]
    detector: IncidentDetector = agent_env["detector"]

    def fake_chat(messages, **kwargs):
        return _chat_response(text=json.dumps(FINAL_JSON), provider=kwargs.get("provider", "openai"))

    agent.llm.chat = MagicMock(side_effect=fake_chat)

    before = time.time()
    diagnosis = agent.investigate(incident)
    after = time.time()

    assert isinstance(diagnosis, Diagnosis)
    assert diagnosis.root_cause_service == "payments"
    assert diagnosis.evidence == FINAL_JSON["evidence"]
    assert diagnosis.proposed_fix["action"] == "restart_service"
    assert diagnosis.t1_unix >= before
    assert diagnosis.t1_unix <= after + 0.5
    assert diagnosis.time_to_diagnosis_seconds == pytest.approx(
        diagnosis.t1_unix - incident.t0_unix, abs=0.05
    )
    assert diagnosis.provider == "openai"
    assert diagnosis.steps_used >= 1

    refreshed = detector.get_incident(incident.id)
    assert refreshed is not None
    assert refreshed.status == "diagnosing"
    assert refreshed.metadata["t1"] == diagnosis.t1
    assert refreshed.metadata["time_to_diagnosis_seconds"] == diagnosis.time_to_diagnosis_seconds
    assert refreshed.metadata["diagnosis"]["root_cause_service"] == "payments"


def test_tool_calling_loop_then_final_answer(agent_env):
    agent: Agent = agent_env["agent"]
    incident: Incident = agent_env["incident"]

    calls = {"n": 0}

    def fake_chat(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_response(
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "get_changes",
                        "arguments": json.dumps({"since": 300}),
                    }
                ]
            )
        if calls["n"] == 2:
            return _chat_response(
                tool_calls=[
                    {
                        "id": "call_2",
                        "name": "get_deps",
                        "arguments": json.dumps({"service": "orders"}),
                    }
                ]
            )
        return _chat_response(text=json.dumps(FINAL_JSON))

    agent.llm.chat = MagicMock(side_effect=fake_chat)
    diagnosis = agent.investigate(incident)

    assert diagnosis.steps_used == 3
    assert len(diagnosis.tool_trace) == 2
    assert diagnosis.tool_trace[0]["name"] == "get_changes"
    assert diagnosis.tool_trace[1]["name"] == "get_deps"
    assert diagnosis.root_cause_service == "payments"


def test_max_steps_forces_final_answer(agent_env):
    agent: Agent = agent_env["agent"]
    agent.max_steps = 3
    incident: Incident = agent_env["incident"]

    def always_tools(messages, **kwargs):
        if kwargs.get("tools") is None:
            # force_final path — no tools offered
            return _chat_response(text=json.dumps(FINAL_JSON))
        return _chat_response(
            tool_calls=[
                {
                    "id": "call_x",
                    "name": "get_health",
                    "arguments": "{}",
                }
            ]
        )

    agent.llm.chat = MagicMock(side_effect=always_tools)
    diagnosis = agent.investigate(incident)
    assert diagnosis.forced_final is True
    assert diagnosis.root_cause_service == "payments"
    assert diagnosis.steps_used == 3


def test_provider_failover_restarts_investigation(agent_env, monkeypatch):
    agent: Agent = agent_env["agent"]
    incident: Incident = agent_env["incident"]
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("BACKUP_PROVIDER", "ollama")
    # Rebuild order after env change
    agent.llm = LLM()

    seen_providers: list[str] = []

    def flaky_chat(messages, **kwargs):
        provider = kwargs.get("provider") or "openai"
        seen_providers.append(provider)
        if provider == "openai":
            raise RuntimeError("primary blew up mid-investigation")
        return _chat_response(text=json.dumps(FINAL_JSON), provider=provider)

    agent.llm.chat = MagicMock(side_effect=flaky_chat)
    # ollama needs no key; mark configured via is_configured already True
    diagnosis = agent.investigate(incident)
    assert diagnosis.provider == "ollama"
    assert "openai" in seen_providers
    assert "ollama" in seen_providers


def test_all_providers_fail_raises(agent_env, monkeypatch):
    agent: Agent = agent_env["agent"]
    incident: Incident = agent_env["incident"]
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("BACKUP_PROVIDER", "ollama")
    agent.llm = LLM()

    agent.llm.chat = MagicMock(side_effect=RuntimeError("boom"))
    with pytest.raises(AgentError, match="Investigation failed"):
        agent.investigate(incident)


def test_cascade_summary_mentions_affected_and_health(agent_env):
    agent: Agent = agent_env["agent"]
    incident: Incident = agent_env["incident"]
    # Avoid live HTTP health polls in unit test by stubbing
    agent.tools.obs.get_health = MagicMock(
        return_value={
            "healthy": False,
            "services": {
                "gateway": {"healthy": True},
                "orders": {"healthy": False},
                "payments": {"healthy": False},
                "inventory": {"healthy": True},
            },
        }
    )
    summary = json.loads(agent._build_incident_summary(incident))
    assert summary["incident_id"] == "INC-1"
    assert "orders" in summary["affected_services"]
    assert summary["current_health"]["healthy"] is False
    assert "get_changes" in summary["instructions"]
