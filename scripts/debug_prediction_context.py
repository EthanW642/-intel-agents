"""Zero-cost diagnostic for the 2026-07-25 prediction-resolution bug: every
one of 14 pending predictions was marked "contradicted," each citing a
completely unrelated news item (a film review, a football signing, mining
reforms...) as justification — the signature of a truncated context window,
not bad reasoning. Ollama silently truncates prompts that exceed its
default num_ctx (2048-4096 tokens) rather than erroring, so a large items
block (predictions.py was dumping all ~393 deduped items into one prompt)
can leave the model unable to see the one item that actually matters.

This builds a large synthetic items list with ONE genuinely relevant item
buried near the end, and calls Ollama twice: once with no context override
(today's actual behavior) and once with an explicit large num_ctx. If the
theory is right, the first call should fail to find/cite the relevant item
(defaulting to "pending" or citing an unrelated filler item) and the second
should correctly find and confirm it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from pipeline.ollama_client import call_ollama
from pipeline.predictions import _build_items_block, _build_predictions_block, _load_system_prompt, _resolution_response_schema
from agents.middle_east.sources import RawItem

WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlists.yaml"
NUM_FILLER_ITEMS = 350


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

    print("=== Call 1: NO num_ctx override (today's actual behavior) ===", file=sys.stderr)
    raw_default = call_ollama(
        pipeline_cfg["ollama_host"],
        pipeline_cfg["triage_model"],
        system_prompt,
        user_prompt,
        response_format=_resolution_response_schema(len(predictions)),
    )
    print(raw_default)

    print("\n=== Call 2: num_ctx=16384 (explicit large context) ===", file=sys.stderr)
    raw_large_ctx = call_ollama(
        pipeline_cfg["ollama_host"],
        pipeline_cfg["triage_model"],
        system_prompt,
        user_prompt,
        response_format=_resolution_response_schema(len(predictions)),
        num_ctx=16384,
    )
    print(raw_large_ctx)


if __name__ == "__main__":
    main()
