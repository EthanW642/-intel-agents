-- Structured relational store (spec section 3.B).
-- One SQLite file per domain; this schema is shared across all domains.

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,              -- person | institution | country | faction
    notes TEXT,
    first_seen TEXT NOT NULL,        -- ISO date
    last_updated TEXT NOT NULL,      -- ISO date
    UNIQUE (domain, name)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    date TEXT NOT NULL,              -- ISO date of the event
    description TEXT NOT NULL,
    event_type TEXT,
    confidence REAL,                 -- 0-1
    source_urls TEXT,                -- JSON array of strings
    entity_ids TEXT,                 -- JSON array of entities.id
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    entity_a_id INTEGER NOT NULL REFERENCES entities(id),
    entity_b_id INTEGER NOT NULL REFERENCES entities(id),
    relationship_type TEXT NOT NULL, -- e.g. succeeds, funds, opposes, allied_with
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active | ended
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    -- active | reinforced | complicated | falsified | dormant (spec section 3.B).
    -- dormant = no corroborating evidence for the configured window (default 14
    -- days), not a verdict; falsified = evidence actively contradicts the thesis.
    status TEXT NOT NULL DEFAULT 'active',
    evidence_log TEXT,               -- JSON array of {date, event_ref, note}
    -- Independent supporting events cited when the thesis was formed (spec
    -- 3.B's 2-event bar). JSON array of event refs; length must be >= 2 at
    -- creation time, enforced in pipeline/memory.py write-back, not just here.
    supporting_event_refs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (domain, title)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    claim TEXT NOT NULL,
    date_made TEXT NOT NULL,         -- ISO date the prediction was made
    target_date TEXT,                -- ISO date/window the prediction targets, if given
    status TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | contradicted
    resolution_note TEXT,            -- set when status moves off 'pending'
    resolved_at TEXT,
    source_run_date TEXT NOT NULL,   -- date of the analysis run that produced this prediction
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    thinking_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_domain_date ON events(domain, date);
CREATE INDEX IF NOT EXISTS idx_relationships_entities ON relationships(entity_a_id, entity_b_id);
CREATE INDEX IF NOT EXISTS idx_theses_domain_status ON theses(domain, status);
CREATE INDEX IF NOT EXISTS idx_predictions_domain_status ON predictions(domain, status);
