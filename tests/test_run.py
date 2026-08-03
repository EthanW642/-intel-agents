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


class FakeItem:
    """Minimal stand-in for RawItem — run.py touches .url (seen-URL
    filter/mark) and .raw_metadata (analysis cap sort)."""

    def __init__(self, url="http://x/1"):
        self.url = url
        self.raw_metadata = {}


@pytest.fixture
def mocked_run_deps(monkeypatch):
    import agents.middle_east.run as run_module

    monkeypatch.setattr(run_module, "init_store", lambda *a, **k: (MagicMock(), MagicMock()))
    monkeypatch.setattr(run_module, "run_ingest", lambda: [FakeItem()])
    monkeypatch.setattr(run_module, "dedup_exact", lambda items: items)
    monkeypatch.setattr(run_module, "dedup_items", lambda items, **k: items)
    monkeypatch.setattr(run_module.db, "get_entities", lambda *a, **k: [])
    monkeypatch.setattr(run_module.db, "get_active_theses", lambda *a, **k: [])
    monkeypatch.setattr(run_module.db, "get_pending_predictions", lambda *a, **k: [])
    monkeypatch.setattr(run_module.db, "filter_unseen", lambda conn, domain, urls: set(urls))
    monkeypatch.setattr(run_module.db, "mark_seen", MagicMock())
    monkeypatch.setattr(run_module.db, "prune_seen", MagicMock())
    monkeypatch.setattr(run_module, "resolve_predictions", lambda *a, **k: [])
    monkeypatch.setattr(run_module, "unload_model", MagicMock())
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


def test_prediction_resolution_receives_triaged_items_not_deduped(mocked_run_deps, monkeypatch, tmp_path):
    # Regression for the real bug (2026-07-25): resolve_predictions used to
    # get the full ~393-item deduped set, blowing Ollama's context window
    # and producing confident verdicts about news the model never saw.
    # triaged (already relevance-filtered, ~6x smaller) is correct here.
    run_module = mocked_run_deps
    deduped_items = ["deduped-item-1", "deduped-item-2", "deduped-item-3"]
    triaged_items = ["triaged-item-1"]
    monkeypatch.setattr(run_module, "dedup_items", lambda items, **k: deduped_items)
    monkeypatch.setattr(run_module, "triage_items", MagicMock(return_value=triaged_items))
    resolve_predictions_mock = MagicMock(return_value=[])
    monkeypatch.setattr(run_module, "resolve_predictions", resolve_predictions_mock)
    monkeypatch.setattr(run_module, "query_memory", lambda *a, **k: {})
    monkeypatch.setattr(
        run_module,
        "run_analysis",
        lambda *a, **k: {
            "effort": "low",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cost_usd": 0.0,
            "write_back": {},
            "markdown": "# Briefing",
        },
    )
    monkeypatch.setattr(
        run_module,
        "write_back",
        lambda *a, **k: {
            "new_entities": 0,
            "new_relationships": 0,
            "new_events": 0,
            "thesis_updates_applied": 0,
            "thesis_updates_rejected": 0,
            "new_predictions": 0,
        },
    )
    briefing_path = tmp_path / "briefing.md"
    briefing_path.write_text("# Briefing")
    monkeypatch.setattr(run_module, "render_briefing", lambda *a, **k: briefing_path)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    result = run_module.run()

    assert result == briefing_path
    resolve_predictions_mock.assert_called_once()
    passed_items = resolve_predictions_mock.call_args[0][1]
    assert passed_items == triaged_items
    assert passed_items != deduped_items


def test_ollama_model_unloaded_after_prediction_resolution_before_analysis(mocked_run_deps, monkeypatch, tmp_path):
    # Confirmed live 2026-08-02: on a 16GB Mac, leaving the Ollama model
    # loaded through Stage 3-5 (none of which touch Ollama) needlessly
    # held ~9-13GB of memory hostage for the rest of the run. Regression
    # guard that unload_model fires exactly once, after prediction
    # resolution and before the Sonnet call -- not skipped, not duplicated,
    # not fired too early (while triage/predictions might still need it).
    run_module = mocked_run_deps
    call_order: list[str] = []
    monkeypatch.setattr(run_module, "triage_items", MagicMock(return_value=["triaged-item"]))
    monkeypatch.setattr(
        run_module, "resolve_predictions", lambda *a, **k: call_order.append("resolve_predictions") or []
    )
    monkeypatch.setattr(run_module, "unload_model", lambda *a, **k: call_order.append("unload_model"))
    monkeypatch.setattr(run_module, "query_memory", lambda *a, **k: {})
    monkeypatch.setattr(
        run_module,
        "run_analysis",
        lambda *a, **k: call_order.append("run_analysis")
        or {
            "effort": "low",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cost_usd": 0.0,
            "write_back": {},
            "markdown": "# Briefing",
        },
    )
    monkeypatch.setattr(
        run_module,
        "write_back",
        lambda *a, **k: {
            "new_entities": 0,
            "new_relationships": 0,
            "new_events": 0,
            "thesis_updates_applied": 0,
            "thesis_updates_rejected": 0,
            "new_predictions": 0,
        },
    )
    briefing_path = tmp_path / "briefing.md"
    briefing_path.write_text("# Briefing")
    monkeypatch.setattr(run_module, "render_briefing", lambda *a, **k: briefing_path)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    run_module.run()

    assert call_order == ["resolve_predictions", "unload_model", "run_analysis"]


def test_seen_urls_marked_only_on_success_not_on_outage(mocked_run_deps, monkeypatch, tmp_path):
    # The seen-URL store must only record items after a fully successful
    # run: marking them on the Ollama-outage abort path would mean those
    # items are never triaged/analyzed at all — silently lost instead of
    # simply reprocessed by the next run.
    run_module = mocked_run_deps
    monkeypatch.setattr(
        run_module, "triage_items", MagicMock(side_effect=OllamaUnavailableError("outage"))
    )
    monkeypatch.setattr(run_module, "run_analysis", MagicMock())
    monkeypatch.setattr(run_module, "send_ollama_outage_alert", MagicMock())
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    assert run_module.run() is None
    run_module.db.mark_seen.assert_not_called()

    # Success path: marks exactly the new (post-seen-filter) items.
    monkeypatch.setattr(run_module, "triage_items", MagicMock(return_value=[]))
    monkeypatch.setattr(run_module, "query_memory", lambda *a, **k: {})
    monkeypatch.setattr(
        run_module,
        "run_analysis",
        lambda *a, **k: {
            "effort": "low",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cost_usd": 0.0,
            "write_back": {},
            "markdown": "# Briefing",
        },
    )
    monkeypatch.setattr(run_module, "write_back", lambda *a, **k: {})
    briefing_path = tmp_path / "briefing.md"
    briefing_path.write_text("# Briefing")
    monkeypatch.setattr(run_module, "render_briefing", lambda *a, **k: briefing_path)

    assert run_module.run() == briefing_path
    run_module.db.mark_seen.assert_called_once()
    marked_urls = run_module.db.mark_seen.call_args[0][2]
    assert marked_urls == ["http://x/1"]
    run_module.db.prune_seen.assert_called_once()
