"""Tests for the Ollama (qwen2.5) LLM wrapper (no network required)."""

import os
import pytest
from unittest.mock import MagicMock

from sre.llm import LLM, LLMConfigError, LLMUnavailable, PROVIDERS


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clear provider-related env vars before each test."""
    for key in list(os.environ):
        if key.endswith("_API_KEY") or key.endswith("_MODEL") or key in (
            "PRIMARY_PROVIDER",
            "BACKUP_PROVIDER",
            "OLLAMA_BASE_URL",
        ):
            monkeypatch.delenv(key, raising=False)
    yield


def test_default_provider_is_ollama():
    llm = LLM()
    assert llm.order == ["ollama"]


def test_provider_order_dedupes(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "ollama")
    monkeypatch.setenv("BACKUP_PROVIDER", "ollama")
    llm = LLM()
    assert llm.order == ["ollama"]


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    with pytest.raises(LLMConfigError, match="Unknown provider 'openai'"):
        LLM()


def test_ollama_needs_no_key():
    llm = LLM()
    assert llm.is_configured("ollama") is True


def test_model_defaults_to_qwen25():
    assert PROVIDERS["ollama"].default_model == "qwen2.5"
    assert PROVIDERS["ollama"].model == "qwen2.5"


def test_model_override_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    assert PROVIDERS["ollama"].model == "qwen2.5:7b"


def test_providers_dict_contains_only_ollama():
    assert list(PROVIDERS.keys()) == ["ollama"]
    cfg = PROVIDERS["ollama"]
    assert cfg.name == "ollama"
    assert "11434" in cfg.base_url
    assert cfg.requires_key is False


def test_chat_success():
    llm = LLM()

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "pong"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

    llm._clients["ollama"] = mock_client
    resp = llm.chat([{"role": "user", "content": "ping"}])
    assert resp["text"] == "pong"
    assert resp["provider"] == "ollama"
    assert resp["model"] == "qwen2.5"


def test_chat_all_fail_raises_llmunavailable():
    llm = LLM()

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("Connection refused")
    llm._clients["ollama"] = mock_client

    with pytest.raises(LLMUnavailable, match="Ollama provider 'ollama' failed"):
        llm.chat([{"role": "user", "content": "ping"}])


def test_complete_convenience_method():
    llm = LLM()

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "pong"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
    llm._clients["ollama"] = mock_client

    assert llm.complete("ping") == "pong"


def test_test_method_returns_text_when_configured():
    llm = LLM()

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "pong"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
    llm._clients["ollama"] = mock_client

    res = llm.test()
    assert res["ollama"]["ok"] is True
    assert res["ollama"]["text"] == "pong"
