"""Stage 4 — deep analysis via Claude Sonnet 5 with adaptive thinking (spec
section 5, Call 2). This is the ONLY stage in the whole pipeline that calls
the Anthropic API; triage and prediction resolution (Stage 2) stay local
via Ollama.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "analysis_system.md"
COST_LOG_PATH = Path(__file__).parent.parent / "data" / "api_cost_log.csv"

# Confirmed live 2026-07-29: a genuine `overloaded_error` (529, Anthropic's
# own servers at capacity) crashed a run at this, the pipeline's only paid
# stage, discarding ~10 minutes of already-completed free local work
# (ingest/dedup/triage/prediction-resolution). The Anthropic SDK already
# retries 429/5xx internally (default `max_retries=2`, short backoff) before
# raising, so by the time this surfaces here the quick retries are already
# exhausted -- a real overload can still be in effect seconds later, so this
# outer retry waits much longer (60s, vs. deliver.py's 30s for SMTP) before
# trying once more. Only genuinely retryable, server/transport-side errors
# are covered here; a 400/401/403/404/413/422 means something is wrong with
# the request itself (bad prompt, bad key, oversized input) and retrying
# would just waste another 60s reproducing the same failure.
ANALYSIS_RETRY_DELAY_SECONDS = 60

RETRYABLE_ANALYSIS_ERRORS = (
    anthropic.APIConnectionError,  # includes APITimeoutError
    anthropic.RateLimitError,  # 429
    anthropic.InternalServerError,  # 500 and other undifferentiated 5xx
    anthropic.OverloadedError,  # 529 -- the actual live failure this covers
)

# Sonnet 5 pricing per spec section 2: introductory $2/$10 per MTok through
# 2026-08-31, reverting to standard $3/$15 after.
PRICING = {
    "intro": {"input": 2.00, "output": 10.00, "cutoff": "2026-08-31"},
    "standard": {"input": 3.00, "output": 15.00},
}


def _current_rates() -> dict:
    cutoff = date.fromisoformat(PRICING["intro"]["cutoff"])
    if date.today() <= cutoff:
        return PRICING["intro"]
    return PRICING["standard"]


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    rates = _current_rates()
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


def _load_system_prompt() -> str:
    return ANALYSIS_PROMPT_PATH.read_text()


def compute_effort(triaged_item_count: int, active_theses_count: int, pipeline_cfg: dict) -> str:
    """Translate spec 5's "scaled thinking budget, not flat" onto
    `output_config.effort` (see config/watchlists.yaml for why — Sonnet 5
    rejects `thinking.budget_tokens` outright).

    Deliberately conservative: "high" is a CAP reached by item/thesis load
    alone; escalating past it to "xhigh" additionally requires a real
    item-volume signal AND at least one currently-active thesis in play,
    so "high" doesn't become the silent default on an ordinary day.

    "max" is deliberately never auto-selected here — confirmed live
    2026-07-26 that `active_theses_count` isn't a reliable "genuinely rare
    heavy day" signal the way it looks on paper: once enough real theses
    accumulate (an expected, not anomalous, outcome of a maturing memory
    store watching a persistently active region), an active-theses
    threshold that was meant to gate a rare tier instead stays permanently
    cleared, and "max" silently becomes the routine outcome — exactly the
    "high becomes the silent default" failure this function was designed
    to prevent, just one tier up. `xhigh` is the documented top
    recommended tier for agentic/reasoning work on Sonnet 5 already, so
    this function now caps there; there is currently no path to "max" in
    this pipeline at all.
    """
    if (
        triaged_item_count >= pipeline_cfg["xhigh_min_triaged_items"]
        and active_theses_count >= pipeline_cfg["xhigh_min_active_theses"]
    ):
        return "xhigh"

    load = triaged_item_count + 2 * active_theses_count
    if load <= pipeline_cfg["effort_low_max_load"]:
        return "low"
    if load <= pipeline_cfg["effort_medium_max_load"]:
        return "medium"
    return "high"


def _format_oil_snapshot(oil_snapshot: dict | None) -> str:
    """Renders the EIA oil-price snapshot (pipeline/oil_prices.py) as a
    prompt section, feeding the system prompt's "Economics & markets"
    lens real numbers. Omitted entirely when unavailable (no EIA_API_KEY
    configured, or the fetch failed) — same "just skip this piece of
    context" pattern as the track-record summary below, not a reason to
    fail the run."""
    if not oil_snapshot:
        return ""
    labels = {"wti": "WTI", "brent": "Brent"}
    lines = []
    for key, label in labels.items():
        data = oil_snapshot.get(key)
        if not data:
            continue
        change_1d = (
            f"{data['change_1d_pct']:+.1f}% vs. prior trading day"
            if data["change_1d_pct"] is not None
            else "no prior-day comparison available"
        )
        change_7d = (
            f"{data['change_7d_pct']:+.1f}% vs. ~7 days ago"
            if data["change_7d_pct"] is not None
            else "no 7-day comparison available"
        )
        lines.append(f"- {label}: ${data['price']:.2f}/bbl as of {data['date']} ({change_1d}, {change_7d})")
    if not lines:
        return ""
    return "## Oil price snapshot (EIA spot prices)\n" + "\n".join(lines) + "\n\n"


def _build_user_prompt(triaged_items: list, memory_context: dict, oil_snapshot: dict | None = None) -> str:
    items_block = "\n".join(
        f"- [{item.source} (Tier {item.raw_metadata.get('tier', '?')}), {item.published}] {item.title}\n"
        f"  excerpt: {item.text[:500]}\n"
        f"  triage: score={item.raw_metadata.get('triage_score')} reason={item.raw_metadata.get('triage_reason')}"
        for item in triaged_items
    ) or "(none survived triage today)"

    active_theses = memory_context["active_theses"]
    theses_block = "\n".join(
        f"- [{t['status']}] {t['title']}: {t['statement']}" for t in active_theses
    ) or "(none yet)"

    entities_block = "\n".join(
        f"- {e['name']} ({e['type']}): {e.get('notes', '')}" for e in memory_context["entity_graph"]
    ) or "(none yet)"

    related_events = memory_context["related_past_events"]
    related_block = "\n".join(
        f"- {r['summary']} (date={r['metadata'].get('date')}, distance={r['distance']:.3f})"
        for r in related_events
    ) or "(no related past events retrieved)"

    # Explicit runtime cold-start signal alongside the system prompt's
    # instruction — spec 5 calls this the primary failure mode to guard
    # against, so don't rely on the model inferring sparseness from an
    # empty-looking section alone.
    memory_status = (
        f"MEMORY STATUS: {len(active_theses)} active thesis(es), "
        f"{len(related_events)} related past event(s) retrieved."
    )
    if not active_theses and not related_events:
        memory_status += " This looks like a cold start — say so plainly in section 2, do not manufacture continuity."

    track_record = memory_context.get("track_record_summary")
    track_record_block = (
        f"## Prediction track record\n{track_record}\n\n" if track_record else ""
    )

    oil_block = _format_oil_snapshot(oil_snapshot)

    return (
        f"{memory_status}\n\n"
        f"## Today's surviving items ({len(triaged_items)})\n{items_block}\n\n"
        f"## Active standing theses\n{theses_block}\n\n"
        f"## Tracked entity graph\n{entities_block}\n\n"
        f"## Retrieved related past events\n{related_block}\n\n"
        f"{oil_block}"
        f"{track_record_block}"
    )


def _extract_json_block(text: str) -> dict:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        logger.warning("No JSON write-back block found in analysis output")
        return {
            "new_entities": [],
            "new_relationships": [],
            "new_events": [],
            "thesis_updates": [],
            "new_predictions": [],
        }
    parsed = json.loads(match.group(1))
    parsed.setdefault("new_predictions", [])
    return parsed


def _log_cost_csv(domain: str, model: str, effort: str, input_tokens: int, output_tokens: int, cost: float) -> None:
    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not COST_LOG_PATH.exists()
    with open(COST_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(
                ["timestamp", "domain", "model", "effort", "input_tokens", "output_tokens", "estimated_cost_usd"]
            )
        writer.writerow(
            [datetime.now(timezone.utc).isoformat(), domain, model, effort, input_tokens, output_tokens, f"{cost:.6f}"]
        )


def run_analysis(
    triaged_items: list,
    memory_context: dict,
    domain: str,
    model: str,
    max_tokens: int,
    pipeline_cfg: dict,
    api_key: str | None = None,
    oil_snapshot: dict | None = None,
) -> dict:
    """Single Sonnet call with adaptive thinking, scaled effort. Returns
    {"markdown": full response text, "write_back": parsed JSON block,
    "usage": {...}, "cost_usd": float, "effort": str}."""
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    effort = compute_effort(len(triaged_items), len(memory_context["active_theses"]), pipeline_cfg)

    system_prompt = _load_system_prompt()
    user_prompt = _build_user_prompt(triaged_items, memory_context, oil_snapshot)

    def _call() -> anthropic.types.Message:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            return stream.get_final_message()

    try:
        response = _call()
    except RETRYABLE_ANALYSIS_ERRORS as first_error:
        logger.warning(
            "Analysis call failed (%s), retrying once in %ds: %s",
            type(first_error).__name__,
            ANALYSIS_RETRY_DELAY_SECONDS,
            first_error,
        )
        time.sleep(ANALYSIS_RETRY_DELAY_SECONDS)
        response = _call()

    text = "".join(block.text for block in response.content if block.type == "text")
    write_back = _extract_json_block(text)

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = _estimate_cost(input_tokens, output_tokens)
    _log_cost_csv(domain, model, effort, input_tokens, output_tokens, cost)

    logger.info(
        "Analysis call: model=%s effort=%s input_tokens=%d output_tokens=%d cost=$%.4f",
        model,
        effort,
        input_tokens,
        output_tokens,
        cost,
    )

    return {
        "markdown": text,
        "write_back": write_back,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost_usd": cost,
        "effort": effort,
    }
