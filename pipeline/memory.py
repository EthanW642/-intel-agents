"""Stage 3 (memory query) and Stage 5 (write-back) — spec section 3.

Stage 3, before analysis: query Chroma for semantically related past events
and pull the domain's active theses + entity graph from SQLite, so the
Sonnet call gets real continuity instead of starting from zero each day.
Also runs the thesis dormancy sweep (mechanical, no LLM) and builds the
prediction track-record summary (spec 3.C) when there's enough resolved
history for it to be meaningful.

Stage 5, after analysis: take the structured JSON block Sonnet is required
to produce and persist it — new/updated entities, relationships, events,
thesis status changes, and new dated predictions — to both SQLite and
Chroma. New-thesis entries are validated against the 2-independent-event
bar (spec 3.B) here, not just trusted from the prompt: an entry that
doesn't cite >= 2 distinct supporting events is logged and dropped rather
than persisted.

Expected shape of the analysis JSON write-back block (see
prompts/analysis_system.md for the authoritative contract):
{
  "new_entities": [{"name": str, "type": str, "notes": str}],
  "new_relationships": [{"entity_a": str, "entity_b": str, "type": str, "description": str}],
  "new_events": [{"date": str, "description": str, "event_type": str,
                  "confidence": float, "source_urls": [str], "entities": [str]}],
  "thesis_updates": [
    {"title": str, "status": "reinforced|complicated|falsified", "note": str},
    {"title": str, "status": "active", "statement": str, "new_thesis": true,
     "supporting_events": [str, str, ...]}  # >= 2 required
  ],
  "new_predictions": [{"claim": str, "target_date": str | None, "source_note": str}]
}
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

from store import chroma_client as cc
from store import sqlite_client as db

logger = logging.getLogger(__name__)

MIN_SUPPORTING_EVENTS_FOR_NEW_THESIS = 2


def init_store(domain: str, sqlite_path: str, chroma_dir: str, watchlist_cfg: dict):
    conn = db.connect(sqlite_path)
    db.seed_entities(conn, domain, watchlist_cfg.get("entities", []))
    db.seed_theses(conn, domain, watchlist_cfg.get("standing_theses", []))

    chroma = cc.get_client(chroma_dir)
    collection = cc.get_domain_collection(chroma, domain)
    return conn, collection


def sweep_dormant_theses(
    conn: sqlite3.Connection, domain: str, window_days: int, run_date: date
) -> list[str]:
    """Mechanical dormancy check (spec 3.B) — no LLM involved. An
    active/reinforced/complicated thesis with no evidence-log entry (or,
    for a thesis never reinforced since creation, no activity since
    creation) inside `window_days` moves to dormant. Returns the titles
    moved, for logging."""
    moved: list[str] = []
    for row in db.get_active_theses(conn, domain):
        thesis = dict(row)
        evidence_log = thesis.get("evidence_log")
        last_activity_str = thesis["created_at"]
        if evidence_log:
            import json as _json

            entries = _json.loads(evidence_log)
            if entries:
                last_activity_str = entries[-1]["date"]
        try:
            last_activity = date.fromisoformat(last_activity_str[:10])
        except ValueError:
            last_activity = datetime.fromisoformat(last_activity_str).date()
        if (run_date - last_activity) > timedelta(days=window_days):
            db.set_thesis_dormant(conn, domain, thesis["title"])
            moved.append(thesis["title"])
    if moved:
        logger.info("Dormancy sweep: moved %d thesis(es) to dormant: %s", len(moved), moved)
    return moved


def build_track_record_summary(conn: sqlite3.Connection, domain: str, min_count: int) -> str | None:
    """Spec 3.C: a short calibration string ("6 of your last 10 dated
    predictions confirmed, 2 contradicted, 2 pending") for Call 2's prompt.
    Returns None when there isn't enough resolved history yet — the prompt
    must omit the summary entirely in that case rather than force a
    misleadingly small sample into it (spec 5)."""
    resolved = [dict(row) for row in db.get_recent_resolved_predictions(conn, domain, limit=10)]
    if len(resolved) < min_count:
        return None
    confirmed = sum(1 for p in resolved if p["status"] == "confirmed")
    contradicted = sum(1 for p in resolved if p["status"] == "contradicted")
    pending = len(db.get_pending_predictions(conn, domain))
    total = len(resolved) + pending
    return (
        f"Of your last {total} dated predictions (most recent {len(resolved)} resolved): "
        f"{confirmed} confirmed, {contradicted} contradicted, {pending} pending."
    )


def query_memory(
    conn: sqlite3.Connection,
    collection,
    domain: str,
    triaged_items: list,
    n_results: int,
    dormancy_window_days: int,
    predictions_min_for_track_record: int,
    embed_fn,
    run_date: date | None = None,
) -> dict:
    """Retrieve the context the analysis stage needs: related past events
    (Chroma), active theses, the current entity graph (SQLite), and the
    prediction track record — after running the mechanical dormancy sweep
    so active_theses reflects today's true state.

    `embed_fn` is the shared embedder from pipeline.dedup — Chroma no
    longer owns its own copy of the model."""
    run_date = run_date or date.today()
    sweep_dormant_theses(conn, domain, dormancy_window_days, run_date)

    query_text = " ".join(f"{item.title}. {item.text}" for item in triaged_items)[:4000]
    related_events = (
        cc.query_related(collection, embed_fn([query_text])[0], n_results) if query_text else []
    )

    active_theses = [dict(row) for row in db.get_active_theses(conn, domain)]
    entities = [dict(row) for row in db.get_entities(conn, domain)]
    track_record = build_track_record_summary(conn, domain, predictions_min_for_track_record)

    return {
        "related_past_events": related_events,
        "active_theses": active_theses,
        "entity_graph": entities,
        "track_record_summary": track_record,
    }


def write_back(
    conn: sqlite3.Connection, collection, domain: str, analysis_json: dict, run_date: str, embed_fn
) -> dict:
    """Persist Sonnet's structured JSON write-back block to SQLite + Chroma.
    Returns a summary dict of what was written, for the run log.

    Unlike the Ollama calls (triage, prediction resolution), Sonnet's JSON
    block has no schema-enforced structure — it's prompt-following inside a
    larger prose response, so a missing/malformed field on any one entry is
    plausible. Each per-item loop below is individually guarded: a
    malformed entry is logged and skipped, not allowed to crash the whole
    write-back — losing an already-paid-for day's entire briefing/email
    over one bad entity/event/relationship would be far worse than losing
    just that one entry.
    """
    entity_name_to_id: dict[str, int] = {}
    for row in db.get_entities(conn, domain):
        entity_name_to_id[row["name"]] = row["id"]

    malformed_skipped = 0

    def resolve_entity(name: str) -> int | None:
        if name in entity_name_to_id:
            return entity_name_to_id[name]
        eid = db.upsert_entity(conn, domain, name, "unknown", "")
        entity_name_to_id[name] = eid
        return eid

    new_entities_written = 0
    for ent in analysis_json.get("new_entities", []):
        try:
            eid = db.upsert_entity(conn, domain, ent["name"], ent.get("type", "unknown"), ent.get("notes", ""))
            entity_name_to_id[ent["name"]] = eid
            new_entities_written += 1
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed new_entities entry (%s): %r", exc, ent)
            malformed_skipped += 1

    new_relationships_written = 0
    for rel in analysis_json.get("new_relationships", []):
        try:
            a_id = resolve_entity(rel["entity_a"])
            b_id = resolve_entity(rel["entity_b"])
            db.upsert_relationship(conn, domain, a_id, b_id, rel["type"], rel.get("description", ""))
            new_relationships_written += 1
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed new_relationships entry (%s): %r", exc, rel)
            malformed_skipped += 1

    new_events_written = 0
    chroma_ids: list[str] = []
    chroma_summaries: list[str] = []
    chroma_metadatas: list[dict] = []
    for evt in analysis_json.get("new_events", []):
        try:
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
            chroma_ids.append(f"{domain}_{event_id}")
            chroma_summaries.append(evt["description"])
            chroma_metadatas.append(
                {"date": evt["date"], "event_type": evt.get("event_type", "unspecified"), "sqlite_event_id": event_id}
            )
            new_events_written += 1
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed new_events entry (%s): %r", exc, evt)
            malformed_skipped += 1

    if chroma_ids:
        # One batched encode + upsert with the shared embedder, instead of
        # per-event calls through a second resident model.
        cc.add_events(collection, chroma_ids, chroma_summaries, embed_fn(chroma_summaries), chroma_metadatas)

    thesis_updates_applied = 0
    thesis_updates_rejected = 0
    for update in analysis_json.get("thesis_updates", []):
        try:
            title = update["title"]
            status = update.get("status", "active")
            note = update.get("note", "")
            if update.get("new_thesis"):
                supporting = update.get("supporting_events") or []
                # Distinct-supporting-event bar (spec 3.B) enforced here, not
                # just trusted from the prompt: reject rather than persist a
                # thesis promoted off fewer than 2 independent events.
                if len(set(supporting)) < MIN_SUPPORTING_EVENTS_FOR_NEW_THESIS:
                    logger.warning(
                        "Rejecting new thesis '%s': only %d distinct supporting event(s) cited "
                        "(need >= %d) — logging as evidence only, not promoting to a thesis.",
                        title,
                        len(set(supporting)),
                        MIN_SUPPORTING_EVENTS_FOR_NEW_THESIS,
                    )
                    thesis_updates_rejected += 1
                    continue
                db.insert_or_update_thesis(
                    conn, domain, title, update.get("statement", ""), status, run_date, supporting
                )
                thesis_updates_applied += 1
            elif "statement" in update:
                db.insert_or_update_thesis(conn, domain, title, update["statement"], status, run_date)
                thesis_updates_applied += 1
            else:
                db.update_thesis_status(conn, domain, title, status, note, run_date)
                thesis_updates_applied += 1
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed thesis_updates entry (%s): %r", exc, update)
            malformed_skipped += 1

    new_predictions_written = 0
    for pred in analysis_json.get("new_predictions", []):
        try:
            claim = pred.get("claim")
            if not claim:
                continue
            db.insert_prediction(
                conn,
                domain,
                claim,
                date_made=run_date,
                target_date=pred.get("target_date") or None,
                source_run_date=run_date,
            )
            new_predictions_written += 1
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed new_predictions entry (%s): %r", exc, pred)
            malformed_skipped += 1

    logger.info(
        "Write-back complete: %d new entities, %d relationships, %d events, "
        "%d thesis updates applied (%d rejected for the 2-event bar), %d new predictions"
        "%s",
        new_entities_written,
        new_relationships_written,
        new_events_written,
        thesis_updates_applied,
        thesis_updates_rejected,
        new_predictions_written,
        f" ({malformed_skipped} malformed entries skipped — check warnings above)" if malformed_skipped else "",
    )

    return {
        "new_entities": new_entities_written,
        "new_relationships": new_relationships_written,
        "new_events": new_events_written,
        "thesis_updates_applied": thesis_updates_applied,
        "thesis_updates_rejected": thesis_updates_rejected,
        "new_predictions": new_predictions_written,
        "malformed_entries_skipped": malformed_skipped,
    }
