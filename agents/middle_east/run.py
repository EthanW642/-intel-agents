"""Full 5-stage pipeline run for the Middle East agent (Phase 1 scope).

INGEST -> DEDUP/TRIAGE -> MEMORY QUERY -> DEEP ANALYSIS -> WRITE-BACK/RENDER
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agents.middle_east.ingest import run_ingest
from pipeline.analyze import run_analysis
from pipeline.dedup import dedup_items
from pipeline.memory import init_store, query_memory, write_back
from pipeline.render import render_briefing
from pipeline.triage import triage_items

logger = logging.getLogger(__name__)

DOMAIN = "middle_east"
ROOT = Path(__file__).parent.parent.parent
WATCHLIST_PATH = ROOT / "config" / "watchlists.yaml"
SQLITE_PATH = ROOT / "data" / f"{DOMAIN}.sqlite3"
CHROMA_DIR = ROOT / "data" / "chroma"


def run() -> Path | None:
    load_dotenv()
    watchlist_cfg = yaml.safe_load(WATCHLIST_PATH.read_text())
    pipeline_cfg = watchlist_cfg["pipeline"]

    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn, collection = init_store(DOMAIN, str(SQLITE_PATH), str(CHROMA_DIR), watchlist_cfg)

    logger.info("Stage 1: ingest")
    raw_items = run_ingest()
    if not raw_items:
        logger.warning("No raw items ingested this run (check network/source config)")

    logger.info("Stage 2a: dedup")
    deduped = dedup_items(raw_items, similarity_threshold=pipeline_cfg["dedup_similarity_threshold"])

    logger.info("Stage 2b: triage")
    entities = [dict(row) for row in conn.execute("SELECT * FROM entities WHERE domain = ?", (DOMAIN,)).fetchall()]
    theses = [dict(row) for row in conn.execute("SELECT * FROM theses WHERE domain = ?", (DOMAIN,)).fetchall()]
    triaged = triage_items(
        deduped,
        entities,
        theses,
        ollama_host=pipeline_cfg["ollama_host"],
        model=pipeline_cfg["triage_model"],
        score_threshold=pipeline_cfg["triage_score_threshold"],
    )

    logger.info("Stage 3: memory query")
    memory_context = query_memory(conn, collection, DOMAIN, triaged, pipeline_cfg["memory_retrieval_count"])

    logger.info("Stage 4: deep analysis (Sonnet)")
    result = run_analysis(
        triaged,
        memory_context,
        domain=DOMAIN,
        model=pipeline_cfg["analysis_model"],
        max_tokens=pipeline_cfg["analysis_max_tokens"],
    )

    logger.info("Stage 5: write-back + render")
    from datetime import date

    write_back(conn, collection, DOMAIN, result["write_back"], date.today().isoformat())
    briefing_path = render_briefing(DOMAIN, result["markdown"])

    conn.close()
    logger.info("Run complete. Briefing at %s. Cost: $%.4f", briefing_path, result["cost_usd"])
    return briefing_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
