"""Slack delivery via per-client incoming webhooks (Block Kit messages)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from db.models import Client

logger = logging.getLogger("lead-intel.delivery.slack")

_SEND_SLEEP = 0.3


def _score_badge(score: int) -> str:
    """Return an emoji + label badge for a score band."""
    if score >= 85:
        return "🔴 HIGH"
    if score >= 60:
        return "🟡 MED"
    return "⚪ LOW"


def _build_blocks(lead: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a Slack Block Kit payload describing one lead."""
    score = int(lead.get("score", 0))
    company = lead.get("company_name") or lead.get("vertical", "Unknown")
    contact_name = lead.get("contact_name") or "—"
    contact_role = lead.get("contact_role") or "—"
    email = lead.get("email") or "—"
    linkedin = lead.get("linkedin_url")
    sources = ", ".join(lead.get("signal_sources") or []) or "—"
    detected = lead.get("detected_at") or lead.get("created_at") or "—"
    source_url = lead.get("source_url") or lead.get("primary_source_url")

    breakdown = lead.get("score_breakdown") or {}
    breakdown_text = (
        "\n".join(f"• {k.replace('_', ' ')}: +{v}" for k, v in breakdown.items())
        or "—"
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{_score_badge(score)} {score}/100 — {company}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Contact:*\n{contact_name}"},
                {"type": "mrkdwn", "text": f"*Role:*\n{contact_role}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{email}"},
                {
                    "type": "mrkdwn",
                    "text": f"*LinkedIn:*\n<{linkedin}|profile>" if linkedin else "*LinkedIn:*\n—",
                },
            ],
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Source(s):*\n{sources}"},
                {"type": "mrkdwn", "text": f"*Detected:*\n{detected}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Why this score:*\n{breakdown_text}"},
        },
    ]

    if source_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View original post"},
                        "url": source_url,
                    }
                ],
            }
        )
    return blocks


def send_lead(lead: dict[str, Any], client: Client, webhook_url: str) -> bool:
    """POST a single lead to a client's Slack webhook if it meets the threshold.

    Returns True if a message was sent.
    """
    if int(lead.get("score", 0)) < client.min_score:
        return False
    if not webhook_url:
        return False
    try:
        with httpx.Client(timeout=15.0) as http:
            resp = http.post(webhook_url, json={"blocks": _build_blocks(lead)})
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Slack send failed for %s: %s", client.name, exc)
        return False


def send_batch(leads: list[dict[str, Any]], client: Client) -> int:
    """Send a batch of leads to a client's Slack channel, throttled.

    Returns the number of messages sent.
    """
    if not client.slack_webhook:
        return 0
    sent = 0
    for lead in leads:
        if send_lead(lead, client, client.slack_webhook):
            sent += 1
            time.sleep(_SEND_SLEEP)
    logger.info("Slack: sent %d/%d leads to %s", sent, len(leads), client.name)
    return sent
