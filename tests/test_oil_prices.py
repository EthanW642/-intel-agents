from unittest.mock import MagicMock

import httpx

from pipeline import oil_prices as oil_prices_module
from pipeline.oil_prices import _pct_change, _summarize, fetch_oil_snapshot


class _FakeResponse:
    def __init__(self, data, status_ok=True):
        self._data = data
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            request = httpx.Request("GET", "https://api.eia.gov/v2/petroleum/pri/spt/data/")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self):
        return {"response": {"data": self._data}}


def _rows(*pairs):
    # pairs of (date, price), newest first -- matches sort[0][direction]=desc
    return [{"period": d, "value": str(p), "series": "RWTC"} for d, p in pairs]


def test_fetch_oil_snapshot_returns_none_without_api_key():
    assert fetch_oil_snapshot(None) is None
    assert fetch_oil_snapshot("") is None


def test_fetch_oil_snapshot_success_for_both_series(monkeypatch):
    wti_rows = _rows(("2026-07-31", 68.42), ("2026-07-30", 66.38), ("2026-07-24", 62.94))
    brent_rows = _rows(("2026-07-31", 71.90), ("2026-07-30", 69.94), ("2026-07-24", 65.72))

    def fake_get(url, params=None, timeout=None):
        series = params["facets[series][]"]
        rows = wti_rows if series == "RWTC" else brent_rows
        return _FakeResponse(rows)

    monkeypatch.setattr(oil_prices_module.httpx, "get", fake_get)

    snapshot = fetch_oil_snapshot("fake-key")

    assert snapshot is not None
    assert snapshot["wti"]["price"] == 68.42
    assert snapshot["wti"]["date"] == "2026-07-31"
    assert snapshot["wti"]["change_1d_pct"] is not None
    assert snapshot["brent"]["price"] == 71.90


def test_fetch_oil_snapshot_one_series_fails_other_still_returned(monkeypatch):
    wti_rows = _rows(("2026-07-31", 68.42), ("2026-07-30", 66.38))

    def fake_get(url, params=None, timeout=None):
        series = params["facets[series][]"]
        if series == "RWTC":
            return _FakeResponse(wti_rows)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(oil_prices_module.httpx, "get", fake_get)

    snapshot = fetch_oil_snapshot("fake-key")

    assert snapshot is not None
    assert "wti" in snapshot
    assert "brent" not in snapshot


def test_fetch_oil_snapshot_both_series_fail_returns_none(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(oil_prices_module.httpx, "get", fake_get)

    assert fetch_oil_snapshot("fake-key") is None


def test_fetch_oil_snapshot_http_error_status_treated_as_failure(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse([], status_ok=False)

    monkeypatch.setattr(oil_prices_module.httpx, "get", fake_get)

    assert fetch_oil_snapshot("fake-key") is None


def test_fetch_oil_snapshot_skips_malformed_rows(monkeypatch):
    # A row missing "value" (EIA sometimes omits it for a gap day) shouldn't
    # crash parsing -- it's just dropped, same log-and-skip pattern as the
    # rest of this pipeline's external-source parsing.
    rows = [
        {"period": "2026-07-31", "series": "RWTC"},  # missing "value"
        {"period": "2026-07-30", "value": "66.38", "series": "RWTC"},
    ]

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(rows)

    monkeypatch.setattr(oil_prices_module.httpx, "get", fake_get)

    snapshot = fetch_oil_snapshot("fake-key")

    assert snapshot["wti"]["price"] == 66.38  # the only row that parsed


def test_pct_change_basic_math():
    assert round(_pct_change(110, 100), 2) == 10.0
    assert round(_pct_change(90, 100), 2) == -10.0


def test_summarize_single_row_has_no_change_data():
    summary = _summarize([{"date": "2026-07-31", "price": 68.42}])
    assert summary["price"] == 68.42
    assert summary["change_1d_pct"] is None
    assert summary["change_7d_pct"] is None


def test_summarize_picks_closest_row_to_seven_days_ago():
    # Trading days skip weekends -- the closest available row to exactly
    # 7 calendar days back is 2026-07-24 (Friday), not an exact match.
    rows = [
        {"date": "2026-07-31", "price": 68.42},
        {"date": "2026-07-30", "price": 66.38},
        {"date": "2026-07-29", "price": 65.00},
        {"date": "2026-07-24", "price": 62.94},
        {"date": "2026-07-23", "price": 61.00},
    ]
    summary = _summarize(rows)
    expected = round(_pct_change(68.42, 62.94), 2)
    assert summary["change_7d_pct"] == expected
