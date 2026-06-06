"""Telegram delivery via python-telegram-bot (async Bot API).

Functions are async because python-telegram-bot v21 is fully async; the pipeline
awaits them. A synchronous convenience wrapper is provided for non-async callers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from telegram import Bot
from telegram.constants import ParseMode

from config import settings
from db.models import Client

logger = logging.getLogger("lead-intel.delivery.telegram")

_SEND_SLEEP = 0.3


def _score_emoji(score: int) -> str:
    """Return a colour emoji for a score band."""
    if score >= 85:
        return "🔴"
    if score >= 60:
        return "🟡"
    return "⚪"


def _escape(text: Any) -> str:
    """Escape Markdown-sensitive characters in a value for Telegram Markdown."""
    if text is None:
        return "—"
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, f"\\{ch}")
    return out


def _build_message(lead: dict[str, Any]) -> str:
    """Render a lead as a Markdown message string."""
    score = int(lead.get("score", 0))
    company = _escape(lead.get("company_name"))
    contact = _escape(lead.get("contact_name"))
    role = _escape(lead.get("contact_role"))
    email = _escape(lead.get("email"))
    sources = _escape(", ".join(lead.get("signal_sources") or []) or "—")
    signal_summary = _escape(lead.get("signal_summary") or "—")
    return (
        f"*Company:* {company}\n"
        f"*Score:* {score}/100 {_score_emoji(score)}\n"
        f"*Contact:* {contact} ({role})\n"
        f"*Email:* {email}\n"
        f"*Signal:* {signal_summary}\n"
        f"*Source:* {sources}"
    )


async def send_lead(lead: dict[str, Any], chat_id: str, bot_token: str) -> bool:
    """Send a single lead to a Telegram chat. Returns True on success."""
    if not bot_token or not chat_id:
        return False
    try:
        bot = Bot(token=bot_token)
        await bot.send_message(
            chat_id=chat_id,
            text=_build_message(lead),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram send failed for chat %s: %s", chat_id, exc)
        return False


async def send_batch(
    leads: list[dict[str, Any]], client: Client, bot_token: Optional[str] = None
) -> int:
    """Send a batch of leads to a client's Telegram chat, throttled.

    Returns the number of messages sent.
    """
    chat_id = client.telegram_chat_id
    token = bot_token or settings.TELEGRAM_BOT_TOKEN
    if not chat_id or not token:
        return 0
    sent = 0
    for lead in leads:
        if int(lead.get("score", 0)) < client.min_score:
            continue
        if await send_lead(lead, chat_id, token):
            sent += 1
            await asyncio.sleep(_SEND_SLEEP)
    logger.info("Telegram: sent %d/%d leads to %s", sent, len(leads), client.name)
    return sent


def send_batch_sync(leads: list[dict[str, Any]], client: Client) -> int:
    """Synchronous wrapper around :func:`send_batch` for non-async callers."""
    return asyncio.run(send_batch(leads, client))
