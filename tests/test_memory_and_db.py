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

    def upsert(self, ids, documents, metadatas):
        self.upserted.append((ids, documents, metadatas))

    def count(self):
        return len(self.upserted)

    def query(self, query_texts, n_results):
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}


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
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01")

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
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01")

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
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01")
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
    summary = memory.write_back(conn, fake_collection, "middle_east", analysis_json, "2026-03-01")
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
