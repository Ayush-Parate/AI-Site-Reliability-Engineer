#!/usr/bin/env python3
"""Single entry point for the AI-SRE demo.

Phase 0 scope: verify the environment and LLM configuration. Later phases add
the service supervisor, observability collector, detector, agent and dashboard
under the same command.

Usage:
    python run.py                # print environment / provider status
    python run.py --test-llm     # send a test call to each configured provider
"""

from __future__ import annotations

import argparse
import sys

from sre import __version__
from sre.llm import LLM, LLMConfigError, PROVIDERS


def _print_status() -> None:
    print(f"AI-SRE v{__version__} — Phase 0 setup")
    print(f"Python: {sys.version.split()[0]}")
    print()

    llm = LLM()
    print("LLM provider order (primary -> backup):")
    for i, name in enumerate(llm.order):
        role = "primary" if i == 0 else "backup"
        cfg = PROVIDERS[name]
        state = "configured" if llm.is_configured(name) else f"missing {cfg.api_key_env}"
        print(f"  {role:7} {name:11} model={cfg.model:28} [{state}]")
    print()


def _run_llm_test() -> int:
    print("Testing each configured provider...\n")
    results = LLM().test()
    ok_all = True
    for name, res in results.items():
        if res["ok"]:
            print(f"  [PASS] {name}: {res['text']!r} (model={res.get('model')})")
        else:
            ok_all = False
            print(f"  [FAIL] {name}: {res['error']}")
    print()
    if not any(r["ok"] for r in results.values()):
        print("No provider returned text. Set a key in .env or start Ollama.")
        return 1
    if not ok_all:
        print("Some providers failed; at least one works, which is enough to run.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Site Reliability Engineer")
    parser.add_argument(
        "--test-llm",
        action="store_true",
        help="Send a test call to each configured LLM provider and exit.",
    )
    args = parser.parse_args(argv)

    try:
        if args.test_llm:
            return _run_llm_test()
        _print_status()
        print("Phase 0 ready. Services, detector, agent and dashboard arrive in later phases.")
        return 0
    except LLMConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())