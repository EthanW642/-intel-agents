"""Stage 2b — local relevance triage via Ollama (spec section 5, Call 1).

Runs entirely against a local Ollama server — no Anthropic API call happens
in this stage. Batches surviving dedup'd items, asks the local model to
score each 0-10 against the domain watchlist/theses, and cuts everything
below the configured threshold.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from pipeline.ollama_client import call_ollama

logger = logging.getLogger(__name__)

TRIAGE_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "triage_system.md"
BATCH_SIZE = 20


class TriageError(Exception):
    pass


def _load_system_prompt() -> str:
    return TRIAGE_PROMPT_PATH.read_text()


def _build_context_block(entities: list, theses: list) -> str:
    entity_lines = "\n".join(f"- {e['name']} ({e['type']}): {e.get('notes', '')}" for e in entities)
    thesis_lines = "\n".join(f"- {t['title']}: {t['statement'].strip()}" for t in theses)
    return (
        f"## Standing watchlist entities\n{entity_lines}\n\n"
        f"## Active standing theses\n{thesis_lines}\n"
    )


def _build_batch_block(items: list) -> str:
    lines = []
    for i, item in enumerate(items):
        lines.append(
            f"[{i}] title: {item.title}\nsource: {item.source}\ndate: {item.published}\n"
            f"excerpt: {item.text[:600]}\n"
        )
    return "\n".join(lines)


def _parse_scores(raw_content: str, batch_len: int, fallback_score: int) -> dict[int, dict]:
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        logger.warning("Triage model returned invalid JSON, falling back to pass-through for this batch")
        parsed = []

    scores: dict[int, dict] = {}
    for entry in parsed:
        try:
            idx = int(entry["index"])
            scores[idx] = {"score": int(entry["score"]), "reason": entry.get("reason", "")}
        except (KeyError, ValueError, TypeError):
            continue

    for i in range(batch_len):
        if i not in scores:
            scores[i] = {"score": fallback_score, "reason": "triage-parse-fallback: model omitted this item"}
    return scores


def triage_items(
    items: list,
    entities: list,
    theses: list,
    ollama_host: str,
    model: str,
    score_threshold: int,
) -> list:
    """Score every item 0-10 via the local Ollama model and return only
    those meeting `score_threshold`, each annotated with its triage score."""
    if not items:
        return []

    system_prompt = _load_system_prompt()
    context_block = _build_context_block(entities, theses)

    total_batches = (len(items) + BATCH_SIZE - 1) // BATCH_SIZE
    surviving = []
    fallback_count = 0
    for batch_num, batch_start in enumerate(range(0, len(items), BATCH_SIZE), start=1):
        batch = items[batch_start : batch_start + BATCH_SIZE]
        # Per-batch marker so a slow-but-working run is visibly
        # distinguishable from a hung one on a heavily-loaded local model —
        # the alternative (nothing until the final summary log) looks
        # identical to a freeze on a long run.
        logger.info("Triage batch %d/%d (%d items)...", batch_num, total_batches, len(batch))
        user_prompt = f"{context_block}\n## Items to score\n{_build_batch_block(batch)}"
        try:
            raw_content = call_ollama(ollama_host, model, system_prompt, user_prompt)
        except httpx.HTTPError:
            logger.exception(
                "Ollama triage call failed after retries for batch starting at %d; "
                "is `ollama serve` running with model '%s' pulled?",
                batch_start,
                model,
            )
            continue

        scores = _parse_scores(raw_content, len(batch), fallback_score=score_threshold)
        batch_fallbacks = sum(1 for r in scores.values() if r["reason"].startswith("triage-parse-fallback"))
        fallback_count += batch_fallbacks
        for i, item in enumerate(batch):
            result = scores[i]
            if result["score"] >= score_threshold:
                item.raw_metadata["triage_score"] = result["score"]
                item.raw_metadata["triage_reason"] = result["reason"]
                surviving.append(item)
        logger.info(
            "Triage batch %d/%d done — %d survivors so far (%d fallback-scored this batch)",
            batch_num,
            total_batches,
            len(surviving),
            batch_fallbacks,
        )

    logger.info(
        "Triage: %d items scored, %d survived threshold %d — of those survivors, "
        "%d were genuine model scores and %d were parse-fallback pass-throughs "
        "(not real judgments; investigate the raw Ollama output if this is high)",
        len(items),
        len(surviving),
        score_threshold,
        len(surviving) - fallback_count,
        fallback_count,
    )
    return surviving
