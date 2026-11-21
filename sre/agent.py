"""LLM tool-calling investigation loop for AI-SRE.

Investigates open incidents with allowlisted read-only tools, fails over
providers per incident (fresh history on each provider), caps at 12 steps,
and returns a structured JSON diagnosis with T1 / time-to-diagnosis.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.detector import Incident, IncidentDetector
from sre.llm import LLM, LLMUnavailable
from sre.observability import format_iso, get_observability
from sre.supervisor import Supervisor
from sre.tools import ToolRegistry

MAX_STEPS = 12

SYSTEM_PROMPT = """You are an AI Site Reliability Engineer investigating a production incident.

Principles (AIA-02 — follow strictly):
1. Find the earliest anomaly, not the loudest symptom.
2. Walk the dependency graph from the symptom toward the source. If A is slow/erroring and A calls C, check C first.
3. Check recent changes first (deploys, restarts, chaos flags) via get_changes before deep diving.
4. Confirm every hypothesis with at least two independent pieces of evidence before concluding.
5. Cite evidence IDs (LOG-x, MET-x, CHG-x) for every claim. Never invent IDs — only cite IDs returned by tools.
6. State confidence honestly. If evidence is insufficient, say so with lower confidence.

Investigation tools (read-only): get_logs, get_metrics, get_deps, get_changes, get_health, get_evidence.

When you have enough evidence, stop calling tools and reply with ONLY a JSON object
(no markdown fences, no commentary) matching this schema:
{
  "root_cause_service": "<service name>",
  "root_cause": "<concise description of the root cause>",
  "evidence": ["LOG-x", "MET-y", "CHG-z"],
  "causal_chain": ["upstream cause", "...", "downstream symptom"],
  "confidence": 0.0,
  "proposed_fix": {"action": "<action>", "args": {"service": "<name>"}},
  "mitigation_vs_cure": "mitigation" | "cure"
}

proposed_fix.action should be one of: restart_service, clear_chaos_flag, rotate_logs, rollback, scale_up, change_config.
Prefer the upstream root-cause service, not the noisy caller.
"""

FINAL_ANSWER_KEYS = (
    "root_cause_service",
    "root_cause",
    "evidence",
    "causal_chain",
    "confidence",
    "proposed_fix",
    "mitigation_vs_cure",
)

FORCE_FINAL_PROMPT = (
    "You have reached the investigation step limit. Stop calling tools. "
    "Reply with ONLY the final JSON diagnosis object now, using the best evidence "
    "gathered so far. Cite only real evidence IDs you saw in tool results."
)


@dataclass
class Diagnosis:
    """Structured root-cause diagnosis produced by the agent."""

    root_cause_service: str
    root_cause: str
    evidence: list[str]
    causal_chain: list[str]
    confidence: float
    proposed_fix: dict[str, Any]
    mitigation_vs_cure: str
    incident_id: str
    t0: str
    t0_unix: float
    t1: str
    t1_unix: float
    time_to_diagnosis_seconds: float
    provider: str
    model: str
    steps_used: int
    forced_final: bool = False
    tool_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentError(RuntimeError):
    """Raised when investigation cannot produce a diagnosis."""


class Agent:
    """Investigates incidents via an LLM tool-calling loop with provider failover."""

    def __init__(
        self,
        llm: LLM | None = None,
        tools: ToolRegistry | None = None,
        detector: IncidentDetector | None = None,
        supervisor: Supervisor | None = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.llm = llm or LLM()
        self.tools = tools or ToolRegistry()
        self.detector = detector
        self.supervisor = supervisor or Supervisor()
        self.max_steps = max(1, int(max_steps))
        self.last_diagnosis: Diagnosis | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def investigate(self, incident: Incident | dict[str, Any] | str) -> Diagnosis:
        """Run a full investigation with per-provider failover.

        Failover restarts the entire loop on the backup so tool-call histories
        are never mixed across providers.
        """
        inc = self._resolve_incident(incident)
        if self.detector is not None:
            self.detector.update_incident_status(inc.id, "diagnosing")

        errors: list[str] = []
        for provider in self.llm.order:
            if not self.llm.is_configured(provider):
                errors.append(f"{provider}: not configured")
                continue
            try:
                diagnosis = self._investigate_with_provider(inc, provider)
                self._record_diagnosis(inc, diagnosis)
                self.last_diagnosis = diagnosis
                return diagnosis
            except Exception as exc:  # noqa: BLE001 - failover to next provider
                errors.append(f"{provider}: {exc}")

        raise AgentError(
            "Investigation failed on all providers:\n  " + "\n  ".join(errors)
        )

    def step(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str,
        force_final: bool = False,
    ) -> dict[str, Any]:
        """Execute one LLM turn (optionally forcing a final JSON answer).

        Returns a dict with keys: response, messages, tool_results, done, parsed.
        """
        working = list(messages)
        if force_final:
            working.append({"role": "user", "content": FORCE_FINAL_PROMPT})

        tools = None if force_final else self.tools.schemas()
        response = self.llm.chat(
            working,
            tools=tools,
            provider=provider,
            temperature=0.1,
        )

        assistant_msg = self._assistant_message(response)
        working.append(assistant_msg)

        tool_calls = response.get("tool_calls") or []
        tool_results: list[dict[str, Any]] = []
        parsed: dict[str, Any] | None = None

        if tool_calls and not force_final:
            for tc in tool_calls:
                result_text = self.tools.call(tc["name"], tc.get("arguments"))
                tool_results.append(
                    {
                        "id": tc.get("id"),
                        "name": tc["name"],
                        "arguments": tc.get("arguments"),
                        "result": result_text,
                    }
                )
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or tc["name"],
                        "content": result_text,
                    }
                )
            return {
                "response": response,
                "messages": working,
                "tool_results": tool_results,
                "done": False,
                "parsed": None,
            }

        # No tool calls (or forced final) — try to parse structured answer.
        parsed = self._parse_final_answer(response.get("text") or "")
        return {
            "response": response,
            "messages": working,
            "tool_results": tool_results,
            "done": parsed is not None,
            "parsed": parsed,
        }

    # ------------------------------------------------------------------
    # Investigation internals
    # ------------------------------------------------------------------

    def _investigate_with_provider(self, incident: Incident, provider: str) -> Diagnosis:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._build_incident_summary(incident)},
        ]
        tool_trace: list[dict[str, Any]] = []
        model_name = ""
        forced_final = False
        parsed: dict[str, Any] | None = None
        steps_used = 0

        for step_num in range(1, self.max_steps + 1):
            force = step_num == self.max_steps
            if force:
                forced_final = True

            turn = self.step(messages, provider=provider, force_final=force)
            messages = turn["messages"]
            model_name = turn["response"].get("model") or model_name
            steps_used = step_num

            for tr in turn["tool_results"]:
                tool_trace.append(
                    {
                        "step": step_num,
                        "name": tr["name"],
                        "arguments": tr["arguments"],
                        "result_preview": str(tr["result"])[:500],
                    }
                )

            if turn["done"] and turn["parsed"]:
                parsed = turn["parsed"]
                break

            # Model returned text without parseable JSON and without tools —
            # nudge once before continuing (unless this was already the last step).
            if not turn["tool_results"] and not turn["parsed"] and not force:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue investigating with tools, or if ready, reply with "
                            "ONLY the final JSON diagnosis object."
                        ),
                    }
                )

        if parsed is None:
            # Last-resort forced call if the capped final step still failed to parse.
            turn = self.step(messages, provider=provider, force_final=True)
            messages = turn["messages"]
            model_name = turn["response"].get("model") or model_name
            forced_final = True
            steps_used += 1
            parsed = turn["parsed"]

        if parsed is None:
            raise AgentError(
                f"Provider '{provider}' did not return a parseable final diagnosis "
                f"after {self.max_steps} steps."
            )

        t1_unix = time.time()
        t1 = format_iso(t1_unix)
        ttd = max(0.0, round(t1_unix - incident.t0_unix, 2))

        return Diagnosis(
            root_cause_service=str(parsed["root_cause_service"]),
            root_cause=str(parsed["root_cause"]),
            evidence=list(parsed["evidence"]),
            causal_chain=list(parsed["causal_chain"]),
            confidence=float(parsed["confidence"]),
            proposed_fix=dict(parsed["proposed_fix"]),
            mitigation_vs_cure=str(parsed["mitigation_vs_cure"]),
            incident_id=incident.id,
            t0=incident.t0,
            t0_unix=incident.t0_unix,
            t1=t1,
            t1_unix=t1_unix,
            time_to_diagnosis_seconds=ttd,
            provider=provider,
            model=model_name,
            steps_used=steps_used,
            forced_final=forced_final,
            tool_trace=tool_trace,
        )

    def _record_diagnosis(self, incident: Incident, diagnosis: Diagnosis) -> None:
        """Persist T1 / diagnosis onto the incident and audit log."""
        meta = {
            "t1": diagnosis.t1,
            "t1_unix": diagnosis.t1_unix,
            "time_to_diagnosis_seconds": diagnosis.time_to_diagnosis_seconds,
            "diagnosis": {
                "root_cause_service": diagnosis.root_cause_service,
                "root_cause": diagnosis.root_cause,
                "evidence": diagnosis.evidence,
                "causal_chain": diagnosis.causal_chain,
                "confidence": diagnosis.confidence,
                "proposed_fix": diagnosis.proposed_fix,
                "mitigation_vs_cure": diagnosis.mitigation_vs_cure,
                "provider": diagnosis.provider,
                "model": diagnosis.model,
                "steps_used": diagnosis.steps_used,
                "forced_final": diagnosis.forced_final,
            },
        }

        if self.detector is not None:
            # Keep status diagnosing until the fix pipeline (Phase 6) takes over.
            self.detector.update_incident_status(
                incident.id,
                "diagnosing",
                metadata_update=meta,
            )
            # Refresh local object
            refreshed = self.detector.get_incident(incident.id)
            if refreshed:
                incident.metadata.update(refreshed.metadata)

        try:
            self.supervisor.log_change(
                action="agent_diagnosis",
                service=diagnosis.root_cause_service or ",".join(incident.affected_services),
                details={
                    "incident_id": incident.id,
                    "t0": diagnosis.t0,
                    "t1": diagnosis.t1,
                    "time_to_diagnosis_seconds": diagnosis.time_to_diagnosis_seconds,
                    "root_cause": diagnosis.root_cause,
                    "evidence": diagnosis.evidence,
                    "confidence": diagnosis.confidence,
                    "provider": diagnosis.provider,
                },
                author="agent",
            )
        except Exception:
            pass

    def _resolve_incident(self, incident: Incident | dict[str, Any] | str) -> Incident:
        if isinstance(incident, Incident):
            return incident
        if isinstance(incident, str):
            if self.detector is None:
                raise AgentError("Cannot load incident by id without a detector")
            loaded = self.detector.get_incident(incident)
            if not loaded:
                raise AgentError(f"Incident '{incident}' not found")
            return loaded
        if isinstance(incident, dict):
            return Incident(
                id=str(incident["id"]),
                status=str(incident.get("status", "open")),
                trigger_rule=str(incident.get("trigger_rule", "unknown")),
                t0=str(incident["t0"]),
                t0_unix=float(incident["t0_unix"]),
                created_at=str(incident.get("created_at", incident["t0"])),
                created_at_unix=float(
                    incident.get("created_at_unix", incident["t0_unix"])
                ),
                ttd_seconds=float(incident.get("ttd_seconds", 0.0)),
                affected_services=list(incident.get("affected_services") or []),
                evidence=list(incident.get("evidence") or []),
                breaches=list(incident.get("breaches") or []),
                metadata=dict(incident.get("metadata") or {}),
            )
        raise AgentError(f"Unsupported incident type: {type(incident)!r}")

    def _build_incident_summary(self, incident: Incident) -> str:
        health: dict[str, Any] = {}
        try:
            health = self.tools.obs.get_health()
        except Exception as exc:  # noqa: BLE001
            health = {"error": str(exc)}

        summary = {
            "incident_id": incident.id,
            "status": incident.status,
            "trigger_rule": incident.trigger_rule,
            "t0": incident.t0,
            "t0_unix": incident.t0_unix,
            "time_to_detect_seconds": incident.ttd_seconds,
            "affected_services": incident.affected_services,
            "detector_evidence": incident.evidence,
            "breaches": incident.breaches[:10],
            "current_health": health,
            "instructions": (
                "Investigate this incident. Start with get_changes near T0, then "
                "walk dependencies upstream from affected services. Cite evidence IDs."
            ),
        }
        return json.dumps(summary, default=str, indent=2)

    @staticmethod
    def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
        """Build an OpenAI-compatible assistant message including tool_calls."""
        raw = response.get("message")
        if raw is not None and hasattr(raw, "tool_calls"):
            # Prefer the SDK message object converted to a plain dict shape.
            msg: dict[str, Any] = {"role": "assistant", "content": raw.content}
            if raw.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in raw.tool_calls
                ]
            return msg

        msg = {"role": "assistant", "content": response.get("text") or None}
        tool_calls = response.get("tool_calls") or []
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc.get("arguments")
                        if isinstance(tc.get("arguments"), str)
                        else json.dumps(tc.get("arguments") or {}),
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]
        return msg

    @staticmethod
    def _parse_final_answer(text: str) -> dict[str, Any] | None:
        """Extract and validate the structured final JSON diagnosis."""
        if not text or not text.strip():
            return None

        candidates: list[str] = []
        stripped = text.strip()
        candidates.append(stripped)

        # Fenced ```json ... ``` blocks
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL):
            candidates.append(match.group(1))

        # First JSON object in the text
        brace = stripped.find("{")
        if brace >= 0:
            depth = 0
            for i, ch in enumerate(stripped[brace:], start=brace):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(stripped[brace : i + 1])
                        break

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if not all(k in data for k in FINAL_ANSWER_KEYS):
                continue
            return Agent._normalize_final_answer(data)
        return None

    @staticmethod
    def _normalize_final_answer(data: dict[str, Any]) -> dict[str, Any]:
        evidence = data.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        causal = data.get("causal_chain") or []
        if isinstance(causal, str):
            causal = [causal]

        proposed = data.get("proposed_fix") or {}
        if isinstance(proposed, str):
            proposed = {"action": proposed, "args": {}}
        if not isinstance(proposed, dict):
            proposed = {"action": "restart_service", "args": {}}
        proposed.setdefault("action", "restart_service")
        proposed.setdefault("args", {})

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        mitigation = str(data.get("mitigation_vs_cure") or "mitigation").lower().strip()
        if mitigation not in ("mitigation", "cure"):
            mitigation = "mitigation"

        return {
            "root_cause_service": str(data.get("root_cause_service") or "").strip(),
            "root_cause": str(data.get("root_cause") or "").strip(),
            "evidence": [str(e) for e in evidence],
            "causal_chain": [str(c) for c in causal],
            "confidence": confidence,
            "proposed_fix": proposed,
            "mitigation_vs_cure": mitigation,
        }


# =========================================================================
# CLI
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-SRE Agent Core CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inv = sub.add_parser("investigate", help="Investigate an incident by ID (or active)")
    p_inv.add_argument("incident_id", nargs="?", default=None, help="Incident ID")
    p_inv.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help=f"Max investigation steps (default {MAX_STEPS})",
    )

    sub.add_parser("prompt", help="Print the system prompt")

    args = parser.parse_args(argv)

    if args.command == "prompt":
        print(SYSTEM_PROMPT)
        return 0

    if args.command == "investigate":
        obs = get_observability()
        detector = IncidentDetector(observability=obs)
        agent = Agent(
            detector=detector,
            tools=ToolRegistry(obs),
            max_steps=args.max_steps,
        )

        if args.incident_id:
            incident = detector.get_incident(args.incident_id)
        else:
            incident = detector.get_active_incident()

        if not incident:
            print("No active incident to investigate.")
            return 1

        print(f"Investigating {incident.id} (T0={incident.t0}, trigger={incident.trigger_rule})...")
        try:
            diagnosis = agent.investigate(incident)
        except (AgentError, LLMUnavailable) as exc:
            print(f"[FAIL] {exc}")
            return 1

        print(json.dumps(diagnosis.to_dict(), indent=2, default=str))
        print(
            f"\nT1={diagnosis.t1} | Time-to-diagnosis={diagnosis.time_to_diagnosis_seconds:.2f}s "
            f"| provider={diagnosis.provider} | steps={diagnosis.steps_used}"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
