from datetime import datetime, timezone

import httpx

from agents.middle_east import sources as sources_module
from agents.middle_east.sources import (
    _asam_date_to_iso,
    _asam_field,
    _asam_response_to_rows,
    _asam_row_to_item,
    fetch_asam,
)

BBOX = (20.0, 8.0, 65.0, 32.0)


def _row(**overrides):
    base = {
        "description": "Vessel attacked with small arms fire.",
        "latitude": "13.5",
        "longitude": "43.2",
        "date": "2026-08-05",
        "reference": "2026-123",
        "hostility": "Fired Upon",
        "navArea": "Red Sea",
    }
    base.update(overrides)
    return base


# ---- field matching across unverified possible key-name variants ----


def test_asam_field_matches_case_insensitively():
    row = {"Latitude": "12.3", "DESCRIPTION": "x"}
    assert _asam_field(row, "latitude") == "12.3"
    assert _asam_field(row, "description") == "x"


def test_asam_field_tries_alternate_candidate_names():
    row = {"lat": "12.3"}  # not "latitude"
    assert _asam_field(row, "latitude") == "12.3"


def test_asam_field_missing_returns_none():
    assert _asam_field({}, "description") is None


# ---- envelope unwrapping ----


def test_response_to_rows_bare_list():
    assert _asam_response_to_rows([{"a": 1}]) == [{"a": 1}]


def test_response_to_rows_wrapped_dict():
    assert _asam_response_to_rows({"data": [{"a": 1}]}) == [{"a": 1}]
    assert _asam_response_to_rows({"results": [{"a": 1}]}) == [{"a": 1}]


def test_response_to_rows_unrecognized_shape_returns_empty():
    assert _asam_response_to_rows({"unexpected": "shape"}) == []
    assert _asam_response_to_rows("not even json-object-or-list") == []


# ---- date parsing ----


def test_date_parses_iso_date():
    iso, dt = _asam_date_to_iso("2026-08-05")
    assert iso == "2026-08-05"
    assert dt.year == 2026 and dt.month == 8 and dt.day == 5


def test_date_parses_iso_datetime_with_z():
    iso, dt = _asam_date_to_iso("2026-08-05T14:30:00Z")
    assert iso == "2026-08-05"
    assert dt is not None


def test_date_none_input():
    assert _asam_date_to_iso(None) == (None, None)


def test_date_unparseable_falls_back_to_prefix():
    iso, dt = _asam_date_to_iso("not-a-real-date")
    assert dt is None
    # still returns *something* usable rather than crashing


# ---- row -> item filtering (bbox, missing fields) ----


def test_row_kept_inside_bbox():
    result = _asam_row_to_item(_row(), BBOX)
    assert result is not None
    item, published_dt = result
    assert item.source == "NGA-ASAM"
    assert item.raw_metadata["tier"] == 1
    assert "Fired Upon" in item.title


def test_row_dropped_outside_bbox():
    # Far outside the Middle East/chokepoint box (e.g. Gulf of Guinea piracy).
    result = _asam_row_to_item(_row(latitude="4.0", longitude="3.0"), BBOX)
    assert result is None


def test_row_dropped_missing_description():
    result = _asam_row_to_item(_row(description=""), BBOX)
    assert result is None


def test_row_dropped_unparseable_coordinates():
    result = _asam_row_to_item(_row(latitude="not-a-number"), BBOX)
    assert result is None


def test_row_url_uses_reference_when_present():
    result = _asam_row_to_item(_row(reference="2026-999"), BBOX)
    item, _ = result
    assert "2026-999" in item.url


def test_row_falls_back_to_base_url_without_reference():
    result = _asam_row_to_item(_row(reference=""), BBOX)
    item, _ = result
    assert item.url == sources_module.ASAM_API_BASE


# ---- fetch_asam end-to-end with mocked httpx ----


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            request = httpx.Request("GET", sources_module.ASAM_API_BASE)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self):
        return self._payload


def test_fetch_asam_filters_by_region_and_lookback(monkeypatch):
    now = datetime.now(timezone.utc)
    recent_date = now.strftime("%Y-%m-%d")
    old_date = "2020-01-01"
    rows = [
        _row(description="in-region, recent", latitude="13.5", longitude="43.2", date=recent_date),
        _row(description="in-region, stale", latitude="13.5", longitude="43.2", date=old_date),
        _row(description="out-of-region", latitude="4.0", longitude="3.0", date=recent_date),
    ]
    monkeypatch.setattr(sources_module.httpx, "get", lambda *a, **k: _FakeResponse(rows))

    items = fetch_asam({"lookback_days": 3, "bbox": list(BBOX)})

    assert len(items) == 1
    assert items[0].text.startswith("in-region, recent")


def test_fetch_asam_network_failure_returns_empty(monkeypatch):
    def fake_get(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(sources_module.httpx, "get", fake_get)
    assert fetch_asam({}) == []


def test_fetch_asam_http_error_status_returns_empty(monkeypatch):
    monkeypatch.setattr(sources_module.httpx, "get", lambda *a, **k: _FakeResponse([], status_ok=False))
    assert fetch_asam({}) == []


def test_fetch_asam_empty_response_returns_empty(monkeypatch):
    monkeypatch.setattr(sources_module.httpx, "get", lambda *a, **k: _FakeResponse([]))
    assert fetch_asam({}) == []


def test_fetch_asam_unmatched_shape_logs_and_returns_empty(monkeypatch):
    # Rows exist but none have a usable lat/lon/description -- the "field
    # shape probably changed" warning path, not a crash.
    monkeypatch.setattr(
        sources_module.httpx, "get", lambda *a, **k: _FakeResponse([{"totally": "unexpected"}])
    )
    assert fetch_asam({}) == []
