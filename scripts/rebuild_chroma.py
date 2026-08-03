"""One-time Chroma rebuild for the shared-embedder upgrade.

The August 2026 optimization pass removed Chroma's own embedding function
(store/chroma_client.py now takes caller-supplied vectors from the single
shared model in pipeline/dedup.py). A data/chroma directory created before
that change stores the old embedding-function config and must be deleted —
but the events themselves live in SQLite, so nothing is lost: this script
re-embeds every event row into a fresh collection.

Usage:
    rm -rf data/chroma
    python scripts/rebuild_chroma.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.dedup import default_embed_fn  # noqa: E402
from pipeline.logging_setup import configure_logging  # noqa: E402
from store import chroma_client as cc  # noqa: E402
from store import sqlite_client as db  # noqa: E402

configure_logging()
logger = logging.getLogger("rebuild_chroma")

DOMAIN = "middle_east"
ROOT = Path(__file__).parent.parent
SQLITE_PATH = ROOT / "data" / f"{DOMAIN}.sqlite3"
CHROMA_DIR = ROOT / "data" / "chroma"

BATCH = 200


def main() -> int:
    if not SQLITE_PATH.exists():
        logger.info("No SQLite store at %s — nothing to rebuild.", SQLITE_PATH)
        return 0

    conn = db.connect(str(SQLITE_PATH))
    rows = conn.execute(
        "SELECT id, date, description, event_type FROM events WHERE domain = ? ORDER BY id",
        (DOMAIN,),
    ).fetchall()
    if not rows:
        logger.info("No events in SQLite — a fresh Chroma directory will fill up from future runs.")
        return 0

    client = cc.get_client(str(CHROMA_DIR))
    collection = cc.get_domain_collection(client, DOMAIN)

    total = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        ids = [f"{DOMAIN}_{r['id']}" for r in batch]
        summaries = [r["description"] for r in batch]
        metadatas = [
            {"date": r["date"], "event_type": r["event_type"] or "unspecified", "sqlite_event_id": r["id"]}
            for r in batch
        ]
        cc.add_events(collection, ids, summaries, default_embed_fn(summaries), metadatas)
        total += len(batch)
        logger.info("Re-embedded %d/%d events", total, len(rows))

    conn.close()
    logger.info("Done: %d events rebuilt into %s (collection count: %d)", total, CHROMA_DIR, collection.count())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
