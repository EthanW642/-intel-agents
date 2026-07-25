"""Zero-cost diagnostic: calls the local Ollama triage model with a tiny
fixed batch and prints the RAW response text, unparsed. No Sonnet call, no
Anthropic API cost — for diagnosing why real triage runs have shown 0
genuine model scores / 100% parse-fallback pass-throughs (see README
findings log). Run this before re-running the full pipeline so we know
qwen2.5:14b's actual response shape instead of guessing against a $0.40+
live run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from pipeline.ollama_client import call_ollama
from pipeline.triage import _build_batch_block, _build_context_block, _load_system_prompt, _triage_response_schema
from agents.middle_east.sources import RawItem

WATCHLIST_PATH = Path(__file__).parent.parent / "config" / "watchlists.yaml"


def main() -> None:
    cfg = yaml.safe_load(WATCHLIST_PATH.read_text())
    pipeline_cfg = cfg["pipeline"]

    items = [
        RawItem(
            title="Iran fires missiles at Saudi oil facility",
            source="test",
            url="http://example.com/1",
            published="2026-07-25",
            text="Houthi forces launched missiles at a Saudi oil installation overnight.",
        ),
        RawItem(
            title="Local weather forecast for the weekend",
            source="test",
            url="http://example.com/2",
            published="2026-07-25",
            text="Sunny skies expected across the region this weekend.",
        ),
    ]

    system_prompt = _load_system_prompt()
    context_block = _build_context_block(entities=[], theses=[])
    user_prompt = f"{context_block}\n## Items to score\n{_build_batch_block(items)}"

    print("=== Calling Ollama with bare 'format': 'json' (the OLD behavior) ===", file=sys.stderr)
    raw_bare = call_ollama(
        pipeline_cfg["ollama_host"],
        pipeline_cfg["triage_model"],
        system_prompt,
        user_prompt,
        response_format="json",
    )
    print("--- bare json-mode response (repr) ---")
    print(repr(raw_bare))

    print("\n=== Calling Ollama with a schema pinned to 2 items (the FIXED behavior) ===", file=sys.stderr)
    raw_schema = call_ollama(
        pipeline_cfg["ollama_host"],
        pipeline_cfg["triage_model"],
        system_prompt,
        user_prompt,
        response_format=_triage_response_schema(len(items)),
    )
    print("--- schema-constrained response (repr) ---")
    print(repr(raw_schema))
    print("\n--- schema-constrained response (printed) ---")
    print(raw_schema)


if __name__ == "__main__":
    main()
