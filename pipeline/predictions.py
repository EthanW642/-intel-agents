"""Stage 2c — prediction resolution check (spec section 3.C).

Runs locally via the same Ollama model as triage, before Call 2 — this is
explicitly NOT a new Anthropic API call. Compares each still-open
prediction against today's already-triaged (relevance-filtered) items and
flags confirmed/contradicted outcomes so Call 2's track-record summary
(pipeline/memory.py::build_track_record_summary) reflects reality.

Deliberately called with the *triaged* set, not the full deduped set — see
agents/middle_east/run.py's Stage 2c comment: passing all ~393 deduped
items here (confirmed live 2026-07-25) blew Ollama's context window,
silently truncating input and producing confident-looking verdicts about
news the model never actually saw.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from pipeline.ollama_client import OllamaUnavailableError, call_ollama

logger = logging.getLogger(__name__)

RESOLUTION_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "prediction_resolution_system.md"
BATCH_SIZE = 20
# See pipeline/triage.py's NUM_CTX comment. Sized generously (vs. triage's
# 8192) because the full items block is repeated in every predictions-batch
# call here, and the triaged set can run larger on heavy news days.
NUM_CTX = 16384


def _load_system_prompt() -> str:
    return RESOLUTION_PROMPT_PATH.read_text()


def _resolution_response_schema(batch_len: int) -> dict:
    """A JSON Schema pinning the response to exactly one {index, verdict,
    reason} object per pending prediction in the batch — see
    pipeline/ollama_client.py::call_ollama's docstring for why this, rather
    than bare "format": "json", is required. `index` bounds catch
    out-of-range nonsense the model can still emit even with a schema
    (confirmed live 2026-07-25: a 1-prediction batch returned index 17,
    then index 35 on a retry — almost certainly a context-truncation
    symptom, but bounding this is cheap insurance regardless)."""
    return {
        "type": "array",
        "minItems": batch_len,
        "maxItems": batch_len,
        "items": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 0, "maximum": max(batch_len - 1, 0)},
                "verdict": {"type": "string", "enum": ["confirmed", "contradicted", "pending"]},
                "reason": {"type": "string"},
            },
            "required": ["index", "verdict", "reason"],
        },
    }


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
    total_batches = (len(pending_predictions) + BATCH_SIZE - 1) // BATCH_SIZE
    connection_failures = 0

    for batch_num, batch_start in enumerate(range(0, len(pending_predictions), BATCH_SIZE), start=1):
        batch = pending_predictions[batch_start : batch_start + BATCH_SIZE]
        logger.info("Prediction resolution batch %d/%d (%d predictions)...", batch_num, total_batches, len(batch))
        predictions_block = _build_predictions_block(batch)
        user_prompt = (
            f"## Pending predictions\n{predictions_block}\n\n## Today's items\n{items_block}\n"
        )
        try:
            raw_content = call_ollama(
                ollama_host,
                model,
                system_prompt,
                user_prompt,
                response_format=_resolution_response_schema(len(batch)),
                num_ctx=NUM_CTX,
            )
        except httpx.HTTPError:
            logger.exception(
                "Ollama prediction-resolution call failed for batch starting at %d; "
                "leaving these predictions pending", batch_start
            )
            connection_failures += 1
            continue

        verdicts = _parse_verdicts(raw_content, len(batch))
        for i, pred in enumerate(batch):
            v = verdicts[i]
            if v["verdict"] != "pending":
                resolutions.append({"id": pred["id"], "verdict": v["verdict"], "reason": v["reason"]})

    if total_batches > 0 and connection_failures == total_batches:
        raise OllamaUnavailableError(
            f"All {total_batches} prediction-resolution batches failed to reach Ollama at "
            f"{ollama_host} (model={model}) — is `ollama serve` running?"
        )

    logger.info(
        "Prediction resolution check: %d pending checked, %d resolved this run",
        len(pending_predictions),
        len(resolutions),
    )
    return resolutions
