"""Stage 1 (ingest) orchestrator for the Middle East agent — pulls from
GDELT, LiveUAMap, and the narrative RSS layer, and returns one flat list of
RawItem for Stage 2 (dedup/triage) to consume.
"""
from __future__ import annotations

import logging

from agents.middle_east.sources import (
    RawItem,
    fetch_all_rss,
    fetch_gdelt,
    fetch_liveuamap,
    load_sources_config,
)

logger = logging.getLogger(__name__)


def run_ingest() -> list[RawItem]:
    cfg = load_sources_config()

    items: list[RawItem] = []

    gdelt_items = fetch_gdelt(cfg.get("gdelt", {}))
    logger.info("GDELT: %d items", len(gdelt_items))
    items.extend(gdelt_items)

    liveuamap_items = fetch_liveuamap(cfg.get("liveuamap", {}))
    logger.info("LiveUAMap: %d items", len(liveuamap_items))
    items.extend(liveuamap_items)

    rss_items = fetch_all_rss(cfg)
    logger.info("RSS: %d items", len(rss_items))
    items.extend(rss_items)

    logger.info("Total raw items ingested: %d", len(items))
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_ingest()
    print(f"Ingested {len(result)} raw items")
    for item in result[:5]:
        print(f"- [{item.source}] {item.title}")
