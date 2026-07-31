"""Stage 1.5 — oil price snapshot (structured/primary, Tier 1 — same class
of input as GDELT), added 2026-07-31 to give Stage 4's "Economics &
markets" analytical lens real numbers to reason over instead of relying on
narrative claims like "oil prices rose" from triaged articles.

Uses the EIA (U.S. Energy Information Administration) open data API v2 —
free, official, no credit card required. Get a key at
https://www.eia.gov/opendata/register.php and set EIA_API_KEY in .env.

Series used (daily spot prices, both from the petroleum/pri/spt endpoint):
- RWTC: Cushing, OK WTI Spot Price FOB
- RBRTE: Europe Brent Spot Price FOB

UNVERIFIED from this build environment (network-restricted sandbox, same
as every other external source in this repo) — the exact v2 query-parameter
shape (facets[series][], frequency, sort[]) is built from EIA's published
API v2 documentation, not confirmed against a live response. Test with a
real EIA_API_KEY before trusting this in production; this module fails
closed (returns None, logs a warning) rather than crashing the pipeline if
the request or response shape is wrong, so a bad integration here degrades
to "no oil snapshot today," not a lost run.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

EIA_API_BASE = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
SERIES = {"wti": "RWTC", "brent": "RBRTE"}
# Enough trading days to comfortably cover a 7-calendar-day lookback even
# across a long weekend/holiday cluster (EIA spot prices only post on
# trading days).
LOOKBACK_ROWS = 12


def _fetch_series(series_id: str, api_key: str, timeout: float) -> list[dict] | None:
    params = {
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": series_id,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": LOOKBACK_ROWS,
    }
    try:
        resp = httpx.get(EIA_API_BASE, params=params, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json()["response"]["data"]
    except Exception:
        logger.exception("Failed to fetch EIA series %s", series_id)
        return None
    # Rows are {"period": "YYYY-MM-DD", "value": "68.42", ...} per row, most
    # recent first (sort[0][direction]=desc above). Defensive parsing since
    # this is an external API response, not our own schema.
    parsed: list[dict] = []
    for row in rows:
        try:
            parsed.append({"date": row["period"], "price": float(row["value"])})
        except (KeyError, TypeError, ValueError):
            continue
    return parsed or None


def _pct_change(latest: float, reference: float) -> float:
    return ((latest - reference) / reference) * 100 if reference else 0.0


def _summarize(rows: list[dict]) -> dict:
    """rows is sorted newest-first. Computes latest price plus 1-day and
    7-day % change, using the nearest available trading day for each
    lookback since EIA spot prices skip weekends/holidays — an exact
    7-calendar-day match often doesn't exist."""
    latest = rows[0]
    summary = {"date": latest["date"], "price": latest["price"], "change_1d_pct": None, "change_7d_pct": None}
    if len(rows) > 1:
        summary["change_1d_pct"] = round(_pct_change(latest["price"], rows[1]["price"]), 2)
    if len(rows) > 1:
        from datetime import date as _date

        target = _date.fromisoformat(latest["date"])
        from datetime import timedelta as _timedelta

        seven_days_ago = target - _timedelta(days=7)
        closest = min(rows[1:], key=lambda r: abs((_date.fromisoformat(r["date"]) - seven_days_ago).days), default=None)
        if closest:
            summary["change_7d_pct"] = round(_pct_change(latest["price"], closest["price"]), 2)
    return summary


def fetch_oil_snapshot(api_key: str | None, timeout: float = 15.0) -> dict | None:
    """Returns {"wti": {...}, "brent": {...}} or None if unavailable (no
    key configured, or the request/parse failed) — mirrors the
    Gmail-credentials-missing pattern elsewhere in this pipeline: a
    missing/broken optional input skips that piece of context rather than
    aborting the run."""
    if not api_key:
        logger.info("EIA_API_KEY not set — skipping oil price snapshot.")
        return None

    snapshot: dict = {}
    for label, series_id in SERIES.items():
        rows = _fetch_series(series_id, api_key, timeout)
        if not rows:
            logger.warning("No usable EIA data for %s (%s) — omitting from oil snapshot.", label, series_id)
            continue
        snapshot[label] = _summarize(rows)

    return snapshot or None
