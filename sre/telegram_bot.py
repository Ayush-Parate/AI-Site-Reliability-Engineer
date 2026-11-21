"""Telegram Incident & Approval Bot for AI Site Reliability Engineer (AI-SRE).

Uses python-telegram-bot v20+ with asyncio to:
1. Poll the AI-SRE REST API for pending Tier-2 'Ask-First' approvals and active incidents.
2. Push immediate alerts to an on-call SRE Telegram group/chat.
3. Provide interactive inline keyboard buttons: [ ✅ Approve ] and [ ❌ Deny ].
4. Execute decisions via POST /api/approvals/{id}/approve or /deny and update messages in-place.
5. Provide slash commands: /status, /incidents, /approvals, /postmortem <id>, /restart <svc>, /help.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import html
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

# Telegram imports (python-telegram-bot v20+)
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger("sre.telegram_bot")
DEFAULT_API_URL = os.getenv("AI_SRE_URL", "http://localhost:9000")


# =============================================================================
# AI-SRE REST API Client
# =============================================================================

class SREApiClient:
    """Async HTTP client for the AI-SRE REST API."""

    def __init__(self, base_url: str = DEFAULT_API_URL, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_status(self) -> dict[str, Any]:
        """Fetch system summary and microservices health."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/api/status")
            resp.raise_for_status()
            return resp.json()

    async def get_incidents(self) -> dict[str, Any]:
        """Fetch active and recent incidents."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/api/incidents")
            resp.raise_for_status()
            return resp.json()

    async def get_approvals(self) -> dict[str, Any]:
        """Fetch pending approvals and recent history."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/api/approvals")
            resp.raise_for_status()
            return resp.json()

    async def approve_action(
        self, approval_id: str, decided_by: str, reason: str = "Approved via Telegram"
    ) -> dict[str, Any]:
        """Submit approval decision to AI-SRE backend."""
        payload = {"decided_by": decided_by, "reason": reason}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/approvals/{approval_id}/approve", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def deny_action(
        self, approval_id: str, decided_by: str, reason: str = "Operator denied via Telegram"
    ) -> dict[str, Any]:
        """Submit denial decision to AI-SRE backend."""
        payload = {"decided_by": decided_by, "reason": reason}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/approvals/{approval_id}/deny", json=payload
            )
            resp.raise_for_status()
            return resp.json()

    async def get_report(self, incident_id: str) -> dict[str, Any]:
        """Fetch postmortem markdown report."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/api/reports/{incident_id}")
            resp.raise_for_status()
            return resp.json()

    async def restart_service(self, service: str) -> dict[str, Any]:
        """Trigger supervisor restart for a microservice."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/services/{service}/restart")
            resp.raise_for_status()
            return resp.json()


# =============================================================================
# Message Formatters
# =============================================================================

def format_approval_alert(appr: dict[str, Any]) -> str:
    """Format a pending approval alert with HTML styling."""
    appr_id = html.escape(str(appr.get("id", "UNKNOWN")))
    inc_id = html.escape(str(appr.get("incident_id", "N/A")))
    action = html.escape(str(appr.get("action", "unknown")))
    args = appr.get("args") or {}
    service = html.escape(str(appr.get("service") or args.get("service") or "system"))
    reason = html.escape(str(appr.get("reason") or "High-risk mitigation requires human authorization."))
    created = html.escape(str(appr.get("created_at") or "")[:19].replace("T", " "))

    args_formatted = html.escape(json.dumps(args, indent=2)) if args else "{}"

    return (
        f"🚨 <b>HUMAN APPROVAL REQUIRED (Tier: Ask-First)</b>\n\n"
        f"• <b>Approval ID:</b> <code>{appr_id}</code>\n"
        f"• <b>Incident ID:</b> <code>{inc_id}</code>\n"
        f"• <b>Proposed Action:</b> <code>{action}</code> on <code>{service}</code>\n"
        f"• <b>Target Reason:</b> {reason}\n"
        f"• <b>Parameters:</b> <code>{args_formatted}</code>\n"
        f"• <b>Created At:</b> <code>{created} UTC</code>\n\n"
        f"<i>⚠️ Operator verification required before execution. Choose an action:</i>"
    )


def format_status_report(status_data: dict[str, Any]) -> str:
    """Format system services status overview."""
    raw_services = status_data.get("services", [])
    active_inc = status_data.get("active_incident")
    pending_count = status_data.get("pending_approvals_count", 0)
    ts = html.escape(str(status_data.get("timestamp", ""))[:19].replace("T", " "))

    lines = [
        f"📊 <b>AI-SRE Microservices Status</b> ({ts} UTC)\n"
    ]

    # Handle both list and dict formats gracefully
    if isinstance(raw_services, dict):
        services_list = [{"service": k, **v} for k, v in raw_services.items()]
    else:
        services_list = list(raw_services)

    for s in services_list:
        name = html.escape(str(s.get("service", "unknown")).upper())
        port = s.get("port", "-")
        healthy = s.get("healthy", False)
        st = html.escape(str(s.get("status", "unknown")).upper())
        err = s.get("error_rate_pct", round(float(s.get("error_rate", 0.0)) * 100.0, 1))
        p95 = s.get("p95_latency_ms", 0.0)

        dot = "🟢" if healthy and st == "RUNNING" else ("🟡" if st == "UNHEALTHY" else "🔴")
        lines.append(
            f"{dot} <b>{name}</b> (:{port})\n"
            f"   Status: <code>{st}</code> | Err: <code>{err}%</code> | p95: <code>{p95}ms</code>"
        )

    lines.append("")
    if active_inc:
        inc_id = html.escape(str(active_inc.get("id")))
        rule = html.escape(str(active_inc.get("trigger_rule")))
        ttd = active_inc.get("ttd_seconds", 0.0)
        lines.append(f"🔥 <b>Active Incident:</b> <code>{inc_id}</code> ({rule}, TTD: {ttd:.1f}s)")
    else:
        lines.append("✅ <b>No Active Incidents:</b> All services operating within SLO.")

    if pending_count > 0:
        lines.append(f"⚠️ <b>Pending Approvals:</b> {pending_count} action(s) awaiting operator sign-off.")

    return "\n".join(lines)


def format_incidents_report(inc_data: dict[str, Any]) -> str:
    """Format active and recent incidents."""
    active = inc_data.get("active")
    recent = inc_data.get("recent") or inc_data.get("incidents") or []

    lines = ["🚨 <b>Incident Registry (AI-SRE)</b>\n"]

    if active:
        inc_id = html.escape(str(active.get("id")))
        rule = html.escape(str(active.get("trigger_rule")))
        svcs = html.escape(", ".join(active.get("affected_services", [])))
        t0 = html.escape(str(active.get("t0", ""))[:19].replace("T", " "))
        ttd = active.get("ttd_seconds", 0.0)
        lines.append(
            f"🔥 <b>ACTIVE INCIDENT: {inc_id}</b>\n"
            f"• <b>Trigger Rule:</b> <code>{rule}</code>\n"
            f"• <b>Impacted:</b> <code>{svcs}</code>\n"
            f"• <b>T0 Anomaly:</b> <code>{t0} UTC</code>\n"
            f"• <b>Detection Lag (TTD):</b> <code>{ttd:.2f}s</code>\n"
        )
    else:
        lines.append("🟢 <b>Status:</b> Baseline normal. Zero active breaches.\n")

    lines.append("📋 <b>Recent Resolved Incidents:</b>")
    if not recent:
        lines.append("<i>No past incidents recorded.</i>")
    else:
        for inc in recent[:6]:
            if active and inc.get("id") == active.get("id"):
                continue
            iid = html.escape(str(inc.get("id")))
            rule = html.escape(str(inc.get("trigger_rule") or inc.get("rule") or "slo_breach"))
            st = html.escape(str(inc.get("status", "resolved")).upper())
            lines.append(f"• <code>{iid}</code> [{st}] - {rule}")

    return "\n".join(lines)


def format_postmortem_summary(report_dict: dict[str, Any]) -> str:
    """Format markdown postmortem into a safe, concise Telegram message (<4096 chars)."""
    inc_id = html.escape(str(report_dict.get("incident_id", "UNKNOWN")))
    markdown_text = str(report_dict.get("markdown", ""))

    if not markdown_text:
        return f"ℹ️ No postmortem text found for <code>{inc_id}</code>."

    # Telegram limit is 4096 characters; summarize or truncate cleanly
    clean_lines = []
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            clean_lines.append(f"📄 <b>{html.escape(line[2:])}</b>\n")
        elif line.startswith("## "):
            clean_lines.append(f"\n<b>{html.escape(line[3:])}</b>")
        elif line.startswith("- ") or line.startswith("* "):
            clean_lines.append(f"• {html.escape(line[2:])}")
        elif line.startswith("**"):
            clean_lines.append(html.escape(line))
        elif line.strip() and not line.startswith("|") and not line.startswith("---"):
            clean_lines.append(html.escape(line))

    formatted = "\n".join(clean_lines)
    if len(formatted) > 3800:
        formatted = formatted[:3750] + "\n\n<i>... [Report truncated. View full markdown on web dashboard]</i>"

    return formatted


# =============================================================================
# Bot Implementation
# =============================================================================

class SRETelegramBot:
    """Telegram Incident & Approval Bot."""

    def __init__(
        self,
        token: str,
        chat_id: str | int | None = None,
        api_url: str = DEFAULT_API_URL,
        poll_interval: float = 2.0,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id).strip() if chat_id else None
        self.api = SREApiClient(base_url=api_url)
        self.poll_interval = poll_interval

        # Deduplication caches
        self.notified_approvals: set[str] = set()
        self.notified_incidents: set[str] = set()

        # Build application
        self.app: Application = ApplicationBuilder().token(self.token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register Telegram slash commands and button callback query handlers."""
        self.app.add_handler(CommandHandler(["start", "help"], self.cmd_help))
        self.app.add_handler(CommandHandler(["status", "health"], self.cmd_status))
        self.app.add_handler(CommandHandler("incidents", self.cmd_incidents))
        self.app.add_handler(CommandHandler("approvals", self.cmd_approvals))
        self.app.add_handler(CommandHandler("postmortem", self.cmd_postmortem))
        self.app.add_handler(CommandHandler("restart", self.cmd_restart))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback_query))

    # -------------------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------------------

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start and /help command."""
        text = (
            "🤖 <b>AI-SRE Telegram Incident & Approval Bot</b>\n\n"
            "This bot alerts on-call engineers of autonomous microservice incidents and "
            "gated Tier-2 ('Ask-First') approval requests.\n\n"
            "<b>Available Commands:</b>\n"
            "• /status - Microservices health, latencies, and error rates\n"
            "• /incidents - Active and recent detected incidents\n"
            "• /approvals - Inspect pending fix approvals awaiting human sign-off\n"
            "• /postmortem &lt;ID&gt; - View verified incident postmortem report\n"
            "• /restart &lt;service&gt; - Restart a microservice (e.g. <code>/restart payments</code>)\n"
            "• /help - Display this manual\n\n"
            f"🔗 <b>Backend Target:</b> <code>{html.escape(self.api.base_url)}</code>"
        )
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        try:
            data = await self.api.get_status()
            text = format_status_report(data)
        except Exception as exc:
            text = f"❌ <b>Error querying AI-SRE status:</b>\n<code>{html.escape(str(exc))}</code>"

        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_incidents(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /incidents command."""
        try:
            data = await self.api.get_incidents()
            text = format_incidents_report(data)
        except Exception as exc:
            text = f"❌ <b>Error querying incidents:</b>\n<code>{html.escape(str(exc))}</code>"

        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /approvals command."""
        try:
            data = await self.api.get_approvals()
            pending = data.get("pending") or []
            if not pending:
                text = "✅ <b>No Pending Approvals:</b> Zero actions requiring operator sign-off."
                if update.effective_message:
                    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
                return

            for a in pending:
                text = format_approval_alert(a)
                appr_id = a.get("id")
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve & Apply", callback_data=f"approve:{appr_id}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"deny:{appr_id}"),
                    ]
                ])
                if update.effective_message:
                    await update.effective_message.reply_text(
                        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
                    )
        except Exception as exc:
            text = f"❌ <b>Error querying approvals:</b>\n<code>{html.escape(str(exc))}</code>"
            if update.effective_message:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_postmortem(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /postmortem <incident_id> command."""
        if not context.args:
            text = "⚠️ <b>Usage:</b> <code>/postmortem &lt;INC-ID&gt;</code> (e.g. <code>/postmortem INC-1</code>)"
            if update.effective_message:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        inc_id = context.args[0].strip().upper()
        try:
            data = await self.api.get_report(inc_id)
            text = format_postmortem_summary(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                text = f"❌ Postmortem report for <code>{inc_id}</code> not found in <code>reports/</code>."
            else:
                text = f"❌ Error fetching report: <code>{html.escape(str(exc))}</code>"
        except Exception as exc:
            text = f"❌ Error: <code>{html.escape(str(exc))}</code>"

        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /restart <service> command."""
        if not context.args:
            text = "⚠️ <b>Usage:</b> <code>/restart &lt;service&gt;</code> (options: gateway, orders, payments, inventory)"
            if update.effective_message:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        service = context.args[0].strip().lower()
        try:
            res = await self.api.restart_service(service)
            text = f"🔄 <b>Service Restart Triggered:</b>\n<code>{html.escape(json.dumps(res, indent=2))}</code>"
        except Exception as exc:
            text = f"❌ Failed to restart service <code>{service}</code>:\n<code>{html.escape(str(exc))}</code>"

        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    # -------------------------------------------------------------------------
    # Inline Keyboard Callback Query Handler
    # -------------------------------------------------------------------------

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process operator clicks on [ ✅ Approve ] and [ ❌ Deny ] buttons."""
        query = update.callback_query
        if not query or not query.data:
            return

        user = query.from_user
        username = f"@{user.username}" if user.username else f"{user.first_name} ({user.id})"
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        action, sep, appr_id = query.data.partition(":")
        if not sep or not appr_id:
            await query.answer("Invalid action payload.", show_alert=True)
            return

        await query.answer("Processing decision with AI-SRE server...")

        if action == "approve":
            try:
                res = await self.api.approve_action(
                    approval_id=appr_id,
                    decided_by=username,
                    reason=f"Approved by {username} via Telegram",
                )
                appr_data = res.get("approval") or {}
                result_data = res.get("result") or {}
                act_name = appr_data.get("action", "action")
                svc_name = appr_data.get("service") or (appr_data.get("args") or {}).get("service", "target")
                status_res = result_data.get("status", "applied")

                updated_text = (
                    f"✅ <b>APPROVED & EXECUTED</b>\n\n"
                    f"• <b>Approval ID:</b> <code>{html.escape(appr_id)}</code>\n"
                    f"• <b>Action:</b> <code>{html.escape(act_name)}</code> on <code>{html.escape(svc_name)}</code>\n"
                    f"• <b>Decided by:</b> {html.escape(username)}\n"
                    f"• <b>Decision Time:</b> <code>{now_str} UTC</code>\n"
                    f"• <b>Execution Status:</b> <code>{html.escape(status_res)}</code>\n\n"
                    f"<i>Remediation verified and recorded in supervisor audit log.</i>"
                )
                await query.edit_message_text(
                    text=updated_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,  # removes buttons in-place
                )
            except Exception as exc:
                await query.edit_message_text(
                    text=f"❌ <b>Failed to execute approval {appr_id}:</b>\n<code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )

        elif action == "deny":
            try:
                res = await self.api.deny_action(
                    approval_id=appr_id,
                    decided_by=username,
                    reason=f"Denied by {username} via Telegram",
                )
                appr_data = res.get("approval") or {}
                act_name = appr_data.get("action", "action")
                svc_name = appr_data.get("service") or (appr_data.get("args") or {}).get("service", "target")

                updated_text = (
                    f"❌ <b>DENIED BY OPERATOR</b>\n\n"
                    f"• <b>Approval ID:</b> <code>{html.escape(appr_id)}</code>\n"
                    f"• <b>Action:</b> <code>{html.escape(act_name)}</code> on <code>{html.escape(svc_name)}</code>\n"
                    f"• <b>Decided by:</b> {html.escape(username)}\n"
                    f"• <b>Decision Time:</b> <code>{now_str} UTC</code>\n\n"
                    f"<i>Proposed fix canceled. Gated action was not executed.</i>"
                )
                await query.edit_message_text(
                    text=updated_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,  # removes buttons in-place
                )
            except Exception as exc:
                await query.edit_message_text(
                    text=f"❌ <b>Failed to record denial for {appr_id}:</b>\n<code>{html.escape(str(exc))}</code>",
                    parse_mode=ParseMode.HTML,
                )

    # -------------------------------------------------------------------------
    # Background Proactive Polling Loop
    # -------------------------------------------------------------------------

    async def poll_backend_worker(self) -> None:
        """Poll AI-SRE server and push proactive alerts to the on-call chat."""
        if not self.chat_id:
            logger.info("No TELEGRAM_CHAT_ID configured; background alert pusher disabled.")
            return

        logger.info(
            "Starting background alert pusher targeting chat_id=%s (interval=%.1fs)",
            self.chat_id,
            self.poll_interval,
        )

        while True:
            try:
                # 1. Check pending approvals
                approvals_resp = await self.api.get_approvals()
                pending = approvals_resp.get("pending") or []
                for a in pending:
                    appr_id = a.get("id")
                    if appr_id and appr_id not in self.notified_approvals:
                        self.notified_approvals.add(appr_id)
                        text = format_approval_alert(a)
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Approve & Apply", callback_data=f"approve:{appr_id}"),
                                InlineKeyboardButton("❌ Deny", callback_data=f"deny:{appr_id}"),
                            ]
                        ])
                        await self.app.bot.send_message(
                            chat_id=self.chat_id,
                            text=text,
                            reply_markup=keyboard,
                            parse_mode=ParseMode.HTML,
                        )
                        logger.info("Sent Telegram approval notification for %s", appr_id)

                # 2. Check active incidents
                inc_resp = await self.api.get_incidents()
                active = inc_resp.get("active")
                if active:
                    inc_id = active.get("id")
                    if inc_id and inc_id not in self.notified_incidents:
                        self.notified_incidents.add(inc_id)
                        rule = html.escape(str(active.get("trigger_rule", "unknown")))
                        svcs = html.escape(", ".join(active.get("affected_services", [])))
                        ttd = active.get("ttd_seconds", 0.0)
                        msg = (
                            f"🔥 <b>AI-SRE INCIDENT OPENED: {html.escape(inc_id)}</b>\n\n"
                            f"• <b>Trigger Rule:</b> <code>{rule}</code>\n"
                            f"• <b>Affected Services:</b> <code>{svcs}</code>\n"
                            f"• <b>Time-to-Detect (TTD):</b> <code>{ttd:.2f}s</code>\n\n"
                            f"<i>Autonomous LLM agent investigating causal telemetry...</i>"
                        )
                        await self.app.bot.send_message(
                            chat_id=self.chat_id,
                            text=msg,
                            parse_mode=ParseMode.HTML,
                        )
                        logger.info("Sent Telegram incident notification for %s", inc_id)

            except Exception as exc:
                logger.debug("Polling iteration error (will retry): %s", exc)

            await asyncio.sleep(self.poll_interval)

    # -------------------------------------------------------------------------
    # Runner Lifecycle
    # -------------------------------------------------------------------------

    async def run_async(self) -> None:
        """Start both polling bot and background alert task."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram SRE bot started successfully.")
        poll_task = asyncio.create_task(self.poll_backend_worker())

        try:
            while True:
                await asyncio.sleep(1.0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            poll_task.cancel()
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                pass

    def run(self) -> None:
        """Sync entrypoint running asyncio loop."""
        try:
            asyncio.run(self.run_async())
        except (KeyboardInterrupt, SystemExit):
            pass


# =============================================================================
# CLI Main
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="AI-SRE Telegram Incident & Approval Bot")
    parser.add_argument(
        "--token",
        default=os.getenv("TELEGRAM_BOT_TOKEN"),
        help="Telegram Bot Token (or set TELEGRAM_BOT_TOKEN env var)",
    )
    parser.add_argument(
        "--chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID"),
        help="Target On-Call Chat/Channel ID for push notifications",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("AI_SRE_URL", DEFAULT_API_URL),
        help=f"AI-SRE Server Base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("POLL_INTERVAL", "2.0")),
        help="Polling interval in seconds (default: 2.0)",
    )

    args = parser.parse_args(argv)

    if not args.token:
        print(
            "ERROR: Telegram Bot Token is required.\n"
            "Provide it via --token <TOKEN> or set TELEGRAM_BOT_TOKEN in .env\n"
            "Get one by chatting with @BotFather on Telegram.",
            file=sys.stderr,
        )
        return 1

    bot = SRETelegramBot(
        token=args.token,
        chat_id=args.chat_id,
        api_url=args.api_url,
        poll_interval=args.poll_interval,
    )

    try:
        print(f"Starting Telegram Bot connected to AI-SRE at {args.api_url}...")
        bot.run()
        return 0
    except KeyboardInterrupt:
        print("\nStopping Telegram bot...")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
