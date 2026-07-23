"""Stage 5 (part 3) — email delivery (spec section 6). Sent via Gmail SMTP
using an App Password (never hardcoded — read from env). Self-send: the
same Gmail account both authenticates and receives. The local Markdown
file is always written by pipeline/render.py regardless of what happens
here — this module runs strictly after that, and its failure never loses
the briefing.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from pipeline.render import markdown_to_html

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
RETRY_DELAY_SECONDS = 30


def _subject_for(run_date: date) -> str:
    # Static format, not derived from content, so it stays parseable/
    # searchable (spec section 6): "[Middle East Intel] <Month Day, Year>".
    # %-d/%e aren't portable across platforms, so build it by hand.
    return f"[Middle East Intel] {run_date:%B} {run_date.day}, {run_date:%Y}"


def _build_message(
    gmail_address: str, subject: str, markdown_text: str, briefing_path: Path
) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(markdown_text, "plain", "utf-8"))
    alt.attach(MIMEText(markdown_to_html(markdown_text), "html", "utf-8"))
    msg.attach(alt)

    attachment = MIMEApplication(briefing_path.read_bytes(), Name=briefing_path.name)
    attachment["Content-Disposition"] = f'attachment; filename="{briefing_path.name}"'
    msg.attach(attachment)

    return msg


def _send_once(gmail_address: str, gmail_app_password: str, msg: MIMEMultipart) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [gmail_address], msg.as_string())


def send_briefing_email(
    markdown_text: str,
    briefing_path: Path,
    gmail_address: str,
    gmail_app_password: str,
    run_date: date | None = None,
) -> dict:
    """Send the briefing. Retries once after a short delay on failure; the
    caller is expected to log the outcome (spec: failed sends go in the run
    CSV) — this function just reports what happened, it doesn't write logs
    itself, so run.py can fold it into one unified run-log row.

    Returns {"sent": bool, "retried": bool, "error": str | None}.
    """
    run_date = run_date or date.today()
    subject = _subject_for(run_date)
    msg = _build_message(gmail_address, subject, markdown_text, briefing_path)

    try:
        _send_once(gmail_address, gmail_app_password, msg)
        logger.info("Briefing email sent to %s (subject: %s)", gmail_address, subject)
        return {"sent": True, "retried": False, "error": None}
    except Exception as first_error:
        logger.warning(
            "Briefing email send failed, retrying once in %ds: %s", RETRY_DELAY_SECONDS, first_error
        )
        time.sleep(RETRY_DELAY_SECONDS)
        try:
            _send_once(gmail_address, gmail_app_password, msg)
            logger.info("Briefing email sent on retry to %s (subject: %s)", gmail_address, subject)
            return {"sent": True, "retried": True, "error": None}
        except Exception as second_error:
            logger.exception("Briefing email failed on retry — briefing file is still saved at %s", briefing_path)
            return {"sent": False, "retried": True, "error": str(second_error)}
