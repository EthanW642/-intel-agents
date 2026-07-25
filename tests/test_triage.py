import json
from unittest.mock import patch

import httpx
import pytest

from agents.middle_east.sources import RawItem
from pipeline.ollama_client import OllamaUnavailableError
from pipeline.triage import _parse_scores, triage_items


def _item(title: str) -> RawItem:
    return RawItem(title=title, source="test", url="http://x", published="2026-01-01", text=title)


def test_parse_scores_normal():
    raw = json.dumps([{"index": 0, "score": 8, "reason": "matters"}, {"index": 1, "score": 2, "reason": "noise"}])
    scores = _parse_scores(raw, batch_len=2, fallback_score=6)
    assert scores[0]["score"] == 8
    assert scores[1]["score"] == 2


def test_parse_scores_invalid_json_falls_back():
    scores = _parse_scores("not json", batch_len=3, fallback_score=6)
    assert all(scores[i]["score"] == 6 for i in range(3))
    assert all("fallback" in scores[i]["reason"] for i in range(3))


def test_parse_scores_missing_index_gets_fallback():
    raw = json.dumps([{"index": 0, "score": 9, "reason": "x"}])
    scores = _parse_scores(raw, batch_len=2, fallback_score=6)
    assert scores[0]["score"] == 9
    assert scores[1]["score"] == 6  # model omitted this item -> fail-safe fallback


def test_triage_items_filters_by_threshold():
    items = [_item("keep"), _item("drop")]
    raw_response = json.dumps(
        [{"index": 0, "score": 9, "reason": "relevant"}, {"index": 1, "score": 2, "reason": "irrelevant"}]
    )
    with patch("pipeline.triage.call_ollama", return_value=raw_response):
        survivors = triage_items(items, entities=[], theses=[], ollama_host="http://x", model="m", score_threshold=6)

    assert len(survivors) == 1
    assert survivors[0].title == "keep"
    assert survivors[0].raw_metadata["triage_score"] == 9


def test_triage_preserves_tier_metadata():
    item = _item("keep")
    item.raw_metadata["tier"] = 2
    raw_response = json.dumps([{"index": 0, "score": 9, "reason": "relevant"}])
    with patch("pipeline.triage.call_ollama", return_value=raw_response):
        survivors = triage_items([item], entities=[], theses=[], ollama_host="http://x", model="m", score_threshold=6)

    assert survivors[0].raw_metadata["tier"] == 2
    assert survivors[0].raw_metadata["triage_score"] == 9


def test_triage_raises_ollama_unavailable_when_every_batch_fails_to_connect():
    # Regression for a real run (2026-07-25): Ollama wasn't running, all 19
    # triage batches hit httpx.ConnectError, and the pipeline silently
    # proceeded to a Sonnet call reporting "0 items survived triage" —
    # indistinguishable from a genuinely quiet news day. A total connection
    # outage must be surfaced distinctly, not swallowed into "0 survivors."
    items = [_item("a"), _item("b")]
    with patch("pipeline.triage.call_ollama", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(OllamaUnavailableError):
            triage_items(items, entities=[], theses=[], ollama_host="http://x", model="m", score_threshold=6)


def test_triage_does_not_raise_when_only_some_batches_fail_to_connect():
    # A partial outage (some batches fine, some fail) is a normal degraded
    # run, not a total-infrastructure-down situation — must not abort.
    items = [_item(f"item{i}") for i in range(25)]  # 2 batches (BATCH_SIZE=20)
    responses = [
        json.dumps([{"index": i, "score": 9, "reason": "x"} for i in range(20)]),
        httpx.ConnectError("refused"),
    ]

    def fake_call(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("pipeline.triage.call_ollama", side_effect=fake_call):
        survivors = triage_items(items, entities=[], theses=[], ollama_host="http://x", model="m", score_threshold=6)

    assert len(survivors) == 20  # first batch's real survivors, second batch just skipped


def test_triage_does_not_raise_when_items_list_is_empty():
    assert triage_items([], entities=[], theses=[], ollama_host="http://x", model="m", score_threshold=6) == []
