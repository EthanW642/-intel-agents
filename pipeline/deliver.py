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


def _send_with_retry(gmail_address: str, gmail_app_password: str, msg: MIMEMultipart, description: str) -> dict:
    """Shared retry-once-then-log wrapper for both the daily briefing and
    the Ollama-outage alert — same delivery guarantees, different message
    bodies. Returns {"sent": bool, "retried": bool, "error": str | None}."""
    try:
        _send_once(gmail_address, gmail_app_password, msg)
        logger.info("%s sent to %s (subject: %s)", description, gmail_address, msg["Subject"])
        return {"sent": True, "retried": False, "error": None}
    except Exception as first_error:
        logger.warning(
            "%s send failed, retrying once in %ds: %s", description, RETRY_DELAY_SECONDS, first_error
        )
        time.sleep(RETRY_DELAY_SECONDS)
        try:
            _send_once(gmail_address, gmail_app_password, msg)
            logger.info("%s sent on retry to %s (subject: %s)", description, gmail_address, msg["Subject"])
            return {"sent": True, "retried": True, "error": None}
        except Exception as second_error:
            logger.exception("%s failed on retry", description)
            return {"sent": False, "retried": True, "error": str(second_error)}


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
    return _send_with_retry(gmail_address, gmail_app_password, msg, "Briefing email")


def _build_outage_alert_message(gmail_address: str, subject: str, error_detail: str, run_date: date) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    body = (
        f"The Middle East intel pipeline aborted its {run_date.isoformat()} run before reaching "
        "the analysis (Sonnet) call because local triage infrastructure was completely "
        "unreachable — not a single triage or prediction-resolution batch could contact "
        "Ollama.\n\n"
        "No briefing was generated today. This is a genuine outage, not a quiet news day — "
        "don't read a missing/empty briefing as \"nothing happened.\"\n\n"
        f"Detail: {error_detail}\n\n"
        "Check that `ollama serve` is running and the configured model is pulled, then "
        "re-run the pipeline manually to get today's briefing."
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def send_ollama_outage_alert(
    error_detail: str,
    gmail_address: str,
    gmail_app_password: str,
    run_date: date | None = None,
) -> dict:
    """Sent instead of the briefing when every triage/prediction-resolution
    batch failed to reach Ollama this run (see
    pipeline.ollama_client.OllamaUnavailableError) — a plain-text alert, no
    Sonnet call involved, so an outage is never silently indistinguishable
    from a genuinely quiet day. Same retry-once-then-log contract as
    send_briefing_email."""
    run_date = run_date or date.today()
    subject = f"[Middle East Intel] ALERT: triage unreachable — {run_date:%B} {run_date.day}, {run_date:%Y}"
    msg = _build_outage_alert_message(gmail_address, subject, error_detail, run_date)
    return _send_with_retry(gmail_address, gmail_app_password, msg, "Ollama-outage alert email")
