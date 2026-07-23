"""Stage 3 (memory query) and Stage 5 (write-back) — spec section 3.

Stage 3, before analysis: query Chroma for semantically related past events
and pull the domain's active theses + entity graph from SQLite, so the
Sonnet call gets real continuity instead of starting from zero each day.

Stage 5, after analysis: take the structured JSON block Sonnet is required
to produce and persist it — new/updated entities, relationships, events,
and thesis status changes — to both SQLite and Chroma.

Expected shape of the analysis JSON write-back block:
{
  "new_entities": [{"name": str, "type": str, "notes": str}],
  "new_relationships": [{"entity_a": str, "entity_b": str, "type": str, "description": str}],
  "new_events": [{"date": str, "description": str, "event_type": str,
                  "confidence": float, "source_urls": [str], "entities": [str]}],
  "thesis_updates": [{"title": str, "status": "active|confirmed|falsified",
                       "note": str, "statement": str (optional, only if revised)}]
}
"""
from __future__ import annotations

import logging
import sqlite3

from store import chroma_client as cc
from store import sqlite_client as db

logger = logging.getLogger(__name__)


def init_store(domain: str, sqlite_path: str, chroma_dir: str, watchlist_cfg: dict):
    conn = db.connect(sqlite_path)
    db.seed_entities(conn, domain, watchlist_cfg.get("entities", []))
    db.seed_theses(conn, domain, watchlist_cfg.get("standing_theses", []))

    chroma = cc.get_client(chroma_dir)
    collection = cc.get_domain_collection(chroma, domain)
    return conn, collection


def query_memory(
    conn: sqlite3.Connection,
    collection,
    domain: str,
    triaged_items: list,
    n_results: int,
) -> dict:
    """Retrieve the context the analysis stage needs: related past events
    (Chroma), active theses, and the current entity graph (SQLite)."""
    query_text = " ".join(f"{item.title}. {item.text}" for item in triaged_items)[:4000]
    related_events = cc.query_related(collection, query_text, n_results) if query_text else []

    active_theses = [dict(row) for row in db.get_active_theses(conn, domain)]
    entities = [dict(row) for row in db.get_entities(conn, domain)]

    return {
        "related_past_events": related_events,
        "active_theses": active_theses,
        "entity_graph": entities,
    }


def write_back(conn: sqlite3.Connection, collection, domain: str, analysis_json: dict, run_date: str) -> None:
    """Persist Sonnet's structured JSON write-back block to SQLite + Chroma."""
    entity_name_to_id: dict[str, int] = {}
    for row in db.get_entities(conn, domain):
        entity_name_to_id[row["name"]] = row["id"]

    for ent in analysis_json.get("new_entities", []):
        eid = db.upsert_entity(conn, domain, ent["name"], ent.get("type", "unknown"), ent.get("notes", ""))
        entity_name_to_id[ent["name"]] = eid

    def resolve_entity(name: str) -> int | None:
        if name in entity_name_to_id:
            return entity_name_to_id[name]
        eid = db.upsert_entity(conn, domain, name, "unknown", "")
        entity_name_to_id[name] = eid
        return eid

    for rel in analysis_json.get("new_relationships", []):
        a_id = resolve_entity(rel["entity_a"])
        b_id = resolve_entity(rel["entity_b"])
        db.upsert_relationship(conn, domain, a_id, b_id, rel["type"], rel.get("description", ""))

    for evt in analysis_json.get("new_events", []):
        entity_ids = [resolve_entity(name) for name in evt.get("entities", [])]
        event_id = db.insert_event(
            conn,
            domain,
            evt["date"],
            evt["description"],
            evt.get("event_type", "unspecified"),
            float(evt.get("confidence", 0.5)),
            evt.get("source_urls", []),
            entity_ids,
        )
        cc.add_event(
            collection,
            event_id=f"{domain}_{event_id}",
            summary=evt["description"],
            metadata={"date": evt["date"], "event_type": evt.get("event_type", "unspecified"), "sqlite_event_id": event_id},
        )

    for update in analysis_json.get("thesis_updates", []):
        title = update["title"]
        status = update.get("status", "active")
        note = update.get("note", "")
        if "statement" in update:
            db.insert_or_update_thesis(conn, domain, title, update["statement"], status, run_date)
        else:
            db.update_thesis_status(conn, domain, title, status, note, run_date)

    logger.info(
        "Write-back complete: %d new entities, %d relationships, %d events, %d thesis updates",
        len(analysis_json.get("new_entities", [])),
        len(analysis_json.get("new_relationships", [])),
        len(analysis_json.get("new_events", [])),
        len(analysis_json.get("thesis_updates", [])),
    )
