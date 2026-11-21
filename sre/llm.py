"""LLM provider wrapper with failover.

All providers speak the OpenAI-compatible chat-completions interface, so a
single ``openai.OpenAI`` client (with a custom ``base_url``) covers every
provider. ``PROVIDERS`` lists the known providers; ``PRIMARY_PROVIDER`` and
``BACKUP_PROVIDER`` in ``.env`` pick the order in which they are tried.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ProviderConfig:
    """Connection details for one OpenAI-compatible provider."""

    name: str
    base_url: str
    api_key_env: str
    default_model: str
    requires_key: bool = True

    @property
    def model(self) -> str:
        return os.getenv(f"{self.name.upper()}_MODEL", self.default_model)

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None


PROVIDERS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
    ),
    "gemini": ProviderConfig(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-2.0-flash",
    ),
    "groq": ProviderConfig(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
    ),
    "ollama": ProviderConfig(
        name="ollama",
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        api_key_env="OLLAMA_API_KEY",
        default_model="qwen2.5",
        requires_key=False,
    ),
}


class LLMConfigError(RuntimeError):
    """Raised when no usable provider is configured."""


class LLMUnavailable(RuntimeError):
    """Raised when every configured provider fails for a request."""


def _provider_order() -> list[str]:
    """Provider names to try, primary first then backup, de-duplicated."""
    primary = (os.getenv("PRIMARY_PROVIDER") or "openai").strip().lower()
    backup = (os.getenv("BACKUP_PROVIDER") or "ollama").strip().lower()

    order: list[str] = []
    for name in (primary, backup):
        if name not in PROVIDERS:
            raise LLMConfigError(
                f"Unknown provider '{name}'. Known: {', '.join(sorted(PROVIDERS))}"
            )
        if name not in order:
            order.append(name)
    return order


class LLM:
    """A chat client that fails over across OpenAI-compatible providers."""

    def __init__(
        self,
        primary: str | None = None,
        backup: str | None = None,
    ) -> None:
        if primary:
            os.environ["PRIMARY_PROVIDER"] = primary
        if backup:
            os.environ["BACKUP_PROVIDER"] = backup

        self.order = _provider_order()
        self._clients: dict[str, Any] = {}
        self._ensure_import()

    @staticmethod
    def _ensure_import() -> None:
        try:
            import openai  # noqa: F401
        except ImportError as exc:  # pragma: no cover - import guard
            raise LLMConfigError(
                "The 'openai' package is required. Run: pip install -r requirements.txt"
            ) from exc

    def _client(self, name: str) -> Any:
        """Return a cached OpenAI-compatible client for a provider."""
        if name in self._clients:
            return self._clients[name]

        from openai import OpenAI

        cfg = PROVIDERS[name]
        if cfg.requires_key and not cfg.api_key:
            raise LLMConfigError(
                f"Provider '{name}' is selected but {cfg.api_key_env} is not set."
            )

        client = OpenAI(
            api_key=cfg.api_key or "not-needed",
            base_url=cfg.base_url,
        )
        self._clients[name] = client
        return client

    def is_configured(self, name: str) -> bool:
        """True if the provider has what it needs to run."""
        cfg = PROVIDERS[name]
        if not cfg.requires_key:
            return True
        return bool(cfg.api_key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat request, failing over across providers.

        Returns a dict with ``text``, ``tool_calls``, ``provider``, ``model``
        and the raw ``message``. Raises :class:`LLMUnavailable` if every
        provider in the order fails.
        """
        names = [provider] if provider else list(self.order)
        errors: list[str] = []

        for name in names:
            if not self.is_configured(name):
                errors.append(f"{name}: not configured")
                continue
            try:
                return self._chat_once(name, messages, tools, model, temperature, max_tokens)
            except Exception as exc:  # noqa: BLE001 - failover on any provider error
                errors.append(f"{name}: {exc}")

        raise LLMUnavailable(
            "All configured LLM providers failed:\n  " + "\n  ".join(errors)
        )

    def _chat_once(
        self,
        name: str,
        messages: list[dict[str, Any]],
        tools: Iterable[dict[str, Any]] | None,
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        cfg = PROVIDERS[name]
        kwargs: dict[str, Any] = {
            "model": model or cfg.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        response = self._client(name).chat.completions.create(**kwargs)
        message = response.choices[0].message
        tool_calls = [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in (message.tool_calls or [])
        ]
        return {
            "text": message.content or "",
            "tool_calls": tool_calls,
            "provider": name,
            "model": kwargs["model"],
            "message": message,
        }

    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Convenience wrapper returning just the assistant text."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)["text"]

    def test(self) -> dict[str, dict[str, Any]]:
        """Send a tiny prompt to each configured provider.

        Used by the Phase 0 exit check. Returns a mapping of provider name to
        a result dict with ``ok`` and either ``text`` or ``error``.
        """
        results: dict[str, dict[str, Any]] = {}
        for name in self.order:
            if not self.is_configured(name):
                results[name] = {
                    "ok": False,
                    "error": f"missing {PROVIDERS[name].api_key_env}",
                }
                continue
            try:
                out = self._chat_once(
                    name,
                    [{"role": "user", "content": "Reply with the single word: pong"}],
                    tools=None,
                    model=None,
                    temperature=0.0,
                    max_tokens=16,
                )
                results[name] = {"ok": True, "text": out["text"].strip(), "model": out["model"]}
            except Exception as exc:  # noqa: BLE001 - report per-provider
                results[name] = {"ok": False, "error": str(exc)}
        return results