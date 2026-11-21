"""Tests for the LLM provider wrapper (no network required)."""

import os

import pytest

from sre.llm import LLM, LLMConfigError, PROVIDERS


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


def test_provider_order_primary_then_backup(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("BACKUP_PROVIDER", "ollama")
    llm = LLM()
    assert llm.order == ["openai", "ollama"]


def test_provider_order_dedupes_when_primary_equals_backup(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "groq")
    monkeypatch.setenv("BACKUP_PROVIDER", "groq")
    llm = LLM()
    assert llm.order == ["groq"]


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "not-a-provider")
    with pytest.raises(LLMConfigError):
        LLM()


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "ollama")
    monkeypatch.setenv("BACKUP_PROVIDER", "openai")
    llm = LLM()
    assert llm.is_configured("ollama") is True
    assert llm.is_configured("openai") is False


def test_keyed_provider_requires_key(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    llm = LLM()
    assert llm.is_configured("openai") is False
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert llm.is_configured("openai") is True


def test_model_override_from_env(monkeypatch):
    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-custom")
    llm = LLM()
    assert PROVIDERS["openai"].model == "gpt-custom"


def test_all_providers_have_required_fields():
    for name, cfg in PROVIDERS.items():
        assert cfg.base_url
        assert cfg.api_key_env
        assert cfg.default_model
        assert name == cfg.name


def test_chat_single_provider_success(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    llm = LLM()

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "pong"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

    llm._clients["openai"] = mock_client
    resp = llm.chat([{"role": "user", "content": "ping"}])
    assert resp["text"] == "pong"
    assert resp["provider"] == "openai"


def test_chat_failover_to_backup(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BACKUP_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    llm = LLM()

    primary_client = MagicMock()
    primary_client.chat.completions.create.side_effect = RuntimeError("Rate limit")

    backup_client = MagicMock()
    backup_choice = MagicMock()
    backup_choice.message.content = "recovered by backup"
    backup_choice.message.tool_calls = None
    backup_client.chat.completions.create.return_value = MagicMock(choices=[backup_choice])

    llm._clients["openai"] = primary_client
    llm._clients["groq"] = backup_client

    resp = llm.chat([{"role": "user", "content": "ping"}])
    assert resp["text"] == "recovered by backup"
    assert resp["provider"] == "groq"


def test_chat_all_fail_raises_llmunavailable(monkeypatch):
    from unittest.mock import MagicMock
    from sre.llm import LLMUnavailable

    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    llm = LLM()

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("Network error")
    llm._clients["openai"] = mock_client

    with pytest.raises(LLMUnavailable):
        llm.chat([{"role": "user", "content": "ping"}])


def test_test_method_returns_text_when_configured(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("PRIMARY_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    llm = LLM()

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "pong"
    mock_choice.message.tool_calls = None
    mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])
    llm._clients["openai"] = mock_client

    res = llm.test()
    assert res["openai"]["ok"] is True
    assert res["openai"]["text"] == "pong"
