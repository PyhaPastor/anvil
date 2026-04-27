"""Notification service — email and webhook dispatch."""
from __future__ import annotations
import logging
from typing import Optional

import httpx
from ..config import settings

logger = logging.getLogger("anvil.notifications")


async def send_email(to: str, subject: str, body: str) -> bool:
    if not settings.notifications.enabled or not settings.notifications.smtp_host:
        return False
    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = settings.notifications.smtp_from
        msg["To"] = to
        await aiosmtplib.send(
            msg,
            hostname=settings.notifications.smtp_host,
            port=settings.notifications.smtp_port,
            username=settings.notifications.smtp_user or None,
            password=settings.notifications.smtp_password or None,
            use_tls=settings.notifications.smtp_tls,
        )
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


async def send_webhook(url: str, payload: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("Webhook dispatch failed: %s", exc)
        return False


async def notify_job_complete(
    job_id: int,
    job_name: str,
    cracked: int,
    total: int,
    webhook_url: Optional[str] = None,
    email_to: Optional[str] = None,
) -> None:
    subject = f"[Anvil] Job completed: {job_name}"
    body = (
        f"Job '{job_name}' (ID: {job_id}) has completed.\n"
        f"Cracked: {cracked}/{total} ({round(cracked/total*100,1) if total else 0}%)\n"
    )
    if email_to:
        await send_email(email_to, subject, body)
    if webhook_url:
        await send_webhook(webhook_url, {
            "event": "job_complete",
            "job_id": job_id,
            "job_name": job_name,
            "cracked": cracked,
            "total": total,
        })
