"""Allowlisted read-only investigation tools for the AI-SRE agent.

Every tool validates arguments in code, truncates output for LLM context,
and returns results that already carry stable evidence IDs (LOG-x, MET-x, CHG-x).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sre.observability import Observability, get_observability

# Truncation limits so tool results fit the LLM context window.
MAX_LOG_ENTRIES = 30
MAX_METRIC_SAMPLES = 20
MAX_CHANGES = 30
MAX_RESULT_CHARS = 12_000

KNOWN_SERVICES = ("gateway", "orders", "payments", "inventory")


def _truncate_json(payload: Any, max_chars: int = MAX_RESULT_CHARS) -> str:
    """Serialize to JSON and truncate if oversized."""
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80] + f'... [truncated, original_chars={len(text)}]"'


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected integer, got {value!r}") from exc
    return max(lo, min(hi, n))


class ToolRegistry:
    """Allowlist of investigation tools with OpenAI function schemas."""

    def __init__(self, observability: Observability | None = None) -> None:
        self.obs = observability or get_observability()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "get_logs": self._get_logs,
            "get_metrics": self._get_metrics,
            "get_deps": self._get_deps,
            "get_changes": self._get_changes,
            "get_health": self._get_health,
            "get_evidence": self._get_evidence,
        }

    @property
    def names(self) -> list[str]:
        return list(self._handlers.keys())

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-compatible tool definitions for chat.completions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_logs",
                    "description": (
                        "Query structured service logs. Each line includes an evidence ID "
                        "(LOG-x). Prefer filtering by service, since, level, or trace_id."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Service name (gateway, orders, payments, inventory).",
                            },
                            "since": {
                                "type": ["string", "number"],
                                "description": "Lookback window (seconds, e.g. 120) or ISO timestamp.",
                            },
                            "level": {
                                "type": "string",
                                "description": "Log level filter (ERROR, WARN, INFO).",
                            },
                            "trace_id": {
                                "type": "string",
                                "description": "Optional x-trace-id to follow a request across services.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": f"Max entries (default 20, max {MAX_LOG_ENTRIES}).",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_metrics",
                    "description": (
                        "Query time-series metric samples with summary stats. "
                        "Samples carry evidence IDs (MET-x)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Service name to filter.",
                            },
                            "metric": {
                                "type": "string",
                                "description": "Metric name or wildcard (e.g. process_resident_memory_bytes).",
                            },
                            "window": {
                                "type": ["string", "number"],
                                "description": "Lookback window in seconds (default 60).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": f"Max samples (default 20, max {MAX_METRIC_SAMPLES}).",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_deps",
                    "description": (
                        "Return upstream (who calls this service) and downstream "
                        "(who this service calls) from the dependency graph. "
                        "Walk upstream from symptoms toward the source."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Service name. Omit for the full graph.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_changes",
                    "description": (
                        "List recent deploys, restarts, chaos injections, and other "
                        "audit changes. Check changes first. Each entry has CHG-x."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "since": {
                                "type": ["string", "number"],
                                "description": "Lookback window (seconds) or ISO timestamp.",
                            },
                            "service": {
                                "type": "string",
                                "description": "Optional service filter.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": f"Max entries (default 20, max {MAX_CHANGES}).",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_health",
                    "description": "Current live /health status for one service or all services.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Service name. Omit to check all services.",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_evidence",
                    "description": (
                        "Look up the underlying record for a citation ID "
                        "(LOG-x, MET-x, or CHG-x)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "evidence_id": {
                                "type": "string",
                                "description": "Evidence ID such as LOG-1042, MET-77, or CHG-9.",
                            },
                        },
                        "required": ["evidence_id"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def call(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        """Validate and execute an allowlisted tool. Returns a JSON string."""
        if name not in self._handlers:
            return _truncate_json(
                {
                    "error": f"Unknown tool '{name}'. Allowlisted: {', '.join(self.names)}",
                    "blocked": True,
                }
            )

        try:
            args = self._parse_args(arguments)
            result = self._handlers[name](args)
            return _truncate_json(result)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
            return _truncate_json({"error": str(exc), "tool": name})

    @staticmethod
    def _parse_args(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            return arguments
        text = str(arguments).strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return parsed

    def _validate_service(self, service: str | None) -> str | None:
        if service is None:
            return None
        clean = service.lower().strip()
        if clean in ("all", "*", ""):
            return None
        # Prefer registry from deps.yaml; fall back to known demo services.
        try:
            registry = self.obs._load_dependencies()
            valid = set(registry.keys()) or set(KNOWN_SERVICES)
        except Exception:
            valid = set(KNOWN_SERVICES)
        if clean not in valid:
            raise ValueError(f"Unknown service '{service}'. Valid: {', '.join(sorted(valid))}")
        return clean

    def _get_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(_optional_str(args.get("service")))
        limit = _optional_int(args.get("limit"), 20, lo=1, hi=MAX_LOG_ENTRIES)
        entries = self.obs.get_logs(
            service=service,
            since=args.get("since"),
            level=_optional_str(args.get("level")),
            trace_id=_optional_str(args.get("trace_id")),
            limit=limit,
        )
        evidence_ids = [e.get("id") for e in entries if e.get("id")]
        return {
            "count": len(entries),
            "evidence_ids": evidence_ids,
            "logs": entries,
        }

    def _get_metrics(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(_optional_str(args.get("service")))
        limit = _optional_int(args.get("limit"), 20, lo=1, hi=MAX_METRIC_SAMPLES)
        result = self.obs.get_metrics(
            service=service,
            metric=_optional_str(args.get("metric")),
            window=args.get("window", 60),
            limit=limit,
        )
        samples = result.get("samples") or []
        result["evidence_ids"] = [s.get("id") for s in samples if s.get("id")]
        return result

    def _get_deps(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(_optional_str(args.get("service")))
        return self.obs.get_deps(service=service)

    def _get_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(_optional_str(args.get("service")))
        limit = _optional_int(args.get("limit"), 20, lo=1, hi=MAX_CHANGES)
        changes = self.obs.get_changes(
            since=args.get("since"),
            service=service,
            limit=limit,
        )
        return {
            "count": len(changes),
            "evidence_ids": [c.get("id") for c in changes if c.get("id")],
            "changes": changes,
        }

    def _get_health(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._validate_service(_optional_str(args.get("service")))
        return self.obs.get_health(service=service)

    def _get_evidence(self, args: dict[str, Any]) -> dict[str, Any]:
        eid = _optional_str(args.get("evidence_id"))
        if not eid:
            raise ValueError("evidence_id is required")
        record = self.obs.get_evidence(eid)
        if record is None:
            return {"evidence_id": eid, "found": False, "error": f"No record for {eid}"}
        return {"evidence_id": eid, "found": True, "record": record}
