from datetime import date
from pathlib import Path
from unittest.mock import patch

from pipeline.deliver import _build_message, _subject_for, send_briefing_email, send_ollama_outage_alert


def test_subject_format_static_and_parseable():
    assert _subject_for(date(2026, 7, 23)) == "[Middle East Intel] July 23, 2026"
    assert _subject_for(date(2026, 1, 5)) == "[Middle East Intel] January 5, 2026"


def test_build_message_has_plaintext_html_and_attachment(tmp_path):
    briefing = tmp_path / "middle_east_2026-07-23.md"
    briefing.write_text("# Briefing\n\nSomething happened.")

    msg = _build_message("me@gmail.com", "[Middle East Intel] July 23, 2026", "# Briefing\n\nSomething happened.", briefing)

    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "me@gmail.com"
    assert msg["Subject"] == "[Middle East Intel] July 23, 2026"

    payload_types = set()
    attachment_names = []
    for part in msg.walk():
        payload_types.add(part.get_content_type())
        if part.get_filename():
            attachment_names.append(part.get_filename())

    assert "text/plain" in payload_types
    assert "text/html" in payload_types
    assert briefing.name in attachment_names


def test_send_briefing_email_succeeds_first_try(tmp_path):
    briefing = tmp_path / "b.md"
    briefing.write_text("content")

    with patch("pipeline.deliver._send_once") as mock_send:
        result = send_briefing_email("content", briefing, "me@gmail.com", "app-password", date(2026, 7, 23))

    assert result == {"sent": True, "retried": False, "error": None}
    assert mock_send.call_count == 1


def test_send_briefing_email_retries_once_then_succeeds(tmp_path):
    briefing = tmp_path / "b.md"
    briefing.write_text("content")

    with patch("pipeline.deliver._send_once", side_effect=[Exception("smtp down"), None]), patch(
        "pipeline.deliver.time.sleep"
    ) as mock_sleep:
        result = send_briefing_email("content", briefing, "me@gmail.com", "app-password", date(2026, 7, 23))

    assert result == {"sent": True, "retried": True, "error": None}
    mock_sleep.assert_called_once()


def test_send_briefing_email_fails_after_retry_but_does_not_raise(tmp_path):
    briefing = tmp_path / "b.md"
    briefing.write_text("content")

    with patch("pipeline.deliver._send_once", side_effect=Exception("smtp down")), patch("pipeline.deliver.time.sleep"):
        result = send_briefing_email("content", briefing, "me@gmail.com", "app-password", date(2026, 7, 23))

    assert result["sent"] is False
    assert result["retried"] is True
    assert "smtp down" in result["error"]


def test_ollama_outage_alert_subject_and_body_distinct_from_briefing():
    with patch("pipeline.deliver._send_once") as mock_send:
        result = send_ollama_outage_alert(
            "All 19 triage batches failed to reach Ollama at http://localhost:11434 (model=qwen2.5:14b)",
            "me@gmail.com",
            "app-password",
            date(2026, 7, 25),
        )

    assert result == {"sent": True, "retried": False, "error": None}
    sent_msg = mock_send.call_args[0][2]
    assert "ALERT" in sent_msg["Subject"]
    assert "July 25, 2026" in sent_msg["Subject"]
    body = sent_msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert "not a quiet news day" in body
    assert "All 19 triage batches failed" in body


def test_ollama_outage_alert_retries_once_then_succeeds():
    with patch("pipeline.deliver._send_once", side_effect=[Exception("smtp down"), None]), patch(
        "pipeline.deliver.time.sleep"
    ) as mock_sleep:
        result = send_ollama_outage_alert("boom", "me@gmail.com", "app-password", date(2026, 7, 25))

    assert result == {"sent": True, "retried": True, "error": None}
    mock_sleep.assert_called_once()
