"""Orchestration-level regression test for agents/middle_east/run.py.

Covers the real bug found in live testing (2026-07-25): every triage batch
failed to reach Ollama, and the pipeline silently proceeded all the way to
a Sonnet call producing a "0 items survived" briefing — indistinguishable
from a genuinely quiet news day. run.py must instead abort before the
Sonnet call and send a distinct outage alert. Every pipeline stage other
than triage is mocked so only run.py's own branching is under test.
"""
from unittest.mock import MagicMock, patch

import pytest

from pipeline.ollama_client import OllamaUnavailableError


@pytest.fixture
def mocked_run_deps(monkeypatch):
    import agents.middle_east.run as run_module

    monkeypatch.setattr(run_module, "init_store", lambda *a, **k: (MagicMock(), MagicMock()))
    monkeypatch.setattr(run_module, "run_ingest", lambda: ["item"])
    monkeypatch.setattr(run_module, "dedup_items", lambda items, **k: items)
    monkeypatch.setattr(run_module.db, "get_entities", lambda *a, **k: [])
    monkeypatch.setattr(run_module.db, "get_active_theses", lambda *a, **k: [])
    monkeypatch.setattr(run_module.db, "get_pending_predictions", lambda *a, **k: [])
    monkeypatch.setattr(run_module, "resolve_predictions", lambda *a, **k: [])
    monkeypatch.setattr(run_module, "write_run_log_row", MagicMock())
    return run_module


def test_run_aborts_before_sonnet_and_sends_alert_when_ollama_unreachable(mocked_run_deps, monkeypatch):
    run_module = mocked_run_deps
    monkeypatch.setattr(
        run_module,
        "triage_items",
        MagicMock(side_effect=OllamaUnavailableError("All 1 triage batches failed to reach Ollama")),
    )
    run_analysis_mock = MagicMock()
    monkeypatch.setattr(run_module, "run_analysis", run_analysis_mock)
    send_briefing_mock = MagicMock()
    monkeypatch.setattr(run_module, "send_briefing_email", send_briefing_mock)
    send_alert_mock = MagicMock(return_value={"sent": True, "retried": False, "error": None})
    monkeypatch.setattr(run_module, "send_ollama_outage_alert", send_alert_mock)
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")

    result = run_module.run()

    assert result is None
    run_analysis_mock.assert_not_called()  # the entire point: no Sonnet spend on a dead run
    send_briefing_mock.assert_not_called()
    send_alert_mock.assert_called_once()
    logged_row = run_module.write_run_log_row.call_args[0][0]
    assert logged_row["ollama_outage"] is True
    assert logged_row["email_sent"] is True


def test_run_skips_alert_email_gracefully_when_no_gmail_credentials(mocked_run_deps, monkeypatch):
    run_module = mocked_run_deps
    monkeypatch.setattr(
        run_module, "triage_items", MagicMock(side_effect=OllamaUnavailableError("outage"))
    )
    run_analysis_mock = MagicMock()
    monkeypatch.setattr(run_module, "run_analysis", run_analysis_mock)
    send_alert_mock = MagicMock()
    monkeypatch.setattr(run_module, "send_ollama_outage_alert", send_alert_mock)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    result = run_module.run()

    assert result is None
    run_analysis_mock.assert_not_called()
    send_alert_mock.assert_not_called()
    logged_row = run_module.write_run_log_row.call_args[0][0]
    assert logged_row["ollama_outage"] is True
    assert logged_row["email_sent"] is False
