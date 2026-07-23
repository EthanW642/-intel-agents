import sys
from unittest.mock import MagicMock, patch

import pandas as pd

from agents.middle_east.sources import fetch_gdelt

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
    with patch.dict(sys.modules, {"gdelt": fake_gdelt_module}):
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
