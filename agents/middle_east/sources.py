"""Stage 1 (ingest) source fetchers for the Middle East agent.

Every fetcher returns a list of RawItem — the common shape the rest of the
pipeline (dedup -> triage -> memory -> analyze) operates on, regardless of
which structured feed or RSS source an item came from.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "sources.yaml"

# Some feeds (LiveUAMap, since retired, confirmed this; likely others) 403
# requests that don't look like a browser — no default User-Agent, no
# Accept header. This is the single source of truth for those headers;
# scripts/verify_sources.py imports it too, so the pre-flight check and
# real ingestion never drift apart on this.
RSS_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # A plain User-Agent/Accept pair alone wasn't enough for LiveUAMap
    # (still 403'd) — some bot-walls specifically check for a same-site
    # Referer to distinguish "loaded from a browser tab" from a bare
    # script hit. Left as a generic same-origin-looking value even after
    # LiveUAMap's removal since it's harmless for every other feed (all
    # verified live with this header set) and re-tuning it isn't worth the
    # churn unless a future feed 403s on User-Agent/Accept alone too.
    "Referer": "https://israelpalestine.liveuamap.com/",
}


@dataclass
class RawItem:
    title: str
    source: str
    url: str
    published: str  # ISO date string
    text: str  # first ~200 words / description
    domain: str = "middle_east"
    raw_metadata: dict = field(default_factory=dict)


def load_sources_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _truncate_words(text: str, n: int = 200) -> str:
    words = text.split()
    return " ".join(words[:n])


def _clean_gdelt_actor_name(value) -> str | None:
    """Normalize a GDELT actor-name field, returning None when it's
    genuinely missing. Kept from the pandas era (where a missing cell was a
    *truthy* float NaN that formatted as the literal string "nan" —
    confirmed live 2026-07-25 in a real analysis prompt); the direct CSV
    reader now yields empty strings for missing cells, but the "nan"
    guard stays because it's cheap and the failure it prevented was ugly.
    `x != x` is the standard NaN self-inequality check.
    """
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_EXPORT_URL_TEMPLATE = "http://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
GDELT_DOWNLOAD_WORKERS = 4

# GDELT 2.0 event-export files are headerless tab-separated CSVs with a
# fixed 61-column layout (the "ADM2-era" codebook). Only the columns the
# pipeline actually uses are named here; rows whose column count doesn't
# match are skipped (see _parse_gdelt_export).
GDELT_EXPORT_NUM_COLUMNS = 61
GDELT_COL = {
    "SQLDATE": 1,
    "Actor1Name": 6,
    "Actor1CountryCode": 7,
    "Actor2Name": 16,
    "Actor2CountryCode": 17,
    "EventCode": 26,
    "GoldsteinScale": 30,
    "NumMentions": 31,
    "AvgTone": 34,
    "ActionGeo_CountryCode": 53,
    "SOURCEURL": 60,
}


def _gdelt_latest_available_timestamp(timeout: float = 15.0) -> datetime | None:
    """Ask GDELT's own live feed for its most recent available 15-minute
    export timestamp (UTC), rather than assuming anything from the local
    clock. Returns None if the live check fails for any reason (network,
    unexpected response shape); the caller treats that as "skip GDELT this
    run" rather than guessing. Since the pipeline now derives every export
    file URL from this value, the old gdeltPyR local-clock-vs-UTC clamp
    problem no longer exists — there is no third-party date validation to
    appease."""
    try:
        resp = httpx.get(GDELT_LASTUPDATE_URL, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        logger.warning(
            "Could not reach GDELT's lastupdate feed at %s; skipping GDELT this run",
            GDELT_LASTUPDATE_URL,
        )
        return None

    match = re.search(r"(\d{14})\.export\.CSV", resp.text)
    if not match:
        logger.warning(
            "GDELT's lastupdate feed responded but didn't match the expected format "
            "(got: %r); skipping GDELT this run. The response format may have changed "
            "— update the regex in _gdelt_latest_available_timestamp if this persists.",
            resp.text[:200],
        )
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _gdelt_export_stamps(latest: datetime, lookback_days: int) -> list[str]:
    """Enumerate the 15-minute export-file timestamps covering
    [latest - lookback_days, latest]. GDELT file URLs are deterministic
    (data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip every 15
    minutes), so no master file list is needed; a missing file (skipped
    update) just 404s and is skipped."""
    stamps: list[str] = []
    cursor = latest - timedelta(days=lookback_days)
    # Align to the 15-minute grid.
    cursor = cursor.replace(minute=(cursor.minute // 15) * 15, second=0, microsecond=0)
    while cursor <= latest:
        stamps.append(cursor.strftime("%Y%m%d%H%M%S"))
        cursor += timedelta(minutes=15)
    return stamps


def _sqldate_to_iso(sqldate: str, fallback: str) -> str:
    try:
        return datetime.strptime(sqldate.strip(), "%Y%m%d").date().isoformat()
    except (ValueError, AttributeError):
        return fallback


def _gdelt_row_to_item(
    row: dict,
    actor_codes: set,
    geo_codes: set,
    min_mentions: int,
    min_abs_goldstein: float,
    fallback_date_iso: str,
) -> RawItem | None:
    """Apply the spec 4.1 filters to one GDELT event row (a plain dict keyed
    by GDELT_COL names) and build the RawItem, or return None to drop it.
    Pure function — the unit tests exercise the filter semantics here
    without touching the network."""
    try:
        mentions = int(row.get("NumMentions") or 0)
    except (ValueError, TypeError):
        mentions = 0
    try:
        goldstein = float(row["GoldsteinScale"]) if row.get("GoldsteinScale") not in (None, "") else None
    except (ValueError, TypeError):
        goldstein = None

    # Kept if EITHER threshold clears (spec 4.1) — a low-mention but
    # high-magnitude event (a breaking strike, before wire pickup
    # accumulates) shouldn't be dropped just because NumMentions is still
    # low, and vice versa.
    goldstein_ok = goldstein is not None and abs(goldstein) >= min_abs_goldstein
    if mentions < min_mentions and not goldstein_ok:
        return None

    actor_match = row.get("Actor1CountryCode") in actor_codes or row.get("Actor2CountryCode") in actor_codes
    geo_match = row.get("ActionGeo_CountryCode") in geo_codes
    if not (actor_match or geo_match):
        return None

    url = (row.get("SOURCEURL") or "").strip()
    if not url:
        return None

    a1_clean = _clean_gdelt_actor_name(row.get("Actor1Name"))
    a2_clean = _clean_gdelt_actor_name(row.get("Actor2Name"))
    if a1_clean is None and a2_clean is None:
        # Neither actor is identifiable — a bilateral event description
        # with no named party on either side is pure noise for the
        # analysis stage, not a borderline case.
        return None
    a1 = a1_clean or "unknown actor"
    a2 = a2_clean or "unknown actor"

    event_code = row.get("EventCode", "")
    desc = (
        f"GDELT event: {a1} -> {a2} (CAMEO {event_code}), "
        f"Goldstein={goldstein}, tone={row.get('AvgTone')}, "
        f"mentions={mentions}"
    )
    return RawItem(
        title=desc,
        source="GDELT",
        url=url,
        published=_sqldate_to_iso(str(row.get("SQLDATE", "")), fallback_date_iso),
        text=desc,
        # Tier 1 (structured/primary) per spec 5.1 — GDELT is the
        # structured event-occurrence source.
        raw_metadata={"event_code": event_code, "goldstein": goldstein, "tier": 1},
    )


def _parse_gdelt_export(zip_bytes: bytes) -> list[dict]:
    """Decompress one 15-minute export zip and yield its rows as plain
    dicts keyed by GDELT_COL names. Rows with an unexpected column count
    are skipped (schema drift guard)."""
    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            for cols in csv.reader(text, delimiter="\t"):
                if len(cols) != GDELT_EXPORT_NUM_COLUMNS:
                    continue
                rows.append({key: cols[idx] for key, idx in GDELT_COL.items()})
    return rows


def _fetch_gdelt_export_file(stamp: str, timeout: float = 30.0) -> list[dict]:
    """Download and parse one export file. A 404 means GDELT skipped that
    15-minute update — normal, skip quietly. Peak memory is one file
    (~1-5MB zipped), not the whole day: this replaces gdeltPyR's
    coverage=True bulk pull, which concatenated every file in the window
    into a single multi-GB pandas DataFrame before any filtering ran."""
    url = GDELT_EXPORT_URL_TEMPLATE.format(stamp=stamp)
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return _parse_gdelt_export(resp.content)
    except Exception:
        logger.warning("GDELT export file %s failed to fetch/parse — skipping it", stamp, exc_info=True)
        return []


def fetch_gdelt(cfg: dict) -> list[RawItem]:
    """Pull GDELT 2.0 events for the region by streaming the 15-minute
    export files directly and filtering row-by-row (spec 4.1). Each file is
    downloaded, filtered, and discarded before the next — memory stays flat
    regardless of how heavy a news day is."""
    lookback_days = cfg.get("lookback_days", 1)
    actor_codes = set(cfg.get("actor_country_codes", []))
    geo_codes = set(cfg.get("geo_country_codes", []))
    min_mentions = cfg.get("min_num_mentions", 0)
    min_abs_goldstein = cfg.get("min_abs_goldstein", 0)

    latest = _gdelt_latest_available_timestamp()
    if latest is None:
        logger.warning("Skipping GDELT this run — could not determine the latest available export")
        return []
    fallback_date_iso = latest.date().isoformat()

    stamps = _gdelt_export_stamps(latest, lookback_days)
    started = time.monotonic()
    items: list[RawItem] = []
    rows_seen = 0
    # URL-level dedup inside GDELT itself: one article routinely generates
    # many event rows; keep the first (dedup_exact in run.py re-checks
    # across sources anyway, this just keeps the intermediate list small).
    seen_urls: set[str] = set()

    with ThreadPoolExecutor(max_workers=GDELT_DOWNLOAD_WORKERS) as pool:
        for rows in pool.map(_fetch_gdelt_export_file, stamps):
            rows_seen += len(rows)
            for row in rows:
                item = _gdelt_row_to_item(
                    row, actor_codes, geo_codes, min_mentions, min_abs_goldstein, fallback_date_iso
                )
                if item is None or item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                items.append(item)

    logger.info(
        "GDELT: %d export files scanned (%d rows) -> %d matched items in %.1fs",
        len(stamps),
        rows_seen,
        len(items),
        time.monotonic() - started,
    )
    return items


def _entry_published_iso(entry) -> tuple[str, datetime | None]:
    """Return (iso_string, datetime) for a feedparser entry, falling back to
    the raw feed string (and None) when no parseable date is provided —
    dates are normalized to ISO at ingest so the events table and prompts
    never see mixed formats."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            dt = datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            return dt.isoformat(), dt
        except (ValueError, OverflowError):
            pass
    raw = entry.get("published", "") or entry.get("updated", "")
    return raw or datetime.now(timezone.utc).isoformat(), None


def fetch_rss(
    name: str,
    url: str,
    tier: int | None = None,
    timeout: float = 15.0,
    max_age_days: int | None = None,
) -> list[RawItem]:
    """Fetch and parse a single RSS feed. Logs and returns [] on failure so
    one dead feed doesn't take down the whole ingestion run. `tier` (spec
    5.1's source reliability tiering) is carried through in raw_metadata so
    the analysis prompt can apply the tiering rule.

    Entries older than `max_age_days` are dropped at ingest (entries with
    no parseable date are kept — fail open, triage handles them): most
    feeds serve their full recent archive on every request, and without a
    cutoff a daily run keeps re-processing last week's items forever."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=RSS_REQUEST_HEADERS)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception:
        logger.exception("Failed to fetch RSS feed %s (%s)", name, url)
        return []

    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    items: list[RawItem] = []
    for entry in parsed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        if not title or not link:
            continue
        published_iso, published_dt = _entry_published_iso(entry)
        if cutoff is not None and published_dt is not None and published_dt < cutoff:
            continue
        items.append(
            RawItem(
                title=title,
                source=name,
                url=link,
                published=published_iso,
                text=_truncate_words(f"{title}. {summary}"),
                raw_metadata={"tier": tier} if tier is not None else {},
            )
        )
    if not items:
        logger.warning("RSS feed %s returned 0 usable items — dead feed, or everything aged out", name)
    return items


def fetch_all_rss(cfg: dict) -> list[RawItem]:
    max_age_days = cfg.get("rss_max_age_days")
    items: list[RawItem] = []
    for feed in cfg.get("rss_feeds", []):
        items.extend(fetch_rss(feed["name"], feed["url"], tier=feed.get("tier"), max_age_days=max_age_days))
    return items
