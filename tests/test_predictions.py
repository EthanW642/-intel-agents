import json
from unittest.mock import patch

from agents.middle_east.sources import RawItem
from pipeline.predictions import _parse_verdicts, resolve_predictions


def _item(title: str, text: str = "") -> RawItem:
    return RawItem(title=title, source="test", url="http://x", published="2026-01-01", text=text or title)


def test_parse_verdicts_normal():
    raw = json.dumps([{"index": 0, "verdict": "confirmed", "reason": "matched"}])
    verdicts = _parse_verdicts(raw, batch_len=1)
    assert verdicts[0]["verdict"] == "confirmed"


def test_parse_verdicts_invalid_verdict_falls_back_to_pending():
    raw = json.dumps([{"index": 0, "verdict": "maybe", "reason": "unsure"}])
    verdicts = _parse_verdicts(raw, batch_len=1)
    assert verdicts[0]["verdict"] == "pending"


def test_parse_verdicts_missing_defaults_to_pending():
    verdicts = _parse_verdicts("[]", batch_len=2)
    assert verdicts[0]["verdict"] == "pending"
    assert verdicts[1]["verdict"] == "pending"


def test_resolve_predictions_only_returns_non_pending():
    pending = [
        {"id": 1, "claim": "IRGC reshuffles Quds Force leadership by March", "target_date": "2026-03-01"},
        {"id": 2, "claim": "No ceasefire announced this month", "target_date": None},
    ]
    items = [_item("IRGC names new Quds Force commander")]
    raw_response = json.dumps(
        [
            {"index": 0, "verdict": "confirmed", "reason": "new commander named"},
            {"index": 1, "verdict": "pending", "reason": ""},
        ]
    )
    with patch("pipeline.predictions.call_ollama", return_value=raw_response):
        resolutions = resolve_predictions(pending, items, ollama_host="http://x", model="m")

    assert len(resolutions) == 1
    assert resolutions[0]["id"] == 1
    assert resolutions[0]["verdict"] == "confirmed"


def test_resolve_predictions_empty_inputs_short_circuit():
    assert resolve_predictions([], [_item("x")], ollama_host="http://x", model="m") == []
    assert resolve_predictions([{"id": 1, "claim": "x", "target_date": None}], [], ollama_host="http://x", model="m") == []
