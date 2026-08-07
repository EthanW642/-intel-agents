import httpx

from pipeline import crs_reports as crs_module
from pipeline.crs_reports import MAX_REPORTS, _extract_reports, _is_relevant, fetch_crs_snapshot


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            request = httpx.Request("GET", crs_module.CRS_API_BASE)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self):
        return self._payload


def _report(**overrides):
    base = {
        "title": "Iran Sanctions: Overview and Issues for Congress",
        "summary": "This report discusses US sanctions policy toward Iran.",
        "publishDate": "2026-07-15T00:00:00Z",
        "url": "https://crsreports.congress.gov/product/details?prodcode=IF12345",
    }
    base.update(overrides)
    return base


def test_fetch_crs_snapshot_returns_none_without_api_key():
    assert fetch_crs_snapshot(None) is None
    assert fetch_crs_snapshot("") is None


def test_is_relevant_matches_middle_east_keywords():
    assert _is_relevant("Iran Sanctions Overview", "discusses Tehran")
    assert _is_relevant("The Situation in Gaza", "")
    assert not _is_relevant("Farm Bill Reauthorization", "crop insurance provisions")


def test_extract_reports_bare_list():
    assert _extract_reports([{"a": 1}]) == [{"a": 1}]


def test_extract_reports_wrapped_dict():
    assert _extract_reports({"CRSReports": [{"a": 1}]}) == [{"a": 1}]
    assert _extract_reports({"data": [{"a": 1}]}) == [{"a": 1}]


def test_extract_reports_unrecognized_shape_returns_empty():
    assert _extract_reports({"nope": "shape"}) == []


def test_fetch_crs_snapshot_filters_to_relevant_reports(monkeypatch):
    reports = [
        _report(title="Iran Sanctions: Overview and Issues for Congress"),
        _report(title="Farm Bill Reauthorization", summary="crop insurance provisions"),
        _report(title="Israel: Background and US Relations"),
    ]
    monkeypatch.setattr(crs_module.httpx, "get", lambda *a, **k: _FakeResponse(reports))

    snapshot = fetch_crs_snapshot("fake-key")

    assert snapshot is not None
    titles = [r["title"] for r in snapshot]
    assert "Farm Bill Reauthorization" not in titles
    assert any("Iran Sanctions" in t for t in titles)
    assert any("Israel" in t for t in titles)


def test_fetch_crs_snapshot_caps_at_max_reports(monkeypatch):
    reports = [_report(title=f"Iran Sanctions Update {i}") for i in range(MAX_REPORTS + 5)]
    monkeypatch.setattr(crs_module.httpx, "get", lambda *a, **k: _FakeResponse(reports))

    snapshot = fetch_crs_snapshot("fake-key")

    assert len(snapshot) == MAX_REPORTS


def test_fetch_crs_snapshot_no_relevant_reports_returns_none(monkeypatch):
    reports = [_report(title="Farm Bill Reauthorization", summary="crop insurance")]
    monkeypatch.setattr(crs_module.httpx, "get", lambda *a, **k: _FakeResponse(reports))

    assert fetch_crs_snapshot("fake-key") is None


def test_fetch_crs_snapshot_network_failure_returns_none(monkeypatch):
    def fake_get(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(crs_module.httpx, "get", fake_get)
    assert fetch_crs_snapshot("fake-key") is None


def test_fetch_crs_snapshot_http_error_status_returns_none(monkeypatch):
    monkeypatch.setattr(crs_module.httpx, "get", lambda *a, **k: _FakeResponse([], status_ok=False))
    assert fetch_crs_snapshot("fake-key") is None


def test_fetch_crs_snapshot_truncates_long_summary(monkeypatch):
    reports = [_report(title="Iran Sanctions", summary="x" * 1000)]
    monkeypatch.setattr(crs_module.httpx, "get", lambda *a, **k: _FakeResponse(reports))

    snapshot = fetch_crs_snapshot("fake-key")

    assert len(snapshot[0]["summary"]) == 500
