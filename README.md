# Intel Agents — Phase 1: Middle East Agent

Local multi-domain intelligence agent system. Phase 1 scope only: the Middle
East agent, end to end. See the full build spec for later phases.

## Architecture (this phase)

```
Stage 1  INGEST        agents/middle_east/{sources.py,ingest.py}   GDELT + LiveUAMap + RSS
Stage 2a DEDUP          pipeline/dedup.py                          local embeddings (all-MiniLM-L6-v2)
Stage 2b TRIAGE         pipeline/triage.py                         local Ollama (qwen2.5:14b) — no API call
Stage 3  MEMORY QUERY   pipeline/memory.py                         Chroma + SQLite
Stage 4  DEEP ANALYSIS  pipeline/analyze.py                        Claude Sonnet 5, adaptive thinking — the ONLY API call
Stage 5  WRITE-BACK     pipeline/memory.py, pipeline/render.py     SQLite/Chroma write-back + Markdown briefing
```

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com) installed locally with
`qwen2.5:14b` pulled (`ollama pull qwen2.5:14b`).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

Make sure Ollama is running (`ollama serve`, or the background service) before
a run — triage will log a warning and skip scoring (failing safe, not open)
if it can't reach `http://localhost:11434`.

## Running

One-off run of the full pipeline:

```bash
python -m agents.middle_east.run
```

Output lands in `briefings/middle_east_<date>.md`. Structured state lives in
`data/middle_east.sqlite3` (entities/events/relationships/theses) and
`data/chroma/` (vector store) — both are created and seeded from
`config/watchlists.yaml` on first run.

To run on the daily schedule instead of once:

```bash
python scheduler.py
```

## Configuration

- `config/watchlists.yaml` — triage threshold, memory retrieval count, Ollama
  model/host, Sonnet model/token budget, seeded entity watchlist, seeded
  standing theses, daily run time. Nothing here is hardcoded in pipeline code
  — tune it here.
- `config/sources.yaml` — GDELT country-code filters, LiveUAMap feed, RSS
  feed list. **Verify these URLs resolve from your machine before relying on
  them** — see the warning comment at the top of that file; they were not
  verified live from the environment this project was built in (network
  policy blocked GDELT/RSS/HuggingFace domains there).

## Cost tracking

Every Sonnet call is logged to `data/api_cost_log.csv` (timestamp, model,
input/output tokens, estimated cost) using Sonnet 5's introductory pricing
($2/$10 per MTok through 2026-08-31, then $3/$15). Target budget per spec is
~$10-15/month for this one agent running daily.

## What's tested vs. what isn't

This was built in a sandboxed environment whose network policy blocks
GDELT, LiveUAMap/RSS hosts, Hugging Face (embedding model download), and
Ollama's default port. As a result:

- **Verified**: SQLite schema and CRUD, Chroma write/query logic (against a
  fake in-memory collection), dedup clustering math (against a fake
  embedder), triage batching/parsing/threshold logic and its failure paths
  (against a mocked Ollama response and a real connection-refused error),
  the Sonnet prompt-building and JSON-write-back-extraction logic, cost
  estimation math, the Markdown renderer, and a full mocked end-to-end run
  of all 5 stages wired together.
- **Not verified from this environment** (needs a real run on a machine with
  network access to GDELT/RSS/HuggingFace/Ollama, and a real Anthropic API
  key): that the GDELT/LiveUAMap/RSS URLs in `config/sources.yaml` actually
  resolve and parse as expected; that `sentence-transformers` downloads and
  runs `all-MiniLM-L6-v2` correctly; that a live Ollama server running
  `qwen2.5:14b` produces well-formed triage JSON in practice; and that a real
  Sonnet 5 call produces the intended 6-part analysis. Do a real run and
  spot-check the output before trusting it for daily use.
