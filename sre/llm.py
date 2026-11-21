"""LLM provider wrapper for local Ollama (qwen2.5).

Speaks the OpenAI-compatible chat-completions interface supported by Ollama at
http://localhost:11434/v1. The dedicated model for this project is qwen2.5.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ProviderConfig:
    """Connection details for Ollama OpenAI-compatible endpoint."""

    name: str = "ollama"
    base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    api_key_env: str = "OLLAMA_API_KEY"
    default_model: str = "qwen2.5"
    requires_key: bool = False

    @property
    def model(self) -> str:
        return os.getenv("OLLAMA_MODEL", self.default_model)

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None


PROVIDERS: dict[str, ProviderConfig] = {
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
    """Raised when Ollama is unavailable or fails for a request."""


def _provider_order() -> list[str]:
    """Provider names to try (defaults to ollama)."""
    primary = (os.getenv("PRIMARY_PROVIDER") or "ollama").strip().lower()
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
    """A chat client for Ollama running qwen2.5 via the OpenAI-compatible API."""

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

    def _client(self, name: str = "ollama") -> Any:
        """Return a cached OpenAI-compatible client for Ollama."""
        if name in self._clients:
            return self._clients[name]

        from openai import OpenAI

        if name not in PROVIDERS:
            raise LLMConfigError(f"Unknown provider '{name}'. Known: {', '.join(sorted(PROVIDERS))}")

        cfg = PROVIDERS[name]
        client = OpenAI(
            api_key=cfg.api_key or "ollama",
            base_url=cfg.base_url,
        )
        self._clients[name] = client
        return client

    def is_configured(self, name: str = "ollama") -> bool:
        """True if the provider is registered."""
        return name in PROVIDERS

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
        """Send a chat request to Ollama qwen2.5.

        Returns a dict with ``text``, ``tool_calls``, ``provider``, ``model``
        and the raw ``message``. Raises :class:`LLMUnavailable` on error.
        """
        target_provider = provider or (self.order[0] if self.order else "ollama")
        if target_provider not in PROVIDERS:
            raise LLMConfigError(f"Unknown provider '{target_provider}'. Known: {', '.join(sorted(PROVIDERS))}")

        try:
            return self._chat_once(target_provider, messages, tools, model, temperature, max_tokens)
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"Ollama provider '{target_provider}' failed: {exc}") from exc

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
        """Send a tiny prompt to verify Ollama connectivity.

        Returns a mapping of provider name to a result dict with ``ok`` and
        either ``text`` or ``error``.
        """
        results: dict[str, dict[str, Any]] = {}
        for name in self.order:
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
            except Exception as exc:  # noqa: BLE001
                results[name] = {"ok": False, "error": str(exc)}
        return results
