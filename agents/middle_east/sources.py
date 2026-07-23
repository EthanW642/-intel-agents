"""Stage 1 (ingest) source fetchers for the Middle East agent.

Every fetcher returns a list of RawItem — the common shape the rest of the
pipeline (dedup -> triage -> memory -> analyze) operates on, regardless of
which structured feed or RSS source an item came from.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "sources.yaml"


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

    end = datetime.now(timezone.utc).date()
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
            if row.get("NumMentions", 0) < min_mentions:
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
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
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
