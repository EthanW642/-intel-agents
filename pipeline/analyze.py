"""Stage 4 — deep analysis via Claude Sonnet 5 with extended (adaptive)
thinking (spec section 5, Call 2). This is the ONLY stage in the whole
pipeline that calls the Anthropic API; triage (Stage 2) stays local via
Ollama.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "analysis_system.md"
COST_LOG_PATH = Path(__file__).parent.parent / "data" / "api_cost_log.csv"

# Sonnet 5 pricing per spec section 2: introductory $2/$10 per MTok through
# 2026-08-31, reverting to standard $3/$15 after. Update INTRO_PRICING_CUTOFF
# has already passed handling below.
PRICING = {
    "intro": {"input": 2.00, "output": 10.00, "cutoff": "2026-08-31"},
    "standard": {"input": 3.00, "output": 15.00},
}


def _current_rates() -> dict:
    from datetime import date

    cutoff = date.fromisoformat(PRICING["intro"]["cutoff"])
    if date.today() <= cutoff:
        return PRICING["intro"]
    return PRICING["standard"]


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    rates = _current_rates()
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


def _load_system_prompt() -> str:
    return ANALYSIS_PROMPT_PATH.read_text()


def _build_user_prompt(triaged_items: list, memory_context: dict) -> str:
    items_block = "\n".join(
        f"- [{item.source}, {item.published}] {item.title}\n"
        f"  excerpt: {item.text[:500]}\n"
        f"  triage: score={item.raw_metadata.get('triage_score')} reason={item.raw_metadata.get('triage_reason')}"
        for item in triaged_items
    )

    theses_block = "\n".join(
        f"- [{t['status']}] {t['title']}: {t['statement']}" for t in memory_context["active_theses"]
    ) or "(none yet)"

    entities_block = "\n".join(
        f"- {e['name']} ({e['type']}): {e.get('notes', '')}" for e in memory_context["entity_graph"]
    ) or "(none yet)"

    related_block = "\n".join(
        f"- {r['summary']} (date={r['metadata'].get('date')}, distance={r['distance']:.3f})"
        for r in memory_context["related_past_events"]
    ) or "(no related past events retrieved)"

    return (
        f"## Today's surviving items ({len(triaged_items)})\n{items_block}\n\n"
        f"## Active standing theses\n{theses_block}\n\n"
        f"## Tracked entity graph\n{entities_block}\n\n"
        f"## Retrieved related past events\n{related_block}\n"
    )


def _extract_json_block(text: str) -> dict:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        logger.warning("No JSON write-back block found in analysis output")
        return {"new_entities": [], "new_relationships": [], "new_events": [], "thesis_updates": []}
    return json.loads(match.group(1))


def _log_cost_csv(domain: str, model: str, input_tokens: int, output_tokens: int, cost: float) -> None:
    from datetime import datetime, timezone

    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not COST_LOG_PATH.exists()
    with open(COST_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "domain", "model", "input_tokens", "output_tokens", "estimated_cost_usd"])
        writer.writerow(
            [datetime.now(timezone.utc).isoformat(), domain, model, input_tokens, output_tokens, f"{cost:.6f}"]
        )


def run_analysis(
    triaged_items: list,
    memory_context: dict,
    domain: str,
    model: str,
    max_tokens: int,
    api_key: str | None = None,
) -> dict:
    """Single Sonnet call with adaptive extended thinking. Returns
    {"markdown": full response text, "write_back": parsed JSON block,
    "usage": {...}, "cost_usd": float}."""
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    system_prompt = _load_system_prompt()
    user_prompt = _build_user_prompt(triaged_items, memory_context)

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive", "display": "summarized"},
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    text = "".join(block.text for block in response.content if block.type == "text")
    write_back = _extract_json_block(text)

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = _estimate_cost(input_tokens, output_tokens)
    _log_cost_csv(domain, model, input_tokens, output_tokens, cost)

    logger.info(
        "Analysis call: model=%s input_tokens=%d output_tokens=%d cost=$%.4f",
        model,
        input_tokens,
        output_tokens,
        cost,
    )

    return {
        "markdown": text,
        "write_back": write_back,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost_usd": cost,
    }
