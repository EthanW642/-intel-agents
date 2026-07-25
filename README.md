# Intel Agents — Phase 1: Middle East Agent

Local multi-domain intelligence agent system. Phase 1 scope: the Middle
East agent, end to end, including its memory system, prediction tracking,
and email delivery. See the full build spec for later phases.

## Architecture (this phase)

```
Stage 1  INGEST         agents/middle_east/{sources.py,ingest.py}   GDELT + LiveUAMap + RSS
Stage 2a DEDUP           pipeline/dedup.py                          local embeddings (all-MiniLM-L6-v2)
Stage 2b TRIAGE          pipeline/triage.py                         local Ollama (qwen2.5:14b) — no API call
Stage 2c PREDICTION RES. pipeline/predictions.py                    local Ollama — checks pending predictions, no API call
Stage 3  MEMORY QUERY    pipeline/memory.py                         Chroma + SQLite; dormancy sweep, track record
Stage 4  DEEP ANALYSIS   pipeline/analyze.py                        Claude Sonnet 5, adaptive thinking — the ONLY API call
Stage 5  WRITE-BACK      pipeline/memory.py, pipeline/render.py,    SQLite/Chroma write-back, Markdown briefing,
         + DELIVERY      pipeline/deliver.py                        Gmail SMTP delivery
```

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com) installed locally with
`qwen2.5:14b` pulled (`ollama pull qwen2.5:14b`).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
```

Make sure Ollama is running (`ollama serve`, or the background service) before
a run — triage and the prediction resolution check will log a warning and
skip (failing safe, not open) if they can't reach `http://localhost:11434`.

For Gmail delivery: Google Account → Security → 2-Step Verification (must be
enabled) → App Passwords → generate a 16-character password for
`GMAIL_APP_PASSWORD`. This is not your regular Google password. If the Gmail
env vars are unset, the pipeline still runs and writes the local `.md`
briefing — it just skips the email step and logs why.

## Running

**Before your first real run**, verify the RSS/GDELT sources actually
resolve from your machine (spec-mandated pre-flight check):

```bash
python scripts/verify_sources.py
```

One-off run of the full pipeline:

```bash
python -m agents.middle_east.run
```

Output lands in `briefings/middle_east_<date>.md`. Structured state lives in
`data/middle_east.sqlite3` (entities/events/relationships/theses/predictions)
and `data/chroma/` (vector store) — both are created and seeded from
`config/watchlists.yaml` on first run.

To run on the daily schedule instead of once (default 5:30am local, per spec):

```bash
python scheduler.py
```

## Configuration

- `config/watchlists.yaml` — triage threshold, memory retrieval count,
  Ollama model/host, Sonnet model, effort-tier thresholds, dormancy window,
  prediction track-record threshold, seeded entity watchlist, seeded
  standing theses, daily run time. Nothing here is hardcoded in pipeline
  code — tune it here.
- `config/sources.yaml` — GDELT country-code filters, LiveUAMap feed, RSS
  feed list, each source's reliability tier (spec section 5's Tier 1-4).
  **Verify these URLs resolve from your machine** with
  `scripts/verify_sources.py` before relying on them.

Reuters is deliberately excluded from `sources.yaml` — the spec explicitly
prohibits it (Reuters retired public RSS in 2020; wire content still
reaches the pipeline indirectly via GDELT's GKG layer). Tehran Times is
included specifically because the spec calls it out as the source that
makes the stated-vs-revealed-behavior divergence check actually checkable.

**LiveUAMap is currently disabled** (`liveuamap.enabled: false` in
`sources.yaml`) — confirmed live (2026-07-23) that its free `/rss` route
302-redirects to a paid-API signup page (`.../promo/api`) regardless of
request headers; it's not bot-blocking, the free tier is gone. This is the
exact contingency the spec names for LiveUAMap ("not needed to start —
only revisit if the free RSS feed proves too thin"). `agents/middle_east/
ingest.py` skips it cleanly (logs and moves on) while disabled. To
re-enable: get a LiveUAMap API key/endpoint, update `feed_url` (and
whatever auth the paid tier needs — the current fetcher assumes a plain
RSS GET), flip `enabled: true`, and re-run `scripts/verify_sources.py`.
Phase 1 runs on the remaining 5 sources (GDELT, Times of Israel, Al
Jazeera, Tehran Times, plus GDELT's own rapid 15-minute update cadence
partially covering the "fastest-updating source" gap LiveUAMap was meant
to fill).

## The effort-tier substitution (read this if the numbers look off)

The build spec's Section 5 describes a scaled *token budget* for extended
thinking: base ~2,000 tokens + ~300/surviving item, capped at ~10,000, via
`thinking.budget_tokens`. That parameter is **rejected outright (HTTP 400)
on Sonnet 5** — it was removed from the current API. The equivalent lever
today is `output_config.effort` (`low`/`medium`/`high`/`xhigh`/`max`), so
`pipeline/analyze.py::compute_effort` translates the same *intent* — quiet
days run light, heavy days with active thesis evaluation run deep — onto
effort tiers instead of a token count.

This is deliberately conservative: `effort` is capped at `"high"` for
ordinary days purely by item/thesis-count load. Escalating past `"high"` to
`"xhigh"` or `"max"` additionally requires a real item-volume signal *and*
at least one currently-active thesis in play this run (thresholds in
`watchlists.yaml`: `xhigh_min_triaged_items`/`xhigh_min_active_theses`,
`max_min_triaged_items`/`max_min_active_theses`) — so a day with a lot of
low-signal volume but no theses in play doesn't silently burn `xhigh`
tokens, and `"high"` doesn't become the silent default the way a single
large token cap effectively would.

## Cost tracking

Two logs, both under `data/` (gitignored):

- `data/api_cost_log.csv` — every Sonnet call: timestamp, model, **effort
  tier chosen**, input/output tokens, estimated cost, using Sonnet 5's
  introductory pricing ($2/$10 per MTok through 2026-08-31, then $3/$15).
- `data/run_log.csv` — one row per pipeline run: ingestion/dedup/triage
  counts, predictions resolved this run, the effort tier and actual
  token/cost outcome, write-back counts (including theses rejected for the
  2-event bar), and email delivery outcome (sent/retried/error). This is
  what actually tracks real cost against the spec's ~$10-15/month
  projection — effort tiers are less predictable up front than a fixed
  token budget was, so watch this file for the first couple of weeks.

## What's tested vs. what isn't

This was built and tested from a sandboxed environment whose network policy
blocks GDELT, RSS hosts, Ollama's default port, and Gmail SMTP — so nothing
requiring live network access to those specific services could be run here.
Hugging Face (for the dedup/Chroma embedding model) and PyPI *were*
reachable, which is documented per-item below rather than assumed.

**Verified in this environment** (`python -m pytest tests/` — 50 tests,
all passing; also `python -m py_compile` on every file and a plain import
of every module):
- Dedup clustering math (against a fake embedder — the real
  `sentence-transformers` model itself was not exercised, see below).
- Triage batching/parsing/threshold logic and its fallback paths, against
  a mocked Ollama response.
- The prediction resolution parser and the resolve/skip logic, against a
  mocked Ollama response.
- Effort-tier computation across the low/medium/high/xhigh/max boundaries,
  including the "high is a cap, escalation needs both gates" behavior.
- Cost estimation math (intro vs. standard pricing).
- The Sonnet prompt-building logic, including the cold-start flag and
  track-record inclusion/omission.
- The JSON write-back extraction, including the 2-independent-event bar
  enforcement for new theses (rejects a thesis backed by <2 distinct
  events, including a duplicate-citation attempt) and the thesis dormancy
  sweep, all against a real (in-memory) SQLite connection.
- Prediction track-record summary generation (present/omitted based on
  the configured minimum resolved count).
- The Markdown → HTML conversion used in the email body.
- Email message construction (plaintext + HTML + attachment, static
  subject format) and the retry-once-then-give-up behavior, with SMTP
  itself mocked out — no real email was sent from this environment.
- The run-log CSV writer.
- Confirmed live from this environment: `pip install -r requirements.txt`
  succeeds cleanly (PyPI was reachable); every module in the repo imports
  without error; ingestion's log-and-skip failure handling was exercised
  for real against the actual network block (GDELT/RSS calls genuinely
  failed with a proxy error and `run_ingest()` still returned cleanly
  rather than raising); the same proxy block was hit when
  `store/chroma_client.py` tried to download the `all-MiniLM-L6-v2`
  embedding model from Hugging Face, confirming *why* the dedup test above
  has to use a fake embedder here.

**Not verified from this environment — do this on your Mac before
trusting daily use:**

1. `python scripts/verify_sources.py` — confirm GDELT and all three RSS
   feeds actually resolve and return current items from your network.
   **Already done and passing as of 2026-07-23**: GDELT + Times of Israel +
   Al Jazeera + Tehran Times all OK; LiveUAMap confirmed dead (redirects
   to a paid-API promo page) and is now disabled in `sources.yaml` — see
   the Configuration section above. Re-run after any source config change.
2. A real end-to-end run: `ollama serve` (with `qwen2.5:14b` pulled) running
   in the background, then `python -m agents.middle_east.run` with a real
   `ANTHROPIC_API_KEY` in `.env`. Check that:
   - `sentence-transformers` downloads and runs `all-MiniLM-L6-v2` without
     error (only failed here due to the sandbox's network policy, not a
     code issue, but worth confirming for real).
   - **Live-run findings log (2026-07-23/24)** — real bugs found and fixed
     while getting the first live run clean, kept here rather than
     scrubbed from history since they're exactly the kind of thing that
     resurfaces if the fix is forgotten:
     1. **GDELT actor-code overmatching.** `actor_country_codes` included
        `USA`/`TUR`, OR-matched against either actor with no requirement
        that the *other* actor be Middle East-relevant — the US alone is a
        party to a huge share of all daily world diplomatic events, so
        this matched ~27,000 GDELT events for one day (spec targets
        ~150-300/day total) and drove ~43 sequential local-triage Ollama
        batches, a real thermal-throttling risk on a fanless Air per the
        spec's own hardware note. Fixed: removed `USA`/`TUR` from
        `actor_country_codes` (a US-vs-actual-Middle-East-country event
        still matches via the other actor; Turkey-located events still
        match via `geo_country_codes`'s `TU`), raised `min_num_mentions`
        5→10, added a real `min_abs_goldstein` magnitude floor (the yaml
        comment always claimed one existed; it didn't). Locked in with
        `tests/test_gdelt_filter.py`.
     2. **GDELT date rejected as "in the future."** Took three attempts to
        actually fix, each one narrowing the diagnosis:
        - Attempt 1 (wrong): assumed UTC-vs-local was the whole story and
          switched the date computation from `datetime.now(timezone.utc)`
          to plain `datetime.now()`. Didn't fix it.
        - Attempt 2 (partially right, overcorrected): assumed gdeltPyR's
          real-world data simply didn't extend to this session's date yet,
          and had `fetch_gdelt` ask GDELT's own `lastupdate.txt` feed for
          its actual latest date instead of trusting any local clock. This
          fixed the *fetch*, but the underlying "local clock is untrustworthy"
          framing was wrong — there's no evidence the system clock is
          anything but the genuine current date.
        - Root cause (confirmed against a real `curl` of `lastupdate.txt`):
          GDELT's file timestamps are UTC (e.g. `20260725020000` = 2am UTC
          July 25), but gdeltPyR's *own* date validation compares the
          requested date against **local naive** `datetime.now()`. On a
          machine west of UTC in the evening, UTC has already rolled into
          the next calendar day while local hasn't — so GDELT's own
          correct, live UTC date can itself look "in the future" to
          gdeltPyR's local-time check. Fix: `fetch_gdelt` now clamps to
          `min(GDELT's live date, local today)`, satisfying both reference
          frames at once. Locked in with a regression test in
          `tests/test_gdelt_filter.py` using a fixed local time one day
          behind a fixed GDELT UTC date.
     3. **Sonnet call truncated at max effort, wrote back nothing.** A real
        `effort=max` call on 68 items hit the old `analysis_max_tokens:
        16000` cap during thinking, never reached the closing JSON
        write-back block, and the $0.20 call persisted zero entities/
        events/theses/predictions. Fixed: raised `analysis_max_tokens` to
        64000, matching the documented minimum for xhigh/max effort.
     4. **The 2 permanent seed theses were tripping xhigh/max effort every
        day.** The Khamenei-succession and escalation/de-escalation theses
        never expire, so `active_theses >= N` was satisfied unconditionally
        for any `N <= 2`, making `max` effort the routine outcome (and, on
        the run that surfaced bug 3, a wasted $0.20) rather than reserved
        for genuinely heavy days. Fixed: raised `xhigh_min_active_theses`/
        `max_min_active_theses` to 3/4 — verified live that a run with just
        the 2 seed theses now correctly caps at `high` regardless of item
        count, at roughly $0.17 instead of $0.20+, and that a subsequent
        real run wrote back real content (10 entities, 5 relationships, 11
        events, 3 thesis updates including one new thesis that correctly
        cleared the 2-event bar with 4 distinct supporting events, 2
        predictions) instead of zero.
     5. Added per-batch progress logging to `pipeline/triage.py` and
        `pipeline/predictions.py` — the previous log-nothing-until-the-end
        behavior made a slow-but-working run indistinguishable from a
        hung one, which is part of why (1) took a while to even notice.

     **First full clean run confirmed (2026-07-24):** cold-start handling
     ("no established pattern yet," not fabricated continuity), source
     tiering, evidence-gated thesis evaluation, the 2-hop inference cap
     with explicit hop labeling, the 2-independent-event bar for new
     theses, real memory write-back, and email delivery are all validated
     against live output, not just unit tests.

     **Re-run the pipeline after pulling these fixes** and confirm: GDELT's
     item count lands in a sane range (tens, not tens of thousands or
     zero), the run completes without a `ValueError` from GDELT, and the
     final `Write-back complete` log line shows non-zero counts (assuming
     the day's items actually warranted new entities/events).
   - The live Ollama triage call produces well-formed JSON in practice
     (the parser has a fallback path, but you want to see real output).
   - The Sonnet call actually produces the 6-part structure with sensible
     source-tier handling, cold-start language (expected on your very
     first run — the store starts empty), and confidence tagging — spot
     check it, don't just trust that it parsed.
   - `data/run_log.csv` and `data/api_cost_log.csv` get real rows with a
     plausible effort tier and cost.
3. A real test email: with `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` set, confirm
   the email actually lands (check spam too), the subject line matches the
   static `[Middle East Intel] <Month Day, Year>` format, the HTML body
   renders reasonably in your mail client, and the `.md` file is attached.
4. After ~1-2 weeks of daily runs: check that "why it matters given prior
   context" is actually citing real past events by date, not hallucinating
   continuity — this is the single failure mode the spec calls out as most
   important to guard against.
5. After several weeks (once enough predictions have resolved): check that
   the track-record summary appears in the prompt and that new predictions'
   confidence language visibly responds to it, not just note it.
