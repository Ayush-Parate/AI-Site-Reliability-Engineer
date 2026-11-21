"""Pytest fixtures and configuration for AI-SRE test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def ensure_clean_versions(monkeypatch):
    """Ensure service VERSION files and env overrides are baseline 1.0.0 before and after tests."""
    services = ["gateway", "orders", "payments", "inventory"]
    for s in services:
        ver_file = REPO_ROOT / "services" / s / "VERSION"
        if ver_file.is_file():
            ver_file.write_text("1.0.0\n", encoding="utf-8")
        monkeypatch.delenv(f"{s.upper()}_VERSION", raising=False)

    yield

    for s in services:
        ver_file = REPO_ROOT / "services" / s / "VERSION"
        if ver_file.is_file():
            ver_file.write_text("1.0.0\n", encoding="utf-8")

