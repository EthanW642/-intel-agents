from datetime import date, timedelta

import pytest

from store import sqlite_client as db
from pipeline import memory


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


class FakeChromaCollection:
    def __init__(self):
        self.upserted = []

    def upsert(self, ids, documents, embeddings, metadatas):
        self.upserted.append((ids, documents, embeddings, metadatas))

    def count(self):
        return len(self.upserted)

    def query(self, query_embeddings, n_results):
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


def fake_embed(texts):
    # Deterministic stand-in for the shared sentence-transformers embedder.
    return [[0.1, 0.2, 0.3] for _ in texts]


# ---- dormancy sweep ----


def test_dormancy_sweep_moves_stale_thesis_to_dormant(conn):
    db.insert_or_update_thesis(conn, "middle_east", "Old thesis", "statement", "active", "2026-01-01")
    row = conn.execute("SELECT * FROM theses WHERE title = ?", ("Old thesis",)).fetchone()
    # Backdate created_at so the thesis looks old with no evidence.
    conn.execute("UPDATE theses SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", row["id"]))
    conn.commit()

    moved = memory.sweep_dormant_theses(conn, "middle_east", window_days=14, run_date=date(2026, 3, 1))

    assert "Old thesis" in moved
    updated = conn.execute("SELECT status FROM theses WHERE title = ?", ("Old thesis",)).fetchone()
    assert updated["status"] == "dormant"


def test_dormancy_sweep_leaves_recent_thesis_alone(conn):
    db.insert_or_update_thesis(conn, "middle_east", "Fresh thesis", "statement", "active", "2026-03-01")
    moved = memory.sweep_dormant_theses(conn, "middle_east", window_days=14, run_date=date(2026, 3, 5))
    assert moved == []
    row = conn.execute("SELECT status FROM theses WHERE title = ?", ("Fresh thesis",)).fetchone()
    assert row["status"] == "active"


def test_dormancy_sweep_respects_evidence_log_activity(conn):
    db.insert_or_update_thesis(conn, "middle_east", "Reinforced thesis", "statement", "active", "2026-01-01")
    row = conn.execute("SELECT id FROM theses WHERE title = ?", ("Reinforced thesis",)).fetchone()
    conn.execute(
        "UPDATE theses SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", row["id"])
    )
    conn.commit()
    # Reinforced recently -> evidence_log has a fresh date, should NOT go dormant
    db.update_thesis_status(conn, "middle_east", "Reinforced thesis", "reinforced", "new evidence", "2026-02-25")

    moved = memory.sweep_dormant_theses(conn, "middle_east", window_days=14, run_date=date(2026, 3, 1))
    assert moved == []


# ---- 2-event-bar write-back validation ----


def test_write_back_rejects_new_thesis_with_fewer_than_2_events(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [
            {
                "title": "Undersupported thesis",
                "status": "active",
                "statement": "x",
                "new_thesis": True,
                "supporting_events": ["only one event"],
            }
        ],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["thesis_updates_rejected"] == 1
    assert summary["thesis_updates_applied"] == 0
    row = conn.execute("SELECT * FROM theses WHERE title = ?", ("Undersupported thesis",)).fetchone()
    assert row is None


def test_write_back_accepts_new_thesis_with_2_distinct_events(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [
            {
                "title": "Well-supported thesis",
                "status": "active",
                "statement": "x",
                "new_thesis": True,
                "supporting_events": ["event A on March 1", "event B on March 3"],
            }
        ],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["thesis_updates_applied"] == 1
    assert summary["thesis_updates_rejected"] == 0
    row = conn.execute("SELECT * FROM theses WHERE title = ?", ("Well-supported thesis",)).fetchone()
    assert row is not None


def test_write_back_rejects_duplicate_events_counted_as_two(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [
            {
                "title": "Fake double-count thesis",
                "status": "active",
                "statement": "x",
                "new_thesis": True,
                "supporting_events": ["same event", "same event"],
            }
        ],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)
    assert summary["thesis_updates_rejected"] == 1


def test_write_back_writes_new_predictions(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [],
        "new_predictions": [{"claim": "X will happen", "target_date": "2026-04-01", "source_note": ""}],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)
    assert summary["new_predictions"] == 1
    pending = db.get_pending_predictions(conn, "middle_east")
    assert len(pending) == 1
    assert pending[0]["claim"] == "X will happen"


# ---- predictions CRUD / track record ----


def test_track_record_omitted_when_sparse(conn):
    summary = memory.build_track_record_summary(conn, "middle_east", min_count=5)
    assert summary is None


def test_track_record_present_when_enough_resolved(conn):
    for i in range(6):
        pid = db.insert_prediction(conn, "middle_east", f"claim {i}", "2026-01-01", None, "2026-01-01")
        status = "confirmed" if i % 2 == 0 else "contradicted"
        db.resolve_prediction(conn, pid, status, "resolved in test")

    summary = memory.build_track_record_summary(conn, "middle_east", min_count=5)
    assert summary is not None
    assert "confirmed" in summary
    assert "contradicted" in summary


# ---- write_back resilience to malformed entries (no schema enforcement on Sonnet's JSON) ----


def test_write_back_skips_malformed_entity_without_crashing(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [{"type": "person", "notes": "missing the name field"}, {"name": "Valid Entity", "type": "person"}],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["new_entities"] == 1  # only the valid one
    assert summary["malformed_entries_skipped"] == 1
    row = conn.execute("SELECT * FROM entities WHERE name = ?", ("Valid Entity",)).fetchone()
    assert row is not None


def test_write_back_skips_malformed_relationship_without_crashing(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [{"entity_a": "A", "description": "missing entity_b and type"}],
        "new_events": [],
        "thesis_updates": [],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["new_relationships"] == 0
    assert summary["malformed_entries_skipped"] == 1


def test_write_back_skips_malformed_event_without_crashing(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [{"description": "missing the date field"}, {"date": "2026-03-01", "description": "valid event"}],
        "thesis_updates": [],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["new_events"] == 1  # only the valid one
    assert summary["malformed_entries_skipped"] == 1


def test_write_back_skips_malformed_thesis_update_without_crashing(conn):
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [],
        "new_relationships": [],
        "new_events": [],
        "thesis_updates": [{"status": "reinforced", "note": "missing the title field"}],
        "new_predictions": [],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["thesis_updates_applied"] == 0
    assert summary["malformed_entries_skipped"] == 1


def test_write_back_one_malformed_entry_does_not_block_the_rest(conn):
    # The actual point of the fix: a single bad entry must not cost the
    # whole write-back (and, in the real pipeline, the briefing/email that
    # follow it) after an already-paid-for Sonnet call.
    fake_collection = FakeChromaCollection()
    analysis_json = {
        "new_entities": [{"notes": "malformed, no name"}],
        "new_relationships": [],
        "new_events": [{"date": "2026-03-01", "description": "a real event"}],
        "thesis_updates": [],
        "new_predictions": [{"claim": "a real prediction", "target_date": None, "source_note": ""}],
    }
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01", fake_embed)

    assert summary["malformed_entries_skipped"] == 1
    assert summary["new_events"] == 1
    assert summary["new_predictions"] == 1


# ---- seen_urls ingest dedup store ----


def test_filter_unseen_and_mark_seen_roundtrip(conn):
    urls = ["https://a.com/1", "https://a.com/2", "https://a.com/3"]
    assert db.filter_unseen(conn, "middle_east", urls) == set(urls)

    db.mark_seen(conn, "middle_east", urls[:2])
    assert db.filter_unseen(conn, "middle_east", urls) == {"https://a.com/3"}


def test_mark_seen_is_idempotent(conn):
    db.mark_seen(conn, "middle_east", ["https://a.com/1"])
    db.mark_seen(conn, "middle_east", ["https://a.com/1"])  # no conflict error
    assert db.filter_unseen(conn, "middle_east", ["https://a.com/1"]) == set()


def test_seen_urls_are_domain_scoped(conn):
    db.mark_seen(conn, "middle_east", ["https://a.com/1"])
    assert db.filter_unseen(conn, "other_domain", ["https://a.com/1"]) == {"https://a.com/1"}


def test_prune_seen_removes_old_rows(conn):
    db.mark_seen(conn, "middle_east", ["https://a.com/1"])
    db.prune_seen(conn, "middle_east", retention_days=90)  # fresh row survives
    assert db.filter_unseen(conn, "middle_east", ["https://a.com/1"]) == set()
    db.prune_seen(conn, "middle_east", retention_days=0)  # everything older than "now" goes
    assert db.filter_unseen(conn, "middle_east", ["https://a.com/1"]) == {"https://a.com/1"}
