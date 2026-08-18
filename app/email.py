"""Outbound email: verification links, password-reset links.

Uses the stdlib smtplib synchronously — fine for this app's volume (a
handful of emails per registration/reset, not a bulk-mail sender), and
avoids adding a new dependency for something this small. Every call goes
through `send_email`, which never raises: a missing or broken SMTP config
logs and returns False instead of taking down the request that triggered
it (registration, forgot-password) — the caller decides how to react to a
failed send (currently: nothing user-visible, since revealing "email
failed" for a specific address would leak whether that address has an
account).
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger("app.email")


def send_email(to_email: str, subject: str, *, html_body: str, text_body: str) -> bool:
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_username and settings.smtp_password):
        logger.warning("SMTP not configured (SMTP_HOST/USERNAME/PASSWORD) — skipping email to %s: %r", to_email, subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email or settings.smtp_username
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(msg["From"], [to_email], msg.as_string())
        return True
    except Exception:  # noqa: BLE001 — never let a mail-server hiccup crash the request that triggered it
        logger.exception("Failed to send email to %s: %r", to_email, subject)
        return False
