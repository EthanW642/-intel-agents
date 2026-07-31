from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from pipeline import analyze as analyze_module
from pipeline.analyze import _build_user_prompt, _estimate_cost, _extract_json_block, compute_effort, _current_rates, run_analysis

CFG = {
    "effort_low_max_load": 8,
    "effort_medium_max_load": 16,
    "xhigh_min_triaged_items": 20,
    "xhigh_min_active_theses": 1,
}

MEMORY_CONTEXT = {
    "active_theses": [],
    "entity_graph": [],
    "related_past_events": [],
    "track_record_summary": None,
}


def _fake_overloaded_error() -> anthropic.OverloadedError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = httpx.Response(529, request=request, json=body)
    return anthropic.OverloadedError("Overloaded", response=response, body=body)


def _fake_bad_request_error() -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad prompt"}}
    response = httpx.Response(400, request=request, json=body)
    return anthropic.BadRequestError("bad prompt", response=response, body=body)


def _fake_message() -> MagicMock:
    message = MagicMock()
    message.content = [
        MagicMock(
            type="text",
            text='```json\n{"new_entities": [], "new_relationships": [], "new_events": [], '
            '"thesis_updates": [], "new_predictions": []}\n```',
        )
    ]
    message.usage.input_tokens = 100
    message.usage.output_tokens = 50
    return message


class _FakeStreamCtx:
    def __init__(self, message=None, error=None):
        self._message = message
        self._error = error

    def __enter__(self):
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._message


def test_run_analysis_retries_once_after_overloaded_error(monkeypatch, tmp_path):
    monkeypatch.setattr(analyze_module, "COST_LOG_PATH", tmp_path / "api_cost_log.csv")
    message = _fake_message()
    calls = [_FakeStreamCtx(error=_fake_overloaded_error()), _FakeStreamCtx(message=message)]
    sleep_mock = MagicMock()
    fake_client = MagicMock()
    fake_client.messages.stream.side_effect = lambda **kwargs: calls.pop(0)
    monkeypatch.setattr(analyze_module.anthropic, "Anthropic", lambda **kwargs: fake_client)
    monkeypatch.setattr(analyze_module.time, "sleep", sleep_mock)

    result = run_analysis(
        triaged_items=[],
        memory_context=MEMORY_CONTEXT,
        domain="middle_east",
        model="claude-sonnet-5",
        max_tokens=128000,
        pipeline_cfg=CFG,
        api_key="fake-key",
    )

    assert fake_client.messages.stream.call_count == 2
    sleep_mock.assert_called_once_with(analyze_module.ANALYSIS_RETRY_DELAY_SECONDS)
    assert result["write_back"]["new_entities"] == []


def test_run_analysis_does_not_retry_forever_when_second_attempt_also_fails(monkeypatch, tmp_path):
    # Regression guard: the retry is once-and-propagate (matching
    # deliver.py's retry-once pattern), not an unbounded loop -- a second
    # consecutive overload should surface to the caller (run.py's outer
    # `except Exception`), not silently hang or swallow the error.
    monkeypatch.setattr(analyze_module, "COST_LOG_PATH", tmp_path / "api_cost_log.csv")
    calls = [_FakeStreamCtx(error=_fake_overloaded_error()), _FakeStreamCtx(error=_fake_overloaded_error())]
    fake_client = MagicMock()
    fake_client.messages.stream.side_effect = lambda **kwargs: calls.pop(0)
    monkeypatch.setattr(analyze_module.anthropic, "Anthropic", lambda **kwargs: fake_client)
    monkeypatch.setattr(analyze_module.time, "sleep", MagicMock())

    with pytest.raises(anthropic.OverloadedError):
        run_analysis(
            triaged_items=[],
            memory_context=MEMORY_CONTEXT,
            domain="middle_east",
            model="claude-sonnet-5",
            max_tokens=128000,
            pipeline_cfg=CFG,
            api_key="fake-key",
        )

    assert fake_client.messages.stream.call_count == 2


def test_run_analysis_does_not_retry_non_retryable_errors(monkeypatch, tmp_path):
    # A 400 bad-request means the request itself is malformed -- retrying
    # would just reproduce the same failure after a wasted 60s wait.
    monkeypatch.setattr(analyze_module, "COST_LOG_PATH", tmp_path / "api_cost_log.csv")
    calls = [_FakeStreamCtx(error=_fake_bad_request_error())]
    fake_client = MagicMock()
    fake_client.messages.stream.side_effect = lambda **kwargs: calls.pop(0)
    monkeypatch.setattr(analyze_module.anthropic, "Anthropic", lambda **kwargs: fake_client)
    sleep_mock = MagicMock()
    monkeypatch.setattr(analyze_module.time, "sleep", sleep_mock)

    with pytest.raises(anthropic.BadRequestError):
        run_analysis(
            triaged_items=[],
            memory_context=MEMORY_CONTEXT,
            domain="middle_east",
            model="claude-sonnet-5",
            max_tokens=128000,
            pipeline_cfg=CFG,
            api_key="fake-key",
        )

    assert fake_client.messages.stream.call_count == 1
    sleep_mock.assert_not_called()


def test_effort_low_on_quiet_day():
    assert compute_effort(triaged_item_count=3, active_theses_count=0, pipeline_cfg=CFG) == "low"


def test_effort_medium_moderate_load():
    # load = 10 + 2*1 = 12 -> medium
    assert compute_effort(triaged_item_count=10, active_theses_count=1, pipeline_cfg=CFG) == "medium"


def test_effort_caps_at_high_without_escalation_signal():
    # Huge item count alone (no active theses) must NOT reach xhigh/max —
    # this is the "don't let high become a silent low-load default, but
    # also don't let raw item count alone blow past high" guarantee.
    assert compute_effort(triaged_item_count=100, active_theses_count=0, pipeline_cfg=CFG) == "high"


def test_effort_does_not_escalate_on_theses_alone():
    # Plenty of active theses but low item volume should not escalate past
    # the load-based tier.
    result = compute_effort(triaged_item_count=5, active_theses_count=5, pipeline_cfg=CFG)
    assert result in ("low", "medium", "high")
    assert result != "xhigh"


def test_effort_escalates_to_xhigh_when_both_gates_met():
    assert compute_effort(triaged_item_count=20, active_theses_count=1, pipeline_cfg=CFG) == "xhigh"


def test_effort_does_not_escalate_to_xhigh_with_only_one_gate():
    assert compute_effort(triaged_item_count=20, active_theses_count=0, pipeline_cfg=CFG) == "high"
    assert compute_effort(triaged_item_count=5, active_theses_count=1, pipeline_cfg=CFG) != "xhigh"


def test_effort_never_reaches_max():
    # Regression for the real bug (2026-07-26): once a maturing memory
    # store accumulates enough real active/reinforced theses (an expected
    # outcome for a domain watching a persistently active conflict, not
    # an anomaly), an active-theses-count gate meant to reserve "max" for
    # genuinely rare heavy days instead stays permanently cleared, making
    # "max" the routine outcome. "max" is no longer reachable at all --
    # xhigh is the ceiling regardless of how extreme the inputs are.
    result = compute_effort(triaged_item_count=10_000, active_theses_count=10_000, pipeline_cfg=CFG)
    assert result == "xhigh"


def test_current_rates_before_cutoff():
    rates = _current_rates()
    assert rates["input"] in (2.00, 3.00)  # intro or standard, depending on today's date vs. cutoff


def test_estimate_cost_scales_with_tokens():
    cost_small = _estimate_cost(1000, 1000)
    cost_large = _estimate_cost(10000, 10000)
    assert cost_large > cost_small
    assert cost_small > 0


def test_extract_json_block_defaults_new_predictions_when_missing():
    text = 'prose\n```json\n{"new_entities": [], "new_relationships": [], "new_events": [], "thesis_updates": []}\n```\n'
    parsed = _extract_json_block(text)
    assert parsed["new_predictions"] == []


def test_extract_json_block_missing_entirely_returns_empty_shape():
    parsed = _extract_json_block("no json block here")
    assert parsed == {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [],
        "new_predictions": [],
    }


def test_build_user_prompt_flags_cold_start_when_memory_empty():
    prompt = _build_user_prompt([], {"active_theses": [], "entity_graph": [], "related_past_events": [], "track_record_summary": None})
    assert "cold start" in prompt.lower()


def test_build_user_prompt_omits_track_record_when_none():
    prompt = _build_user_prompt([], {"active_theses": [], "entity_graph": [], "related_past_events": [], "track_record_summary": None})
    assert "Prediction track record" not in prompt


def test_build_user_prompt_includes_track_record_when_present():
    ctx = {
        "active_theses": [],
        "entity_graph": [],
        "related_past_events": [],
        "track_record_summary": "6 of last 10 confirmed",
    }
    prompt = _build_user_prompt([], ctx)
    assert "6 of last 10 confirmed" in prompt
