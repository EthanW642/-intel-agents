import csv
import io
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx

from agents.middle_east.sources import (
    GDELT_COL,
    GDELT_EXPORT_NUM_COLUMNS,
    _clean_gdelt_actor_name,
    _gdelt_export_stamps,
    _gdelt_latest_available_timestamp,
    _gdelt_row_to_item,
    _parse_gdelt_export,
    fetch_gdelt,
)

CFG = {
    "lookback_days": 1,
    "actor_country_codes": ["ISR", "IRN", "LBN"],
    "geo_country_codes": ["IS", "IR", "LE"],
    "min_num_mentions": 10,
    "min_abs_goldstein": 5.0,
}

ACTOR_CODES = set(CFG["actor_country_codes"])
GEO_CODES = set(CFG["geo_country_codes"])
FALLBACK_DATE = "2026-07-23"


def _row(**overrides):
    base = {
        "SQLDATE": "20260723",
        "Actor1Name": "A",
        "Actor1CountryCode": "",
        "Actor2Name": "B",
        "Actor2CountryCode": "",
        "EventCode": "042",
        "GoldsteinScale": "0.0",
        "NumMentions": "0",
        "AvgTone": "0.0",
        "ActionGeo_CountryCode": "",
        "SOURCEURL": "http://example.com/story",
    }
    base.update(overrides)
    return base


def _filter(row):
    return _gdelt_row_to_item(
        row, ACTOR_CODES, GEO_CODES, CFG["min_num_mentions"], CFG["min_abs_goldstein"], FALLBACK_DATE
    )


# ---- spec 4.1 filter semantics (unchanged from the gdeltPyR era) ----


def test_global_power_vs_unrelated_country_is_excluded():
    # USA is not in actor_country_codes anymore, so a USA-vs-France event
    # with no Middle East nexus must NOT match.
    item = _filter(_row(Actor1CountryCode="USA", Actor2CountryCode="FRA", NumMentions="500", GoldsteinScale="8.0"))
    assert item is None


def test_global_power_vs_middle_east_country_is_included():
    # US-vs-Iran should still match: Iran (the OTHER actor) is in the list.
    item = _filter(_row(Actor1CountryCode="USA", Actor2CountryCode="IRN", NumMentions="20", GoldsteinScale="-5.0"))
    assert item is not None


def test_geo_match_captures_region_regardless_of_actors():
    item = _filter(_row(Actor1CountryCode="FRA", Actor2CountryCode="DEU", ActionGeo_CountryCode="LE", NumMentions="15"))
    assert item is not None


def test_low_mentions_but_high_goldstein_magnitude_is_kept():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="1", GoldsteinScale="-9.0"))
    assert item is not None


def test_high_mentions_but_low_goldstein_is_kept():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="50", GoldsteinScale="0.5"))
    assert item is not None


def test_low_mentions_and_low_goldstein_is_dropped():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="1", GoldsteinScale="0.5"))
    assert item is None


def test_missing_url_is_dropped_even_if_otherwise_matching():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="50", SOURCEURL=""))
    assert item is None


def test_published_date_is_normalized_to_iso():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="50"))
    assert item.published == "2026-07-23"


def test_unparseable_sqldate_falls_back_to_run_date():
    item = _filter(_row(Actor1CountryCode="ISR", NumMentions="50", SQLDATE="bogus"))
    assert item.published == FALLBACK_DATE


# ---- GDELT live-date discovery (spec: don't trust a possibly-wrong local clock) ----


def test_gdelt_latest_available_timestamp_parses_real_response_shape():
    fake_body = (
        "12345 abc123 http://data.gdeltproject.org/gdeltv2/20250723174500.export.CSV.zip\n"
        "67890 def456 http://data.gdeltproject.org/gdeltv2/20250723174500.mentions.CSV.zip\n"
        "11121 ghi789 http://data.gdeltproject.org/gdeltv2/20250723174500.gkg.csv.zip\n"
    )
    fake_response = MagicMock()
    fake_response.text = fake_body
    fake_response.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_response):
        result = _gdelt_latest_available_timestamp()
    assert result == datetime(2025, 7, 23, 17, 45, 0, tzinfo=timezone.utc)


def test_gdelt_latest_available_timestamp_falls_back_to_none_on_network_failure():
    with patch("httpx.get", side_effect=httpx.HTTPError("boom")):
        result = _gdelt_latest_available_timestamp()
    assert result is None


def test_gdelt_latest_available_timestamp_falls_back_to_none_on_unexpected_body():
    fake_response = MagicMock()
    fake_response.text = "not the expected format at all"
    fake_response.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_response):
        result = _gdelt_latest_available_timestamp()
    assert result is None


def test_fetch_gdelt_skips_cleanly_when_date_unknown():
    # With the latest-export timestamp unknowable, fetch_gdelt must skip
    # (no guessing from the local clock — a wrong guess produces 404 storms
    # or silently missing coverage).
    with patch("agents.middle_east.sources._gdelt_latest_available_timestamp", return_value=None):
        items = fetch_gdelt(CFG)
    assert items == []


# ---- export-file enumeration (deterministic 15-minute URL grid) ----


def test_export_stamps_cover_lookback_window_on_15_minute_grid():
    latest = datetime(2026, 7, 23, 17, 45, 0, tzinfo=timezone.utc)
    stamps = _gdelt_export_stamps(latest, lookback_days=1)
    assert stamps[0] == "20260722174500"
    assert stamps[-1] == "20260723174500"
    assert len(stamps) == 97  # 96 fifteen-minute steps + both endpoints
    assert all(s.endswith(("0000", "1500", "3000", "4500")) for s in stamps)


# ---- export-file parsing (headerless 61-column TSV inside a zip) ----


def _make_export_zip(rows: list[dict]) -> bytes:
    """Build a synthetic .export.CSV.zip from dict rows keyed like GDELT_COL."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t")
    for row in rows:
        cols = [""] * GDELT_EXPORT_NUM_COLUMNS
        for key, idx in GDELT_COL.items():
            cols[idx] = str(row.get(key, ""))
        writer.writerow(cols)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("20260723174500.export.CSV", buf.getvalue())
    return zbuf.getvalue()


def test_parse_gdelt_export_roundtrips_named_columns():
    zip_bytes = _make_export_zip([_row(Actor1CountryCode="ISR", NumMentions="50")])
    rows = _parse_gdelt_export(zip_bytes)
    assert len(rows) == 1
    assert rows[0]["Actor1CountryCode"] == "ISR"
    assert rows[0]["SOURCEURL"] == "http://example.com/story"


def test_parse_gdelt_export_skips_rows_with_wrong_column_count():
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("x.export.CSV", "only\tthree\tcolumns\n")
    assert _parse_gdelt_export(zbuf.getvalue()) == []


def test_fetch_gdelt_end_to_end_with_mocked_downloads():
    latest = datetime(2026, 7, 23, 0, 15, 0, tzinfo=timezone.utc)
    zip_bytes = _make_export_zip(
        [
            _row(Actor1CountryCode="ISR", NumMentions="50"),  # kept
            _row(Actor1CountryCode="USA", Actor2CountryCode="FRA", NumMentions="500"),  # filtered
            _row(Actor1CountryCode="IRN", NumMentions="40", SOURCEURL="http://example.com/story"),  # dup URL
        ]
    )

    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "lastupdate" in url:
            resp.text = "1 a http://data.gdeltproject.org/gdeltv2/20260723001500.export.CSV.zip\n"
        else:
            resp.status_code = 200
            resp.content = zip_bytes
        resp.raise_for_status = MagicMock()
        return resp

    cfg = dict(CFG, lookback_days=0)
    with patch("agents.middle_east.sources.httpx.get", side_effect=fake_get), patch(
        "agents.middle_east.sources._gdelt_latest_available_timestamp", return_value=latest
    ):
        items = fetch_gdelt(cfg)

    # One export file in a 0-day window (just the latest stamp), serving 3
    # rows: 1 kept, 1 filtered out (no ME nexus), 1 dropped as a same-URL
    # duplicate of the kept row.
    assert len(items) == 1
    assert items[0].source == "GDELT"
    assert items[0].raw_metadata["tier"] == 1
    assert items[0].published == "2026-07-23"


# ---- GDELT actor-name cleaning (kept from the pandas era) ----


def test_clean_actor_name_catches_float_nan():
    assert _clean_gdelt_actor_name(float("nan")) is None


def test_clean_actor_name_catches_none():
    assert _clean_gdelt_actor_name(None) is None


def test_clean_actor_name_catches_literal_nan_string():
    assert _clean_gdelt_actor_name("nan") is None
    assert _clean_gdelt_actor_name("NaN") is None


def test_clean_actor_name_catches_empty_string():
    assert _clean_gdelt_actor_name("") is None
    assert _clean_gdelt_actor_name("   ") is None


def test_clean_actor_name_passes_through_real_name():
    assert _clean_gdelt_actor_name("IRAN") == "IRAN"
    assert _clean_gdelt_actor_name("  Benjamin Netanyahu  ") == "Benjamin Netanyahu"


def test_rows_with_no_identifiable_actor_are_dropped():
    dropped = _filter(_row(Actor1CountryCode="ISR", Actor1Name="", Actor2Name="", NumMentions="50"))
    kept = _filter(_row(Actor1CountryCode="ISR", Actor1Name="Israel", Actor2Name="", NumMentions="50"))
    assert dropped is None
    assert kept is not None
    assert "nan" not in kept.text.lower()
