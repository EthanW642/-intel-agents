"""Per-run log (spec section 6/7) — one row per pipeline run, separate from
api_cost_log.csv (which is per-Sonnet-call). This is what answers "did
today's run actually work, what did it cost, and did the email go out"
without digging through logs — and per the user's explicit ask, tracks the
effort tier actually chosen against real token/cost outcomes, since effort
tiers are less predictable up front than a fixed token budget was.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

RUN_LOG_PATH = Path(__file__).parent.parent / "data" / "run_log.csv"

FIELDS = [
    "timestamp",
    "run_date",
    "domain",
    "items_ingested",
    "items_deduped",
    "items_triaged_survived",
    "predictions_resolved",
    "effort",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "new_entities",
    "new_relationships",
    "new_events",
    "thesis_updates_applied",
    "thesis_updates_rejected",
    "new_predictions",
    "email_sent",
    "email_retried",
    "email_error",
    "pipeline_error",
]


def write_run_log_row(row: dict) -> None:
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not RUN_LOG_PATH.exists()
    full_row = {field: row.get(field, "") for field in FIELDS}
    full_row["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(RUN_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(full_row)
