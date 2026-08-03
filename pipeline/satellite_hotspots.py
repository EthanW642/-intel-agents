"""Stage 3c — satellite thermal-anomaly detection (structured/primary,
Tier 1 — same class of input as GDELT and the oil price snapshot), added
2026-07-31 per explicit request for "where strikes are hit" analysis.

Uses NASA FIRMS (Fire Information for Resource Management System) — free,
official, no credit card required. Get a key at
https://firms.modaps.eosdis.nasa.gov/api/map_key/ and set FIRMS_MAP_KEY in
.env.

IMPORTANT LIMITATION, by explicit design decision (not an oversight): FIRMS
detects heat signatures from VIIRS/MODIS satellites, not confirmed military
strikes specifically. In this region in particular, routine industrial gas
flaring (Iraq and the Gulf states are among the world's heaviest flarers)
will show up as recurring hotspots at the same facility locations every
day, indistinguishable from a real strike at the raw-data level. This
module deliberately does NOT try to filter or suppress that noise — it
passes through confidence-filtered detections with an explicit
corroboration-required caveat baked into the prompt section (see
pipeline/analyze.py::_format_hotspots_section), the same pattern already
used for GDELT's noisy event data, which the analysis prompt's Tier 1/
corroboration-required tiering already handles well in practice (confirmed
live 2026-07-31: the model correctly flagged an uncorroborated GDELT signal
as "Speculation ... flagged for watch, not asserted" rather than treating
it as settled fact). Trust that same reasoning discipline here rather than
building bespoke hotspot-suppression logic.

UNVERIFIED from this build environment (network-restricted sandbox, same
as every other external source in this repo) — fails closed (returns None,
logs a warning) rather than crashing the pipeline if the request or CSV
shape is wrong, so a bad integration here degrades to "no hotspot data
today," not a lost run.
"""
from __future__ import annotations

import csv
import io
import logging

import httpx

logger = logging.getLogger(__name__)

FIRMS_API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
SOURCE = "VIIRS_SNPP_NRT"
# west,south,east,north -- covers the same region as config/sources.yaml's
# GDELT geo_country_codes (Israel/Palestine through Iran, Egypt through
# Turkey's southern edge). Not verified against any FIRMS area-size limit
# from this sandbox; FIRMS' own docs example a comparably large South Asia
# box (54,5.5,102,40), so this should be within normal bounds.
MIDDLE_EAST_BBOX = "24,11,64,42"
LOOKBACK_DAYS = 1
# VIIRS confidence is "low"/"nominal"/"high" (not a 0-100 score like
# MODIS). Excludes only "low" -- keeps nominal+high as a reasonable
# quality bar without being so narrow it drops most real detections.
MIN_CONFIDENCE_EXCLUDES = {"low"}
MAX_HOTSPOTS = 30


def fetch_strike_zone_hotspots(api_key: str | None, timeout: float = 20.0) -> list[dict] | None:
    """Returns a list of confidence-filtered thermal-anomaly detections in
    the Middle East bounding box over the last LOOKBACK_DAYS, most recent
    first, capped at MAX_HOTSPOTS -- or None if unavailable (no
    FIRMS_MAP_KEY configured, or the request/parse failed). Same
    degrade-gracefully pattern as pipeline/oil_prices.py."""
    if not api_key:
        logger.info("FIRMS_MAP_KEY not set — skipping satellite hotspot fetch.")
        return None

    url = f"{FIRMS_API_BASE}/{api_key}/{SOURCE}/{MIDDLE_EAST_BBOX}/{LOOKBACK_DAYS}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
    except Exception:
        logger.exception("Failed to fetch/parse FIRMS hotspot data")
        return None

    hotspots: list[dict] = []
    for row in rows:
        try:
            confidence = (row.get("confidence") or "").strip().lower()
            if confidence in MIN_CONFIDENCE_EXCLUDES:
                continue
            hotspots.append(
                {
                    "lat": float(row["latitude"]),
                    "lon": float(row["longitude"]),
                    "date": row["acq_date"],
                    "time": row["acq_time"],
                    "confidence": confidence,
                    "frp_mw": float(row["frp"]) if row.get("frp") else None,
                    "satellite": row.get("satellite", "unknown"),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    if not hotspots:
        return None

    hotspots.sort(key=lambda h: (h["date"], h["time"]), reverse=True)
    return hotspots[:MAX_HOTSPOTS]
