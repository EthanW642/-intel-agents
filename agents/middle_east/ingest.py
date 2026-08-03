"""Stage 1 (ingest) orchestrator for the Middle East agent — pulls from
GDELT and the RSS layer (narrative outlets plus a Google News search query
standing in for LiveUAMap's old rapid/uncorroborated Tier 4 role — see
config/sources.yaml), and returns one flat list of RawItem for Stage 2
(dedup/triage) to consume.
"""
from __future__ import annotations

import logging

from agents.middle_east.sources import (
    RawItem,
    fetch_all_rss,
    fetch_gdelt,
    load_sources_config,
)
from pipeline.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def run_ingest() -> list[RawItem]:
    cfg = load_sources_config()

    items: list[RawItem] = []

    gdelt_items = fetch_gdelt(cfg.get("gdelt", {}))
    logger.info("GDELT: %d items", len(gdelt_items))
    items.extend(gdelt_items)

    rss_items = fetch_all_rss(cfg)
    logger.info("RSS: %d items", len(rss_items))
    items.extend(rss_items)

    logger.info("Total raw items ingested: %d", len(items))
    return items


if __name__ == "__main__":
    configure_logging()
    result = run_ingest()
    print(f"Ingested {len(result)} raw items")
    for item in result[:5]:
        print(f"- [{item.source}] {item.title}")
