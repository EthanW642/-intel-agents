from pipeline.analyze import _build_user_prompt, _estimate_cost, _extract_json_block, compute_effort, _current_rates

CFG = {
    "effort_low_max_load": 8,
    "effort_medium_max_load": 16,
    "xhigh_min_triaged_items": 20,
    "xhigh_min_active_theses": 1,
}


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
