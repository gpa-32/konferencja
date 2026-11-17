"""Powiadomienia e-mail (SMTP). Bez konfiguracji SMTP – tylko log."""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("notify")


def send_email(to: str | None, subject: str, body: str) -> None:
    if not to:
        return
    host = os.environ.get("SMTP_HOST", "")
    if not host:
        log.info("E-mail (SMTP nieskonfigurowany) do %s: %s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "sale@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if os.environ.get("SMTP_STARTTLS", "1") == "1":
                s.starttls()
            user = os.environ.get("SMTP_USER")
            if user:
                s.login(user, os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        log.warning("Nie udało się wysłać e-maila do %s: %s", to, exc)
