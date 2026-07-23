"""Stage 2c — prediction resolution check (spec section 3.C).

Runs locally via the same Ollama model as triage, before Call 2 — this is
explicitly NOT a new Anthropic API call. Compares each still-open
prediction against today's deduped raw items and flags confirmed/
contradicted outcomes so Call 2's track-record summary (pipeline/memory.py
::build_track_record_summary) reflects reality.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from pipeline.ollama_client import call_ollama

logger = logging.getLogger(__name__)

RESOLUTION_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "prediction_resolution_system.md"
BATCH_SIZE = 20


def _load_system_prompt() -> str:
    return RESOLUTION_PROMPT_PATH.read_text()


def _build_predictions_block(predictions: list) -> str:
    lines = []
    for i, p in enumerate(predictions):
        target = f", target: {p['target_date']}" if p["target_date"] else ""
        lines.append(f"[{i}] claim: {p['claim']}{target}")
    return "\n".join(lines)


def _build_items_block(items: list) -> str:
    lines = []
    for item in items:
        lines.append(f"- [{item.source}, {item.published}] {item.title}\n  excerpt: {item.text[:400]}")
    return "\n".join(lines)


def _parse_verdicts(raw_content: str, batch_len: int) -> dict[int, dict]:
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        logger.warning("Prediction resolution model returned invalid JSON, treating batch as all-pending")
        parsed = []

    verdicts: dict[int, dict] = {}
    for entry in parsed:
        try:
            idx = int(entry["index"])
            verdict = entry.get("verdict", "pending")
            if verdict not in ("confirmed", "contradicted", "pending"):
                verdict = "pending"
            verdicts[idx] = {"verdict": verdict, "reason": entry.get("reason", "")}
        except (KeyError, ValueError, TypeError):
            continue

    for i in range(batch_len):
        verdicts.setdefault(i, {"verdict": "pending", "reason": ""})
    return verdicts


def resolve_predictions(
    pending_predictions: list,
    todays_items: list,
    ollama_host: str,
    model: str,
) -> list[dict]:
    """Check each pending prediction (sqlite3.Row-like dicts with id/claim/
    target_date) against today's deduped items. Returns a list of
    {"id": int, "verdict": str, "reason": str} for predictions that
    resolved (confirmed/contradicted) — pending ones are omitted, since
    there's nothing to write back for them."""
    if not pending_predictions or not todays_items:
        return []

    system_prompt = _load_system_prompt()
    items_block = _build_items_block(todays_items)
    resolutions: list[dict] = []

    for batch_start in range(0, len(pending_predictions), BATCH_SIZE):
        batch = pending_predictions[batch_start : batch_start + BATCH_SIZE]
        predictions_block = _build_predictions_block(batch)
        user_prompt = (
            f"## Pending predictions\n{predictions_block}\n\n## Today's items\n{items_block}\n"
        )
        try:
            raw_content = call_ollama(ollama_host, model, system_prompt, user_prompt)
        except httpx.HTTPError:
            logger.exception(
                "Ollama prediction-resolution call failed for batch starting at %d; "
                "leaving these predictions pending", batch_start
            )
            continue

        verdicts = _parse_verdicts(raw_content, len(batch))
        for i, pred in enumerate(batch):
            v = verdicts[i]
            if v["verdict"] != "pending":
                resolutions.append({"id": pred["id"], "verdict": v["verdict"], "reason": v["reason"]})

    logger.info(
        "Prediction resolution check: %d pending checked, %d resolved this run",
        len(pending_predictions),
        len(resolutions),
    )
    return resolutions
