"""Zero-cost diagnostic for the 2026-07-25 prediction-resolution bug: every
one of 14 pending predictions was marked "contradicted," each citing a
completely unrelated news item (a film review, a football signing, mining
reforms...) as justification — the signature of a truncated context window,
not bad reasoning. Ollama silently truncates prompts that exceed its
default num_ctx (2048-4096 tokens) rather than erroring, so a large items
block (predictions.py was dumping all ~393 deduped items into one prompt)
could leave the model unable to see the one item that actually matters.

The first fix attempt (raising num_ctx to 16384 while still passing all
~350+ items) did NOT work — the prompt was ~33,012 estimated tokens,
still bigger than 16384, and the model returned a nonsense out-of-range
index both times. The real fix has two parts: (1) run.py now passes
`resolve_predictions` the already-triaged item set (~62 items, not ~393),
a ~6x reduction, and (2) predictions.py now sets an explicit num_ctx
(16384) as a floor regardless. This script mirrors both: it builds a
REALISTIC-sized items list (matching the post-fix triaged-item scale, not
the original 350-filler-item stress test) and calls Ollama the way
predictions.py actually does now, to sanity-check before spending on a
real run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from pipeline.ollama_client import call_ollama
from pipeline.predictions import NUM_CTX, _build_items_block, _build_predictions_block, _load_system_prompt, _resolution_response_schema
from agents.middle_east.sources import RawItem

WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlists.yaml"
NUM_FILLER_ITEMS = 60  # realistic post-fix scale: ~62 triaged items, not the full ~393 deduped set


def _filler_item(i: int) -> RawItem:
    return RawItem(
        title=f"Unrelated local news story #{i}",
        source="filler",
        url=f"http://example.com/{i}",
        published="2026-07-25",
        text=(
            f"This is filler item number {i}, about a completely unrelated local "
            "topic such as a cultural event, a sports result, or routine municipal "
            "business, included only to pad out the context window for this test. "
            "It has no bearing on Iran, Israel, Saudi Arabia, or any Middle East "
            "geopolitical development whatsoever."
        ),
    )


def main() -> None:
    cfg = yaml.safe_load(WATCHLIST_PATH.read_text())
    pipeline_cfg = cfg["pipeline"]

    predictions = [{"id": 1, "claim": "IRGC will name a new Quds Force commander this week.", "target_date": "2026-07-31"}]

    items = [_filler_item(i) for i in range(NUM_FILLER_ITEMS)]
    items.insert(
        NUM_FILLER_ITEMS - 5,  # buried near the end, not the start
        RawItem(
            title="IRGC names new Quds Force commander",
            source="test",
            url="http://example.com/real",
            published="2026-07-25",
            text="Iran's IRGC announced a new commander for the Quds Force overnight, confirming the leadership change reported by state media.",
        ),
    )

    system_prompt = _load_system_prompt()
    predictions_block = _build_predictions_block(predictions)
    items_block = _build_items_block(items)
    user_prompt = f"## Pending predictions\n{predictions_block}\n\n## Today's items\n{items_block}\n"

    print(f"=== Prompt size: {len(user_prompt)} chars (~{len(user_prompt)//4} tokens estimated) ===\n", file=sys.stderr)

    print(f"=== Call: num_ctx={NUM_CTX} (matches predictions.py's actual fixed behavior) ===", file=sys.stderr)
    raw = call_ollama(
        pipeline_cfg["ollama_host"],
        pipeline_cfg["triage_model"],
        system_prompt,
        user_prompt,
        response_format=_resolution_response_schema(len(predictions)),
        num_ctx=NUM_CTX,
    )
    print(raw)
    print(
        "\nExpected: index 0, verdict 'confirmed', reason citing the IRGC Quds Force item. "
        "Anything else (wrong index, 'contradicted', or a reason citing an unrelated filler "
        "item) means the fix isn't sufficient yet — paste this output back before running the "
        "full pipeline.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
