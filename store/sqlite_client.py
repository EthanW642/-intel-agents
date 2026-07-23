"""SQLite structured store (spec section 3.B): entities, events,
relationships, theses, and the api_call_log used for cost tracking.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def seed_entities(conn: sqlite3.Connection, domain: str, entities: list[dict]) -> None:
    ts = now_iso()
    for ent in entities:
        conn.execute(
            """
            INSERT INTO entities (domain, name, type, notes, first_seen, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, name) DO NOTHING
            """,
            (domain, ent["name"], ent["type"], ent.get("notes", ""), ts, ts),
        )
    conn.commit()


def seed_theses(conn: sqlite3.Connection, domain: str, theses: list[dict]) -> None:
    ts = now_iso()
    for t in theses:
        conn.execute(
            """
            INSERT INTO theses (domain, title, statement, status, evidence_log, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, title) DO NOTHING
            """,
            (domain, t["title"], t["statement"], t.get("status", "active"), "[]", ts, ts),
        )
    conn.commit()


def get_active_theses(conn: sqlite3.Connection, domain: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM theses WHERE domain = ? AND status = 'active'", (domain,)
    ).fetchall()


def get_entities(conn: sqlite3.Connection, domain: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM entities WHERE domain = ?", (domain,)).fetchall()


def upsert_entity(conn: sqlite3.Connection, domain: str, name: str, type_: str, notes: str = "") -> int:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO entities (domain, name, type, notes, first_seen, last_updated)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain, name) DO UPDATE SET
            type = excluded.type,
            notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE entities.notes END,
            last_updated = excluded.last_updated
        """,
        (domain, name, type_, notes, ts, ts),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM entities WHERE domain = ? AND name = ?", (domain, name)
    ).fetchone()
    return row["id"]


def insert_event(
    conn: sqlite3.Connection,
    domain: str,
    date: str,
    description: str,
    event_type: str,
    confidence: float,
    source_urls: list[str],
    entity_ids: list[int],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (domain, date, description, event_type, confidence, source_urls, entity_ids, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            domain,
            date,
            description,
            event_type,
            confidence,
            json.dumps(source_urls),
            json.dumps(entity_ids),
            now_iso(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def upsert_relationship(
    conn: sqlite3.Connection,
    domain: str,
    entity_a_id: int,
    entity_b_id: int,
    relationship_type: str,
    description: str,
    status: str = "active",
) -> int:
    ts = now_iso()
    existing = conn.execute(
        """
        SELECT id FROM relationships
        WHERE domain = ? AND entity_a_id = ? AND entity_b_id = ? AND relationship_type = ?
        """,
        (domain, entity_a_id, entity_b_id, relationship_type),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE relationships SET description = ?, status = ?, last_updated = ? WHERE id = ?",
            (description, status, ts, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO relationships (domain, entity_a_id, entity_b_id, relationship_type, description, status, first_seen, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (domain, entity_a_id, entity_b_id, relationship_type, description, status, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def update_thesis_status(
    conn: sqlite3.Connection, domain: str, title: str, status: str, evidence_note: str, date: str
) -> None:
    row = conn.execute(
        "SELECT evidence_log FROM theses WHERE domain = ? AND title = ?", (domain, title)
    ).fetchone()
    if row is None:
        return
    log = json.loads(row["evidence_log"] or "[]")
    log.append({"date": date, "note": evidence_note})
    conn.execute(
        "UPDATE theses SET status = ?, evidence_log = ?, updated_at = ? WHERE domain = ? AND title = ?",
        (status, json.dumps(log), now_iso(), domain, title),
    )
    conn.commit()


def insert_or_update_thesis(
    conn: sqlite3.Connection, domain: str, title: str, statement: str, status: str, date: str
) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO theses (domain, title, statement, status, evidence_log, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain, title) DO UPDATE SET
            statement = excluded.statement,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (domain, title, statement, status, json.dumps([{"date": date, "note": "created/updated by analysis"}]), ts, ts),
    )
    conn.commit()


def log_api_call(
    conn: sqlite3.Connection,
    domain: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    estimated_cost_usd: float,
) -> None:
    conn.execute(
        """
        INSERT INTO api_call_log (domain, timestamp, model, input_tokens, output_tokens, thinking_tokens, estimated_cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (domain, now_iso(), model, input_tokens, output_tokens, thinking_tokens, estimated_cost_usd),
    )
    conn.commit()
