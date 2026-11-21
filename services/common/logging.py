"""Structured JSON logging with request-scoped trace IDs.

Required fields per Phase 1 spec:
ts, level, service, trace_id, msg, latency_ms, status
"""

from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

# ContextVar for request-scoped trace_id
current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_trace_id", default="-"
)


def get_current_trace_id() -> str:
    """Return the active trace_id in the current async context."""
    return current_trace_id.get()


def set_current_trace_id(trace_id: str) -> None:
    """Set the active trace_id for the current async context."""
    current_trace_id.set(trace_id)


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON adhering to the Phase 1 schema."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        # Extract explicit extra attributes if passed, or defaults
        trace_id = getattr(record, "trace_id", None) or get_current_trace_id()
        latency_ms = getattr(record, "latency_ms", 0.0)
        status = getattr(record, "status", None)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        entry: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "service": self.service_name,
            "trace_id": str(trace_id),
            "msg": record.getMessage(),
            "latency_ms": round(float(latency_ms), 2) if latency_ms is not None else 0.0,
            "status": int(status) if status is not None else 0,
        }

        # Include additional extra attributes if present
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            for k, v in record.extra_data.items():
                if k not in entry:
                    entry[k] = v

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def setup_logger(
    service_name: str,
    log_dir: str | Path | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Set up and return a service logger that writes structured JSON to logs/<service>.log."""
    if log_dir is None:
        # Default to repo root / logs
        repo_root = Path(__file__).resolve().parent.parent.parent
        log_dir = repo_root / "logs"
    else:
        log_dir = Path(log_dir)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{service_name}.log"

    logger = logging.getLogger(f"sre.{service_name}")
    logger.setLevel(level)

    # Avoid adding duplicate handlers on re-initialization
    if not any(
        isinstance(h, logging.FileHandler) and getattr(h, "_service_file", None) == str(log_file)
        for h in logger.handlers
    ):
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler._service_file = str(log_file)  # type: ignore[attr-defined]
        file_handler.setFormatter(JSONFormatter(service_name))
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    # Also log to console in non-JSON or concise format for local visibility
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        stream_handler = logging.StreamHandler()
        stream_formatter = logging.Formatter(
            f"[{service_name}] %(levelname)s %(asctime)s: %(message)s"
        )
        stream_handler.setFormatter(stream_formatter)
        stream_handler.setLevel(level)
        logger.addHandler(stream_handler)

    logger.propagate = False
    return logger
