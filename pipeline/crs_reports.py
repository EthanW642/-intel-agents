"""Stage 3d — Congressional Research Service (CRS) report summaries
(standing IC/Congressional analytical context — NOT part of the Tier 1-4
news-sourcing scale), added 2026-08-06 as one half of "actual published
intelligence reports." Real CIA/MI6 products are classified and never hit
a public feed; CRS is Congress's own nonpartisan research arm (staffed in
part by former IC analysts), publishing frequently-updated, unclassified
reports on live policy topics — including Iran sanctions, the Gaza war,
Yemen, and Gulf security — which makes it the closest legitimate,
regularly-published, publicly fetchable equivalent.

Uses the official Congress.gov API v3 (Library of Congress) — free, no
credit card. Get a key at https://api.data.gov/signup/ (the same
api.data.gov key system used across federal open-data APIs) and set
CONGRESS_API_KEY in .env.

UNVERIFIED from this build environment (network-restricted sandbox, same
as every other external source in this repo) — api.congress.gov is
blocked here, so the endpoint path (/v3/crsreport) and the api_key/format
query-parameter convention are built from congress.gov's publicly
documented API v3 pattern shared by every other resource (bill, member,
summaries, etc.), not confirmed against a live response. Field names
(title, summary, publishDate, url) ARE confirmed from the official
endpoint documentation. This module fails closed (returns None, logs a
warning) rather than crashing the pipeline if the request or response
shape is wrong — same pattern as pipeline/oil_prices.py.

The list endpoint has no full-text/topic search, so this fetches the most
recently updated reports and filters to Middle East relevance client-side
by keyword match on title/summary — the same "fetch broad, filter
locally" approach as agents/middle_east/sources.py::fetch_asam, for the
same reason (don't trust an unverified server-side filter parameter).
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

CRS_API_BASE = "https://api.congress.gov/v3/crsreport"
FETCH_LIMIT = 100  # most-recently-updated reports to pull before filtering
MAX_REPORTS = 8  # cap on how many reach the prompt, to bound token cost

MIDDLE_EAST_KEYWORDS = (
    "israel", "gaza", "palestin", "iran", "lebanon", "hezbollah", "houthi",
    "yemen", "syria", "iraq", "saudi", "qatar", "bahrain", "kuwait", "oman",
    "united arab emirates", "hormuz", "red sea", "middle east", "west bank",
    "centcom",
)


def _is_relevant(title: str, summary: str) -> bool:
    haystack = f"{title} {summary}".lower()
    return any(kw in haystack for kw in MIDDLE_EAST_KEYWORDS)


def _extract_reports(parsed) -> list[dict]:
    """Defensively unwrap whatever envelope shape the live API uses — a
    bare list, or a dict wrapping the list under a resource-named key
    (congress.gov's other v3 endpoints use plural resource-name keys, but
    the exact casing for this one isn't confirmed live)."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("CRSReports", "crsReports", "CRSReport", "reports", "data", "results"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    return []


def fetch_crs_snapshot(api_key: str | None, timeout: float = 20.0) -> list[dict] | None:
    """Returns a list of {"title", "summary", "publish_date", "url"} for
    recently-updated CRS reports relevant to the Middle East, most recent
    first, capped at MAX_REPORTS — or None if unavailable (no
    CONGRESS_API_KEY configured, or the request/parse failed)."""
    if not api_key:
        logger.info("CONGRESS_API_KEY not set — skipping CRS report snapshot.")
        return None

    try:
        resp = httpx.get(
            CRS_API_BASE,
            params={"api_key": api_key, "format": "json", "limit": FETCH_LIMIT},
            timeout=timeout,
        )
        resp.raise_for_status()
        raw_reports = _extract_reports(resp.json())
    except Exception:
        logger.exception("Failed to fetch/parse CRS report list")
        return None

    if not raw_reports:
        logger.warning(
            "CRS report list returned 0 records — check the response shape against "
            "pipeline/crs_reports.py::_extract_reports"
        )
        return None

    snapshot: list[dict] = []
    for report in raw_reports:
        title = (report.get("title") or "").strip()
        summary = (report.get("summary") or "").strip()
        if not title or not _is_relevant(title, summary):
            continue
        snapshot.append(
            {
                "title": title,
                "summary": summary[:500],
                "publish_date": (report.get("publishDate") or "")[:10],
                "url": report.get("url") or "",
            }
        )
        if len(snapshot) >= MAX_REPORTS:
            break

    return snapshot or None
