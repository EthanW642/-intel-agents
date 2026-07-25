import sys
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pandas as pd

from agents.middle_east.sources import _gdelt_latest_available_date, fetch_gdelt

CFG = {
    "table": "events",
    "lookback_days": 1,
    "actor_country_codes": ["ISR", "IRN", "LBN"],
    "geo_country_codes": ["IS", "IR", "LE"],
    "min_num_mentions": 10,
    "min_abs_goldstein": 5.0,
}


def _row(**overrides):
    base = {
        "Actor1CountryCode": None,
        "Actor2CountryCode": None,
        "ActionGeo_CountryCode": None,
        "NumMentions": 0,
        "GoldsteinScale": 0.0,
        "SOURCEURL": "http://example.com/story",
        "Actor1Name": "A",
        "Actor2Name": "B",
        "EventCode": "042",
        "AvgTone": 0.0,
        "SQLDATE": "20260723",
    }
    base.update(overrides)
    return base


def _fetch_with_rows(rows: list[dict]):
    df = pd.DataFrame(rows)
    fake_gd_instance = MagicMock()
    fake_gd_instance.Search.return_value = df
    fake_gdelt_module = MagicMock()
    fake_gdelt_module.gdelt.return_value = fake_gd_instance
    # Filter-logic tests shouldn't depend on a real network call to GDELT's
    # lastupdate endpoint (covered separately below) — stub in a fixed valid
    # date so fetch_gdelt proceeds to the actual row-filtering logic under
    # test, rather than skipping early (which is what a None return now
    # correctly does — see test_fetch_gdelt_skips_cleanly_when_date_unknown).
    with patch.dict(sys.modules, {"gdelt": fake_gdelt_module}), patch(
        "agents.middle_east.sources._gdelt_latest_available_date", return_value=date(2025, 7, 23)
    ):
        return fetch_gdelt(CFG)


def test_global_power_vs_unrelated_country_is_excluded():
    # This is the exact bug: USA is not in actor_country_codes anymore, so a
    # USA-vs-France event with no Middle East nexus must NOT match, even
    # with USA historically having been a broad "catch-all" actor code.
    rows = [_row(Actor1CountryCode="USA", Actor2CountryCode="FRA", NumMentions=500, GoldsteinScale=8.0)]
    items = _fetch_with_rows(rows)
    assert items == []


def test_global_power_vs_middle_east_country_is_included():
    # US-vs-Iran should still match: Iran (the OTHER actor) is in the list.
    rows = [_row(Actor1CountryCode="USA", Actor2CountryCode="IRN", NumMentions=20, GoldsteinScale=-5.0)]
    items = _fetch_with_rows(rows)
    assert len(items) == 1


def test_geo_match_captures_region_regardless_of_actors():
    rows = [_row(Actor1CountryCode="FRA", Actor2CountryCode="DEU", ActionGeo_CountryCode="LE", NumMentions=15)]
    items = _fetch_with_rows(rows)
    assert len(items) == 1


def test_low_mentions_but_high_goldstein_magnitude_is_kept():
    rows = [_row(Actor1CountryCode="ISR", NumMentions=1, GoldsteinScale=-9.0)]
    items = _fetch_with_rows(rows)
    assert len(items) == 1


def test_high_mentions_but_low_goldstein_is_kept():
    rows = [_row(Actor1CountryCode="ISR", NumMentions=50, GoldsteinScale=0.5)]
    items = _fetch_with_rows(rows)
    assert len(items) == 1


def test_low_mentions_and_low_goldstein_is_dropped():
    rows = [_row(Actor1CountryCode="ISR", NumMentions=1, GoldsteinScale=0.5)]
    items = _fetch_with_rows(rows)
    assert items == []


def test_missing_url_is_dropped_even_if_otherwise_matching():
    rows = [_row(Actor1CountryCode="ISR", NumMentions=50, SOURCEURL="")]
    items = _fetch_with_rows(rows)
    assert items == []


# ---- GDELT live-date discovery (spec: don't trust a possibly-wrong local clock) ----


def test_gdelt_latest_available_date_parses_real_response_shape():
    fake_body = (
        "12345 abc123 http://data.gdeltproject.org/gdeltv2/20250723176000.export.CSV.zip\n"
        "67890 def456 http://data.gdeltproject.org/gdeltv2/20250723176000.mentions.CSV.zip\n"
        "11121 ghi789 http://data.gdeltproject.org/gdeltv2/20250723176000.gkg.csv.zip\n"
    )
    fake_response = MagicMock()
    fake_response.text = fake_body
    fake_response.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_response):
        result = _gdelt_latest_available_date()
    assert result == date(2025, 7, 23)


def test_gdelt_latest_available_date_falls_back_to_none_on_network_failure():
    with patch("httpx.get", side_effect=httpx.HTTPError("boom")):
        result = _gdelt_latest_available_date()
    assert result is None


def test_gdelt_latest_available_date_falls_back_to_none_on_unexpected_body():
    fake_response = MagicMock()
    fake_response.text = "not the expected format at all"
    fake_response.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_response):
        result = _gdelt_latest_available_date()
    assert result is None


def test_fetch_gdelt_skips_cleanly_when_date_unknown():
    # Regression: fetch_gdelt used to fall back to the local clock when the
    # live date check failed, which — in an environment where the local
    # clock itself is wrong — just reproduced gdeltPyR's ValueError every
    # run instead of degrading gracefully. It must skip instead.
    fake_gdelt_module = MagicMock()
    with patch.dict(sys.modules, {"gdelt": fake_gdelt_module}), patch(
        "agents.middle_east.sources._gdelt_latest_available_date", return_value=None
    ):
        items = fetch_gdelt(CFG)
    assert items == []
    fake_gdelt_module.gdelt.assert_not_called()  # never even attempts Search()
