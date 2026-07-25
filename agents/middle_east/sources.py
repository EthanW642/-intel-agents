"""Stage 1 (ingest) source fetchers for the Middle East agent.

Every fetcher returns a list of RawItem — the common shape the rest of the
pipeline (dedup -> triage -> memory -> analyze) operates on, regardless of
which structured feed or RSS source an item came from.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import feedparser
import httpx
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "sources.yaml"

# Some feeds (LiveUAMap confirmed; likely others) 403 requests that don't
# look like a browser — no default User-Agent, no Accept header. This is
# the single source of truth for those headers; scripts/verify_sources.py
# imports it too, so the pre-flight check and real ingestion never drift
# apart on this.
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
    # script hit. If this still doesn't clear it, the wall is likely doing
    # something header-based fixes can't solve (a JS/Cloudflare challenge)
    # — see the LiveUAMap section of config/sources.yaml.
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


GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


def _gdelt_latest_available_date(timeout: float = 15.0) -> date | None:
    """Ask GDELT's own live feed what its most recent available date
    actually is, rather than trusting the local system clock at all.

    Confirmed necessary 2026-07-24: switching the local date computation
    from UTC to local time (an earlier, wrong diagnosis) did NOT fix
    gdeltPyR's "One of your dates is greater than the current date" error,
    because the real cause isn't a timezone mismatch — it's that this
    machine's system clock reads dates GDELT's real service (running in
    true wall-clock time) doesn't have data for yet. No local date
    computation, in any timezone, can be correct here; the only reliable
    source of "what's the latest real GDELT date" is GDELT itself. Falls
    back to the local date if this live check fails for any reason
    (network, unexpected response shape) so a GDELT outage degrades
    gracefully instead of hard-failing ingestion outright.
    """
    try:
        resp = httpx.get(GDELT_LASTUPDATE_URL, timeout=timeout)
        resp.raise_for_status()
        match = re.search(r"(\d{14})\.export\.CSV", resp.text)
        if match:
            return datetime.strptime(match.group(1)[:8], "%Y%m%d").date()
    except Exception:
        logger.warning(
            "Could not determine GDELT's latest available date from %s; falling back to local date",
            GDELT_LASTUPDATE_URL,
        )
    return None


def fetch_gdelt(cfg: dict) -> list[RawItem]:
    """Pull GDELT 2.0 events for the region via the gdeltPyR package and
    filter to Middle East actor/geo country codes (spec 4.1)."""
    try:
        import gdelt
    except ImportError:
        logger.warning("gdelt package not installed, skipping GDELT ingestion")
        return []

    lookback_days = cfg.get("lookback_days", 1)
    actor_codes = set(cfg.get("actor_country_codes", []))
    geo_codes = set(cfg.get("geo_country_codes", []))
    min_mentions = cfg.get("min_num_mentions", 0)
    min_abs_goldstein = cfg.get("min_abs_goldstein", 0)

    end = _gdelt_latest_available_date() or datetime.now().date()
    start = end - timedelta(days=lookback_days)
    date_range = [start.strftime("%Y %m %d"), end.strftime("%Y %m %d")]

    try:
        gd = gdelt.gdelt(version=2)
        df = gd.Search(date_range, table=cfg.get("table", "events"), coverage=True, output="df")
    except Exception:
        logger.exception("GDELT fetch failed")
        return []

    if df is None or len(df) == 0:
        return []

    def row_matches(row) -> bool:
        actor_match = row.get("Actor1CountryCode") in actor_codes or row.get(
            "Actor2CountryCode"
        ) in actor_codes
        geo_match = row.get("ActionGeo_CountryCode") in geo_codes
        return actor_match or geo_match

    items: list[RawItem] = []
    for _, row in df.iterrows():
        try:
            # Kept if EITHER threshold clears (spec 4.1) — a low-mention but
            # high-magnitude event (a breaking strike, before wire pickup
            # accumulates) shouldn't be dropped just because NumMentions is
            # still low, and vice versa.
            mentions = row.get("NumMentions", 0) or 0
            goldstein = row.get("GoldsteinScale")
            goldstein_ok = goldstein is not None and abs(goldstein) >= min_abs_goldstein
            if mentions < min_mentions and not goldstein_ok:
                continue
            if not row_matches(row):
                continue
            url = row.get("SOURCEURL", "")
            if not url:
                continue
            a1 = row.get("Actor1Name") or "unknown actor"
            a2 = row.get("Actor2Name") or "unknown actor"
            event_code = row.get("EventCode", "")
            tone = row.get("AvgTone", 0)
            desc = (
                f"GDELT event: {a1} -> {a2} (CAMEO {event_code}), "
                f"Goldstein={row.get('GoldsteinScale')}, tone={tone}, "
                f"mentions={row.get('NumMentions')}"
            )
            published = str(row.get("SQLDATE", end.strftime("%Y%m%d")))
            items.append(
                RawItem(
                    title=desc,
                    source="GDELT",
                    url=url,
                    published=published,
                    text=desc,
                    # Tier 1 (structured/primary) per spec 5.1 — GDELT is the
                    # structured event-occurrence source.
                    raw_metadata={"event_code": event_code, "goldstein": row.get("GoldsteinScale"), "tier": 1},
                )
            )
        except Exception:
            logger.exception("Failed to parse a GDELT row, skipping it")
            continue
    return items


def fetch_rss(name: str, url: str, tier: int | None = None, timeout: float = 15.0) -> list[RawItem]:
    """Fetch and parse a single RSS feed. Logs and returns [] on failure so
    one dead feed doesn't take down the whole ingestion run. `tier` (spec
    5.1's source reliability tiering) is carried through in raw_metadata so
    the analysis prompt can apply the tiering rule."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=RSS_REQUEST_HEADERS)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception:
        logger.exception("Failed to fetch RSS feed %s (%s)", name, url)
        return []

    items: list[RawItem] = []
    for entry in parsed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        published = entry.get("published", "") or entry.get("updated", "")
        if not title or not link:
            continue
        items.append(
            RawItem(
                title=title,
                source=name,
                url=link,
                published=published,
                text=_truncate_words(f"{title}. {summary}"),
                raw_metadata={"tier": tier} if tier is not None else {},
            )
        )
    return items


def fetch_liveuamap(cfg: dict) -> list[RawItem]:
    return fetch_rss("LiveUAMap Israel-Palestine", cfg["feed_url"], tier=cfg.get("tier", 4))


def fetch_all_rss(cfg: dict) -> list[RawItem]:
    items: list[RawItem] = []
    for feed in cfg.get("rss_feeds", []):
        items.extend(fetch_rss(feed["name"], feed["url"], tier=feed.get("tier")))
    return items
