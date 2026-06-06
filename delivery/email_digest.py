"""Daily top-10 lead digest delivered via SendGrid transactional email."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Content, Email, Mail, To

from config import settings
from db.models import Client

logger = logging.getLogger("lead-intel.delivery.email_digest")


def _lead_row_html(lead: dict[str, Any]) -> str:
    """Render a single lead as an HTML table row."""
    score = int(lead.get("score", 0))
    color = "#d7263d" if score >= 85 else ("#f4a300" if score >= 60 else "#888")
    company = lead.get("company_name") or "—"
    contact = lead.get("contact_name") or "—"
    role = lead.get("contact_role") or "—"
    email = lead.get("email") or "—"
    sources = ", ".join(lead.get("signal_sources") or []) or "—"
    return f"""
    <tr>
      <td style="padding:8px;border-bottom:1px solid #eee;">
        <span style="color:{color};font-weight:bold;">{score}</span>
      </td>
      <td style="padding:8px;border-bottom:1px solid #eee;"><b>{company}</b></td>
      <td style="padding:8px;border-bottom:1px solid #eee;">{contact} ({role})</td>
      <td style="padding:8px;border-bottom:1px solid #eee;">{email}</td>
      <td style="padding:8px;border-bottom:1px solid #eee;">{sources}</td>
    </tr>"""


def _build_html(leads: list[dict[str, Any]]) -> str:
    """Build the full HTML body for the digest from the top leads."""
    rows = "".join(_lead_row_html(lead) for lead in leads)
    return f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
      <h2>🎯 Your top {len(leads)} leads today</h2>
      <p style="color:#666;">{date.today().isoformat()}</p>
      <table style="border-collapse:collapse;width:100%;">
        <thead>
          <tr style="text-align:left;background:#fafafa;">
            <th style="padding:8px;">Score</th>
            <th style="padding:8px;">Company</th>
            <th style="padding:8px;">Contact</th>
            <th style="padding:8px;">Email</th>
            <th style="padding:8px;">Source</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </body></html>"""


def send_daily_digest(leads: list[dict[str, Any]], client: Client) -> bool:
    """Email a client the top 10 leads from the last 24 hours.

    Returns True if the email was accepted by SendGrid.
    """
    if not client.email_to:
        return False
    if not settings.SENDGRID_API_KEY:
        logger.warning("SENDGRID_API_KEY not set; skipping digest for %s", client.name)
        return False
    if not leads:
        logger.info("No leads to digest for %s today", client.name)
        return False

    top = sorted(leads, key=lambda lead: int(lead.get("score", 0)), reverse=True)[:10]
    subject = f"🎯 Your top {len(top)} leads today — {date.today().isoformat()}"
    message = Mail(
        from_email=Email(settings.SENDGRID_FROM_EMAIL),
        to_emails=To(client.email_to),
        subject=subject,
        html_content=Content("text/html", _build_html(top)),
    )
    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        resp = sg.send(message)
        ok = 200 <= resp.status_code < 300
        logger.info("Digest to %s: status %s", client.email_to, resp.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.error("SendGrid digest failed for %s: %s", client.name, exc)
        return False
