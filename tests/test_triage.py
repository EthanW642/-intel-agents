import json
from unittest.mock import patch

from agents.middle_east.sources import RawItem
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
