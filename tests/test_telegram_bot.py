"""Unit tests for Telegram Incident & Approval Bot (sre/telegram_bot.py)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sre.telegram_bot import (
    SREApiClient,
    SRETelegramBot,
    format_approval_alert,
    format_incidents_report,
    format_postmortem_summary,
    format_status_report,
)


def run_async(coro):
    """Helper to run an async coroutine synchronously in tests."""
    return asyncio.run(coro)


# =============================================================================
# 1. Tests for Message Formatters
# =============================================================================

def test_format_approval_alert_basic():
    appr = {
        "id": "APP-42",
        "incident_id": "INC-7",
        "action": "rollback",
        "service": "payments",
        "args": {"to_version": "1.0.0"},
        "reason": "Breaking change causes 500 errors",
        "created_at": "2026-11-21T18:00:00Z",
    }
    msg = format_approval_alert(appr)
    assert "HUMAN APPROVAL REQUIRED" in msg
    assert "APP-42" in msg
    assert "INC-7" in msg
    assert "rollback" in msg
    assert "payments" in msg
    assert "Breaking change causes 500 errors" in msg


def test_format_approval_alert_escapes_html():
    appr = {
        "id": "<bad_id>",
        "incident_id": "INC-1",
        "action": "sql_drop",
        "service": "orders",
        "args": {"raw": "<script>alert(1)</script>"},
        "reason": "Risk < high & risky >",
    }
    msg = format_approval_alert(appr)
    assert "<script>" not in msg
    assert "&lt;bad_id&gt;" in msg
    assert "&lt;script&gt;" in msg
    assert "&amp;" in msg


def test_format_status_report_dict_services():
    status_data = {
        "timestamp": "2026-11-21T18:30:00Z",
        "services": {
            "gateway": {"port": 8000, "healthy": True, "status": "running", "error_rate": 0.0, "p95_latency_ms": 1.5},
            "payments": {"port": 8002, "healthy": False, "status": "unhealthy", "error_rate": 0.25, "p95_latency_ms": 450.0},
        },
        "active_incident": {"id": "INC-1", "trigger_rule": "error_rate_threshold", "ttd_seconds": 1.2},
        "pending_approvals_count": 1,
    }
    msg = format_status_report(status_data)
    assert "AI-SRE Microservices Status" in msg
    assert "GATEWAY" in msg
    assert "🟢" in msg
    assert "PAYMENTS" in msg
    assert "🟡" in msg or "🔴" in msg
    assert "INC-1" in msg
    assert "Pending Approvals" in msg
    assert "1 action(s) awaiting operator sign-off" in msg


def test_format_status_report_healthy_all():
    status_data = {
        "timestamp": "2026-11-21T18:30:00Z",
        "services": [
            {"service": "gateway", "port": 8000, "healthy": True, "status": "running", "error_rate_pct": 0.0, "p95_latency_ms": 1.0},
        ],
        "active_incident": None,
        "pending_approvals_count": 0,
    }
    msg = format_status_report(status_data)
    assert "No Active Incidents" in msg
    assert "Pending Approvals" not in msg


def test_format_incidents_report():
    inc_data = {
        "active": {
            "id": "INC-3",
            "trigger_rule": "latency_slo_breach",
            "affected_services": ["orders", "payments"],
            "t0": "2026-11-21T18:00:00Z",
            "ttd_seconds": 2.45,
        },
        "recent": [
            {"id": "INC-2", "trigger_rule": "error_rate_threshold", "status": "resolved"},
            {"id": "INC-1", "rule": "consecutive_health_failures", "status": "resolved"},
        ],
    }
    msg = format_incidents_report(inc_data)
    assert "ACTIVE INCIDENT: INC-3" in msg
    assert "latency_slo_breach" in msg
    assert "orders, payments" in msg
    assert "INC-2" in msg
    assert "INC-1" in msg


def test_format_postmortem_summary_truncation():
    large_markdown = "# Incident Postmortem INC-99\n\n" + ("- Detailed step in causal chain.\n" * 300)
    msg = format_postmortem_summary({"incident_id": "INC-99", "markdown": large_markdown})
    assert "Incident Postmortem INC-99" in msg
    assert len(msg) <= 4096
    assert "Report truncated" in msg


# =============================================================================
# 2. Tests for SREApiClient
# =============================================================================

def test_api_client_get_status():
    async def _run():
        client = SREApiClient(base_url="http://test-server:9000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"services": {"gateway": {"healthy": True}}}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            data = await client.get_status()
            assert data["services"]["gateway"]["healthy"] is True
            mock_get.assert_called_once_with("http://test-server:9000/api/status")

    run_async(_run())


def test_api_client_get_approvals():
    async def _run():
        client = SREApiClient(base_url="http://test-server:9000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"pending": [{"id": "APP-1"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            data = await client.get_approvals()
            assert len(data["pending"]) == 1
            mock_get.assert_called_once_with("http://test-server:9000/api/approvals")

    run_async(_run())


def test_api_client_approve_action():
    async def _run():
        client = SREApiClient(base_url="http://test-server:9000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"approval": {"id": "APP-1", "status": "approved"}}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await client.approve_action("APP-1", decided_by="@alice", reason="Looks safe")
            assert res["approval"]["status"] == "approved"
            mock_post.assert_called_once_with(
                "http://test-server:9000/api/approvals/APP-1/approve",
                json={"decided_by": "@alice", "reason": "Looks safe"},
            )

    run_async(_run())


def test_api_client_deny_action():
    async def _run():
        client = SREApiClient(base_url="http://test-server:9000")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"approval": {"id": "APP-2", "status": "denied"}}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await client.deny_action("APP-2", decided_by="@bob", reason="Dangerous")
            assert res["approval"]["status"] == "denied"
            mock_post.assert_called_once_with(
                "http://test-server:9000/api/approvals/APP-2/deny",
                json={"decided_by": "@bob", "reason": "Dangerous"},
            )

    run_async(_run())


def test_api_client_get_report_and_restart():
    async def _run():
        client = SREApiClient(base_url="http://test-server:9000")
        mock_resp_rep = MagicMock()
        mock_resp_rep.json.return_value = {"incident_id": "INC-1", "markdown": "# INC-1"}
        mock_resp_rep.raise_for_status = MagicMock()

        mock_resp_rest = MagicMock()
        mock_resp_rest.json.return_value = {"status": "restarted", "service": "payments"}
        mock_resp_rest.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_get.return_value = mock_resp_rep
            mock_post.return_value = mock_resp_rest

            rep = await client.get_report("INC-1")
            assert rep["incident_id"] == "INC-1"

            rest = await client.restart_service("payments")
            assert rest["status"] == "restarted"

    run_async(_run())


# =============================================================================
# 3. Tests for Bot Command & Callback Handlers
# =============================================================================

@pytest.fixture
def bot_instance():
    # Use dummy bot token for unit test without making real Telegram network calls
    with patch("telegram.ext.ApplicationBuilder.build"):
        bot = SRETelegramBot(token="123456:TEST_TOKEN", chat_id="100200300")
        return bot


def test_cmd_help(bot_instance):
    async def _run():
        update = MagicMock()
        context = MagicMock()
        update.effective_message.reply_text = AsyncMock()

        await bot_instance.cmd_help(update, context)
        update.effective_message.reply_text.assert_called_once()
        call_args = update.effective_message.reply_text.call_args[0][0]
        assert "AI-SRE Telegram Incident & Approval Bot" in call_args
        assert "/status" in call_args
        assert "/approvals" in call_args

    run_async(_run())


def test_cmd_status(bot_instance):
    async def _run():
        update = MagicMock()
        context = MagicMock()
        update.effective_message.reply_text = AsyncMock()

        bot_instance.api.get_status = AsyncMock(return_value={
            "services": {"gateway": {"port": 8000, "healthy": True, "status": "running"}},
            "active_incident": None,
        })

        await bot_instance.cmd_status(update, context)
        update.effective_message.reply_text.assert_called_once()
        reply = update.effective_message.reply_text.call_args[0][0]
        assert "GATEWAY" in reply

    run_async(_run())


def test_cmd_approvals_with_pending(bot_instance):
    async def _run():
        update = MagicMock()
        context = MagicMock()
        update.effective_message.reply_text = AsyncMock()

        bot_instance.api.get_approvals = AsyncMock(return_value={
            "pending": [
                {
                    "id": "APP-1",
                    "incident_id": "INC-2",
                    "action": "rollback",
                    "service": "payments",
                    "args": {"service": "payments"},
                    "reason": "Test approval",
                    "created_at": "2026-11-21T10:00:00Z",
                }
            ]
        })

        await bot_instance.cmd_approvals(update, context)
        update.effective_message.reply_text.assert_called_once()
        kwargs = update.effective_message.reply_text.call_args[1]
        assert "reply_markup" in kwargs
        # Ensure inline buttons approve:APP-1 and deny:APP-1 are present
        keyboard = kwargs["reply_markup"].inline_keyboard
        callbacks = [btn.callback_data for row in keyboard for btn in row]
        assert "approve:APP-1" in callbacks
        assert "deny:APP-1" in callbacks

    run_async(_run())


def test_cmd_restart(bot_instance):
    async def _run():
        update = MagicMock()
        context = MagicMock()
        context.args = ["payments"]
        update.effective_message.reply_text = AsyncMock()

        bot_instance.api.restart_service = AsyncMock(return_value={"status": "restarted", "service": "payments"})

        await bot_instance.cmd_restart(update, context)
        bot_instance.api.restart_service.assert_called_once_with("payments")
        update.effective_message.reply_text.assert_called_once()

    run_async(_run())


def test_handle_callback_query_approve(bot_instance):
    async def _run():
        query = MagicMock()
        query.data = "approve:APP-10"
        query.from_user.username = "sre_lead"
        query.from_user.first_name = "Lead"
        query.from_user.id = 12345
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        bot_instance.api.approve_action = AsyncMock(return_value={
            "approval": {"id": "APP-10", "action": "rollback", "service": "payments"},
            "result": {"status": "verified"},
        })

        await bot_instance.handle_callback_query(update, context)
        bot_instance.api.approve_action.assert_called_once_with(
            approval_id="APP-10",
            decided_by="@sre_lead",
            reason="Approved by @sre_lead via Telegram",
        )
        query.edit_message_text.assert_called_once()
        edited_text = query.edit_message_text.call_args.kwargs.get("text") or query.edit_message_text.call_args[0][0]
        assert "APPROVED & EXECUTED" in edited_text
        assert "@sre_lead" in edited_text
        assert "APP-10" in edited_text

    run_async(_run())


def test_handle_callback_query_deny(bot_instance):
    async def _run():
        query = MagicMock()
        query.data = "deny:APP-10"
        query.from_user.username = None
        query.from_user.first_name = "Dave"
        query.from_user.id = 67890
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        bot_instance.api.deny_action = AsyncMock(return_value={
            "approval": {"id": "APP-10", "action": "rollback", "service": "payments"},
        })

        await bot_instance.handle_callback_query(update, context)
        bot_instance.api.deny_action.assert_called_once_with(
            approval_id="APP-10",
            decided_by="Dave (67890)",
            reason="Denied by Dave (67890) via Telegram",
        )
        query.edit_message_text.assert_called_once()
        edited_text = query.edit_message_text.call_args.kwargs.get("text") or query.edit_message_text.call_args[0][0]
        assert "DENIED BY OPERATOR" in edited_text
        assert "Dave (67890)" in edited_text

    run_async(_run())
