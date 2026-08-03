import httpx

from pipeline import satellite_hotspots as hotspots_module
from pipeline.satellite_hotspots import fetch_strike_zone_hotspots


class _FakeResponse:
    def __init__(self, text, status_ok=True):
        self.text = text
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            request = httpx.Request("GET", "https://firms.modaps.eosdis.nasa.gov/api/area/csv")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)


CSV_HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight"


def _csv(*rows: str) -> str:
    return "\n".join([CSV_HEADER, *rows])


def test_fetch_strike_zone_hotspots_returns_none_without_api_key():
    assert fetch_strike_zone_hotspots(None) is None
    assert fetch_strike_zone_hotspots("") is None


def test_fetch_strike_zone_hotspots_parses_valid_csv(monkeypatch):
    csv_text = _csv(
        "31.5,34.5,320.1,0.4,0.4,2026-07-31,1423,N,VIIRS,high,2.0,290.1,45.2,D",
        "29.9,47.8,305.2,0.4,0.4,2026-07-31,0911,N,VIIRS,nominal,2.0,280.0,12.1,N",
    )

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    result = fetch_strike_zone_hotspots("fake-key")

    assert result is not None
    assert len(result) == 2
    assert result[0]["lat"] == 31.5
    assert result[0]["lon"] == 34.5
    assert result[0]["confidence"] == "high"
    assert result[0]["frp_mw"] == 45.2


def test_fetch_strike_zone_hotspots_excludes_low_confidence(monkeypatch):
    csv_text = _csv(
        "31.5,34.5,320.1,0.4,0.4,2026-07-31,1423,N,VIIRS,high,2.0,290.1,45.2,D",
        "29.9,47.8,305.2,0.4,0.4,2026-07-31,0911,N,VIIRS,low,2.0,280.0,3.1,N",
    )

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    result = fetch_strike_zone_hotspots("fake-key")

    assert len(result) == 1
    assert result[0]["confidence"] == "high"


def test_fetch_strike_zone_hotspots_sorts_most_recent_first(monkeypatch):
    csv_text = _csv(
        "31.5,34.5,320.1,0.4,0.4,2026-07-29,0800,N,VIIRS,high,2.0,290.1,10.0,D",
        "29.9,47.8,305.2,0.4,0.4,2026-07-31,0911,N,VIIRS,high,2.0,280.0,12.1,N",
        "28.0,45.0,300.0,0.4,0.4,2026-07-30,1200,N,VIIRS,high,2.0,275.0,8.0,D",
    )

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    result = fetch_strike_zone_hotspots("fake-key")

    assert [h["date"] for h in result] == ["2026-07-31", "2026-07-30", "2026-07-29"]


def test_fetch_strike_zone_hotspots_caps_at_max(monkeypatch):
    rows = [
        f"31.{i},34.{i},320.1,0.4,0.4,2026-07-31,{1000 + i},N,VIIRS,high,2.0,290.1,10.0,D" for i in range(40)
    ]
    csv_text = _csv(*rows)

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    result = fetch_strike_zone_hotspots("fake-key")

    assert len(result) == hotspots_module.MAX_HOTSPOTS


def test_fetch_strike_zone_hotspots_returns_none_on_http_failure(monkeypatch):
    def fake_get(url, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    assert fetch_strike_zone_hotspots("fake-key") is None


def test_fetch_strike_zone_hotspots_returns_none_on_http_error_status(monkeypatch):
    def fake_get(url, timeout=None):
        return _FakeResponse("", status_ok=False)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    assert fetch_strike_zone_hotspots("fake-key") is None


def test_fetch_strike_zone_hotspots_returns_none_when_all_rows_filtered_out(monkeypatch):
    csv_text = _csv("31.5,34.5,320.1,0.4,0.4,2026-07-31,1423,N,VIIRS,low,2.0,290.1,3.0,D")

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    assert fetch_strike_zone_hotspots("fake-key") is None


def test_fetch_strike_zone_hotspots_skips_malformed_rows(monkeypatch):
    # A row with a non-numeric latitude shouldn't crash parsing -- dropped,
    # same log-and-skip pattern as pipeline/oil_prices.py.
    csv_text = _csv(
        "not-a-number,34.5,320.1,0.4,0.4,2026-07-31,1423,N,VIIRS,high,2.0,290.1,45.2,D",
        "29.9,47.8,305.2,0.4,0.4,2026-07-31,0911,N,VIIRS,high,2.0,280.0,12.1,N",
    )

    def fake_get(url, timeout=None):
        return _FakeResponse(csv_text)

    monkeypatch.setattr(hotspots_module.httpx, "get", fake_get)

    result = fetch_strike_zone_hotspots("fake-key")

    assert len(result) == 1
    assert result[0]["lat"] == 29.9
