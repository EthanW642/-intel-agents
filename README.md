# Intel Agents — Phase 1: Middle East Agent

Local multi-domain intelligence agent system. Phase 1 scope: the Middle
East agent, end to end, including its memory system, prediction tracking,
and email delivery. See the full build spec for later phases.

## Architecture (this phase)

```
Stage 1  INGEST         agents/middle_east/{sources.py,ingest.py}   GDELT (direct 15-min export files) + RSS
Stage 2a SEEN/DEDUP      store (seen_urls) + pipeline/dedup.py      already-seen URL filter, exact URL dedup,
                                                                    then local embeddings (all-MiniLM-L6-v2)
Stage 2b TRIAGE          pipeline/triage.py                         local Ollama (qwen2.5:7b) — no API call
Stage 2c PREDICTION RES. pipeline/predictions.py                    local Ollama — checks pending predictions, no API call
Stage 3  MEMORY QUERY    pipeline/memory.py                         Chroma + SQLite; dormancy sweep, track record
Stage 3b OIL SNAPSHOT    pipeline/oil_prices.py                     EIA API — WTI/Brent spot prices, free, optional
Stage 3c SATELLITE HOTSPOTS pipeline/satellite_hotspots.py          NASA FIRMS — thermal anomalies, free, optional
Stage 4  DEEP ANALYSIS   pipeline/analyze.py                        Claude Sonnet 5, adaptive thinking — the ONLY API call
Stage 5  WRITE-BACK      pipeline/memory.py, pipeline/render.py,    SQLite/Chroma write-back, Markdown briefing,
         + DELIVERY      pipeline/deliver.py                        Gmail SMTP delivery
```

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com) installed locally with
`qwen2.5:7b` pulled (`ollama pull qwen2.5:7b`). **If your machine has 32GB+
RAM**, `qwen2.5:14b` (the original default) will likely give better triage
judgment — see the live-run findings log below for why 7b became the
default (14b needed more memory than a 16GB Mac had, causing severe
slowdowns and silent data loss to timeouts). Whichever you use, set
`triage_model` in `config/watchlists.yaml` to match.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EIA_API_KEY, FIRMS_MAP_KEY
```

Make sure Ollama is running (`ollama serve`, or the background service) before
a run — triage and the prediction resolution check will log a warning and
skip (failing safe, not open) if they can't reach `http://localhost:11434`.

For Gmail delivery: Google Account → Security → 2-Step Verification (must be
enabled) → App Passwords → generate a 16-character password for
`GMAIL_APP_PASSWORD`. This is not your regular Google password. If the Gmail
env vars are unset, the pipeline still runs and writes the local `.md`
briefing — it just skips the email step and logs why.

For the oil price snapshot (optional, added 2026-07-31): register for a free
EIA API key at [eia.gov/opendata/register.php](https://www.eia.gov/opendata/register.php)
(no credit card) and set `EIA_API_KEY`. If unset, the pipeline still runs
normally — it just omits the oil snapshot section from the analysis prompt.

For satellite thermal-anomaly detection (optional, added 2026-07-31):
register for a free NASA FIRMS key at
[firms.modaps.eosdis.nasa.gov/api/map_key](https://firms.modaps.eosdis.nasa.gov/api/map_key/)
(no credit card) and set `FIRMS_MAP_KEY`. If unset, the pipeline still runs
normally — it just omits the hotspots section. **Read the caveat in
`pipeline/satellite_hotspots.py`'s module docstring before trusting this**:
it detects heat, not confirmed strikes, and this region's routine gas
flaring will show up in it constantly.

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
`data/middle_east.sqlite3` (entities/events/relationships/theses/predictions/
seen_urls) and `data/chroma/` (vector store) — both are created and seeded
from `config/watchlists.yaml` on first run.

**Upgrading from a pre-August-2026 checkout?** The old Chroma collection
stored its own embedding-function config, which the shared-embedder client
no longer uses (see `store/chroma_client.py`) — delete `data/chroma/` and
rebuild it from the events already in SQLite (no memory is lost):

```bash
rm -rf data/chroma
python scripts/rebuild_chroma.py
```

Also re-run `pip install -r requirements.txt`; the `gdelt` and `pandas`
dependencies are gone (`pip uninstall gdelt pandas` to reclaim the space).

### Volume/memory architecture (August 2026 optimization pass)

The pipeline is shaped as a funnel where free operations do the killing
before anything expensive runs — the fix for real runs that ingested
~10,000 raw items, held multi-GB pandas frames, and pushed 200+ items into
the paid Sonnet call:

- **GDELT** is fetched by streaming each 15-minute export file directly
  (download → filter → discard, ~4 in flight), replacing gdeltPyR's
  coverage=True bulk pull that concatenated the whole window into one
  multi-GB DataFrame before filtering. Same spec-4.1 filter semantics,
  verified by the same tests (`tests/test_gdelt_filter.py`).
- **seen_urls** (SQLite) drops anything a previous successful run already
  processed, and RSS entries older than `rss_max_age_days` age out at
  ingest — steady-state daily volume is genuinely-new items only. URLs are
  marked seen only after a fully successful run, so crashes and the
  Ollama-outage abort reprocess rather than lose items.
- **Exact URL dedup** (free) runs before embedding; the near-dup pass
  compares incrementally against kept items instead of building an
  n x n similarity matrix (~800MB at 10k items).
- **One embedding model**: dedup, memory query, and Chroma write-back all
  share the single sentence-transformers instance in `pipeline/dedup.py`
  (Chroma previously loaded a second copy).
- **`analysis_max_items`** caps what reaches Sonnet (top triage scores
  kept), bounding cost on heavy days and whenever triage fails open.

To run on the daily schedule instead of once (default 5:30am local, per spec):

```bash
python scheduler.py
```

**This requires the Mac to stay powered on continuously** (`scheduler.py`
just blocks the terminal and fires at 5:30am — a fully shut-down machine
can't run it, and can't be woken by it either). If your Mac is fully
shut down overnight rather than left on, use the boot/login trigger
instead — see below.

### Running when the Mac is off overnight

A fully powered-off Mac cannot be woken on a schedule by software — there's
no way around this on a laptop (macOS's `pmset schedule wakeorpoweron` claims
to, but is unreliable on modern Macs, especially Apple Silicon, and a
silently-skipped morning is worse than an honestly-late one). Confirmed live
2026-07-29: given that hard constraint, the practical alternative is running
once per boot/login instead of at a fixed clock time —
`scripts/run_if_not_already_today.py` checks `data/run_log.csv` for a row
dated today and skips if the pipeline already ran, so logging in more than
once a day doesn't produce duplicate runs or duplicate emails.

Install as a macOS LaunchAgent (fires at login, and retries hourly after
that in case the first attempt lands before Ollama/network are ready —
harmless no-ops once the day's run has actually succeeded):

```bash
cp scripts/com.intel-agents.middle-east-daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.intel-agents.middle-east-daily.plist
```

Check it's loaded: `launchctl list | grep intel-agents`. Uninstall with
`launchctl unload ~/Library/LaunchAgents/com.intel-agents.middle-east-daily.plist`
then delete the copied plist. **Edit the paths inside the plist first** if
your checkout or venv isn't at `/Users/ethanwallace/Projects/-intel-agents`.

**Wrapped in `caffeinate -i`.** Confirmed live 2026-07-30: without it, the
LaunchAgent *did* fire correctly on a real cold boot/login — but with
nobody at the keyboard, the Mac's own idle sleep froze the whole process
mid-run about a minute in (`pmset -g` showed a 1-minute idle sleep
timeout; the stuck process had accumulated only ~10 seconds of real CPU
time after 10+ hours). `caffeinate -i <command>` holds off idle sleep
only while `<command>` runs, and releases it automatically once the
pipeline finishes, so the Mac still sleeps normally the rest of the day.
If you customize the plist's `ProgramArguments`, keep `caffeinate -i` as
the first two entries.

The real trade-off versus `scheduler.py`: you get the briefing shortly
after you next turn the Mac on and log in, not guaranteed at 5:30am — if
you don't boot it until 9am, that's when it runs. There's no way to get
genuine fixed-time delivery without something that's never fully off
(a cloud VM, a Raspberry Pi, etc.) running the pipeline instead of this
laptop — a materially bigger change than a scheduler tweak.

## Configuration

- `config/watchlists.yaml` — triage threshold, memory retrieval count,
  Ollama model/host, Sonnet model, effort-tier thresholds, dormancy window,
  prediction track-record threshold, seeded entity watchlist, seeded
  standing theses, daily run time. Nothing here is hardcoded in pipeline
  code — tune it here.
- `config/sources.yaml` — GDELT country-code filters, RSS feed list, each
  source's reliability tier (spec section 5's Tier 1-4). **Verify these
  URLs resolve from your machine** with `scripts/verify_sources.py` before
  relying on them.
- `prompts/analysis_system.md` — the Stage 4 (Sonnet) system prompt.
  Rewritten 2026-07-31 to a full ICD 203-style tradecraft standard: adds a
  bolded BLUF, an estimative-language lexicon (`almost no chance` through
  `almost certain`, mapped to percentage bands) with probability kept
  distinct from confidence, seven analytical lenses (geography/logistics,
  domestic politics, historical precedent — with a *mandatory* stated
  disanalogy, law/legitimacy, economics, military-technical, social/
  religious/informational) feeding the second-order-implications section,
  and a competing-hypotheses step (with a required devil's-advocate
  sentence) for the day's single most consequential ambiguity. The JSON
  write-back contract is byte-for-byte unchanged from the prior prompt, so
  `pipeline/memory.py`'s write-back validation needed no code changes —
  this was a pure prompt swap. **Confirmed live 2026-07-31**: 82-item day,
  `output_tokens=33723` (well under the 128K ceiling, actually lower than
  the old prompt's previous heaviest day), clean write-back
  (`malformed_entries_skipped=0`, all 4 thesis-status updates matched
  existing titles exactly, the new thesis cleared the 2-event bar with 2
  genuinely distinct events) — see the live-run findings log below.
- `pipeline/oil_prices.py` — the EIA oil price snapshot fetcher (WTI/Brent
  spot prices, added 2026-07-31). Requires `EIA_API_KEY` in `.env` (free,
  see Setup above) — omitted from the analysis prompt entirely if unset or
  if the fetch fails, same graceful-degradation pattern as Gmail
  credentials. Not yet confirmed live from this build environment
  (network-restricted sandbox, same as every other external source here)
  — the EIA API v2 query-parameter shape was built from published
  documentation, not a live response. See the live-run findings log below.
- `pipeline/satellite_hotspots.py` — NASA FIRMS satellite thermal-anomaly
  fetcher (added 2026-07-31), answering "where are strikes hitting" with
  real sensor data instead of relying on prose descriptions in articles.
  Requires `FIRMS_MAP_KEY` (free, see Setup above) — omitted from the
  prompt entirely if unset or the fetch fails. **Read the module's own
  docstring before trusting this**: FIRMS detects heat, not confirmed
  strikes specifically, and this region's routine gas flaring (Iraq, Gulf
  states) will show up constantly. Deliberately unfiltered beyond FIRMS'
  own confidence field — the prompt carries an explicit
  corroboration-required caveat instead of the pipeline trying to
  locally suppress "noise," matching how GDELT's inherent noisiness is
  already handled by the tiering/corroboration rules rather than local
  filtering. Not yet confirmed live from this build environment — the
  FIRMS area-CSV query shape was built from published documentation, not
  a live response. See the live-run findings log below.

Reuters, AP, and AFP — all three major global wire agencies — don't
maintain an official public RSS feed anymore. The spec explicitly calls
this out for Reuters (retired in 2020); live checks on 2026-07-31
confirmed AP is in the same position (no first-party feed, only
unofficial third-party scrapers), and so is AFP — AFP has stated its full
public RSS is deliberately off, since a free feed would compete with its
own paying syndication clients. This isn't a gap specific to any one
agency, it's structural to how wire agencies distribute content now, and
none of the three is a source this pipeline should depend on an
unofficial *scraper* for. Tehran Times is included specifically because
the spec calls it out as the source that makes the
stated-vs-revealed-behavior divergence check actually checkable.

**AP and Reuters are reached anyway**, via a different mechanism added
2026-07-31: Google News RSS search scoped with `site:apnews.com` /
`site:reuters.com` (`rss_feeds` entries "AP — via Google News" and
"Reuters — via Google News", Tier 2). This isn't an unofficial scraper —
it's Google's own search index, returning links to the outlet's actual
articles — so it gets genuine AP/Reuters content without depending on
either agency's (nonexistent) RSS infrastructure. Tagged Tier 2, the
outlet's real tier, not Tier 4: every item genuinely is that outlet's own
reporting, Google is just standing in as the delivery mechanism. Axios
got the same treatment ("Axios — via Google News", Tier 2 — the spec
names Axios as a Tier 2 example directly) even though Axios does publish
a general RSS feed: it has no Middle East-specific vertical, so a
`site:axios.com` query reaches the ME-relevant subset directly rather
than pulling Axios' mostly US-domestic-policy general feed and relying
entirely on triage to filter it. All three are wire content that would
otherwise only reach the pipeline indirectly via GDELT's GKG layer.

**International wire-style sources added 2026-07-31** (BBC News — Middle
East, The Guardian — Middle East, NPR — Middle East, all Tier 2) to
balance the two Tier 3 aligned/interpretive sources already in the file
(Times of Israel, Tehran Times) with outlets that aren't a party to the
conflict themselves. France 24 was the initial pick here but was swapped
for The Guardian the same day, per explicit preference for a source
closer to Reuters/AP than a state-funded broadcaster — The Guardian is
independently owned (Scott Trust), not government-funded like
BBC/France 24/NPR. **Verified live 2026-07-31**, all three: BBC (30
entries), NPR (10 entries), and The Guardian (its first URL guess 404'd,
corrected to the unhyphenated tag slug and confirmed live the same day) —
see the live-run findings log below for the Guardian URL correction.

**Two more added the same day, both spec-named candidates that were never
previously wired in**: **Al-Monitor** (Tier 2 — a Middle East specialist
outlet, analytical/translation-driven rather than raw wire copy, not
aligned to one side of the conflict) and **Haaretz** (Tier 3 — an Israeli
domestic paper, deliberately the Times of Israel's opposite-leaning
counterpart: known for reporting critical of the Israeli government,
capturing internal Israeli dissent that Times of Israel's more mainstream
framing doesn't; still Tier 3 because nationality/proximity to a party in
the conflict drives the tier, not the paper's stance toward its own
government). **Verified live 2026-07-31**: both resolved on the first URL
guess (Al-Monitor 20 entries, Haaretz 20 entries, both same-day).

**LiveUAMap was removed 2026-07-31** (not just disabled — the config block
and its special-case fetcher code are both gone; see git history if you
need the old `feed_url`/rationale). It had been disabled since 2026-07-23,
when its free `/rss` route started 302-redirecting to a paid-API signup
page (`.../promo/api`) regardless of request headers — not bot-blocking,
the free tier is just gone. This is the exact contingency the spec names
for LiveUAMap ("not needed to start — only revisit if the free RSS feed
proves too thin"). In its place: a **Google News RSS search query**
(`rss_feeds` entry "Google News — Middle East", Tier 4), added the same
day as a genuine functional replacement rather than another named outlet
— it's an official Google endpoint (no API key, no signup) that
aggregates across a large, uncurated set of publishers in near-real time,
which is the same high-recall/low-individual-reliability role LiveUAMap
held, not just a topical similarity. It slots into the exact same generic
RSS fetch/dedup/triage/`verify_sources.py` path every other feed uses — no
special-case code needed, unlike LiveUAMap's old bespoke fetcher.
**Verified live 2026-07-31**: 100 entries, newest same-day.

Phase 1 runs on 13 sources total: GDELT, Times of Israel, Al Jazeera,
Tehran Times, BBC, The Guardian, NPR, Al-Monitor, Haaretz, the general
Google News search query (which also partially covers the
"fastest-updating source" gap LiveUAMap was meant to fill, alongside
GDELT's own rapid 15-minute update cadence), and the three site:-scoped
Google News queries (AP, Reuters, Axios).

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
`"xhigh"` additionally requires a real item-volume signal *and* at least
one currently-active thesis in play this run (thresholds in
`watchlists.yaml`: `xhigh_min_triaged_items`/`xhigh_min_active_theses`) —
so a day with a lot of low-signal volume but no theses in play doesn't
silently burn `xhigh` tokens, and `"high"` doesn't become the silent
default the way a single large token cap effectively would. `compute_effort`
never returns `"max"` — confirmed live 2026-07-26 that an active-theses-
count gate meant to reserve `"max"` for genuinely rare heavy days instead
becomes permanently satisfied once a maturing memory store accumulates a
handful of real, non-dormant theses (an expected outcome, not an anomaly,
for a domain watching a persistently active conflict), making `"max"` the
routine outcome instead of a rare one. `"xhigh"` is Sonnet 5's own
documented top recommended tier for agentic/reasoning work, so the
pipeline caps there — see finding #12 in the live-run findings log below.

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

**Verified in this environment** (`python -m pytest tests/` — 125 tests,
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
- The Sonnet prompt-building logic, including the cold-start flag,
  track-record inclusion/omission, oil-snapshot inclusion/omission
  (present with real numbers, omitted entirely when `None`, missing
  1-day/7-day change values rendered as "no comparison available" rather
  than crashing on `None` arithmetic), and satellite-hotspot
  inclusion/omission (present with lat/lon/confidence/FRP, omitted
  entirely when `None`, the corroboration-required caveat text always
  present when hotspots appear — regression-guarded so a future edit
  can't silently drop it, missing FRP rendered without a dangling "FRP"
  label rather than crashing).
- The EIA oil price fetcher (`pipeline/oil_prices.py`): successful
  fetch/parse for both WTI and Brent, one series failing while the other
  still returns, both series failing returns `None` (prompt section
  omitted, not a crashed run), malformed rows (missing `value`) skipped
  rather than raising, and the 7-day lookback correctly picks the closest
  available trading day rather than requiring an exact calendar match —
  all against a mocked `httpx.get`, no real EIA API call made.
- The NASA FIRMS satellite hotspot fetcher
  (`pipeline/satellite_hotspots.py`): successful CSV fetch/parse, "low"
  confidence rows excluded while nominal/high pass through, results
  sorted most-recent-first, results capped at `MAX_HOTSPOTS`, an
  HTTP-level failure or error status returns `None` (not a crashed run),
  malformed rows (non-numeric latitude) skipped rather than raising, and
  a response where every row gets filtered out correctly returns `None`
  rather than an empty-but-truthy list — all against a mocked
  `httpx.get`, no real FIRMS API call made.
- `pipeline/ollama_client.py::unload_model()`: sends the correct
  Ollama unload request shape (`messages: []`, `keep_alive: 0`), and
  never raises on a connection failure or an HTTP error status —
  confirming a broken cleanup call can't turn into a lost run — all
  against a mocked `httpx.post`. Also an orchestration-level test on
  `agents/middle_east/run.py` confirming `unload_model` fires exactly
  once, after prediction resolution and before the Sonnet call — not
  skipped, not duplicated, not fired too early.
- The Sonnet call's retry-once-after-60s behavior on genuinely retryable
  errors (connection/timeout, 429, 500, 529 overloaded) and that it does
  *not* retry non-retryable errors (e.g. 400 bad request) or retry more
  than once — see finding #13 in the live-run findings log below. The
  Anthropic client itself is mocked; no real API call is made.
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

1. `python scripts/verify_sources.py` — confirm GDELT and all RSS feeds
   actually resolve and return current items from your network. **Done and
   passing as of 2026-07-31, all 13 sources OK**: GDELT, Times of Israel,
   Al Jazeera, Tehran Times (verified 2026-07-23), plus BBC News — Middle
   East, NPR — Middle East, The Guardian — Middle East (its first URL
   guess 404'd, corrected to the unhyphenated tag slug, then confirmed
   live — see the live-run findings log below), Al-Monitor, Haaretz, the
   general "Google News — Middle East" query that replaced LiveUAMap, and
   the three `site:`-scoped Google News queries for AP, Reuters, and Axios
   (this whole batch verified 2026-07-31). All four Google News-based
   entries returned exactly 100 entries each — looks like Google News
   RSS's per-request page cap, not a config problem, but worth watching
   whether it caps real coverage on a heavy-news day. Still unconfirmed:
   whether these entries' item links are bare outlet URLs
   (`apnews.com/...`) or Google redirect links
   (`news.google.com/rss/articles/...`) — either works for the pipeline,
   but it affects what a human clicking through from the briefing lands
   on; check a rendered briefing once real items flow through. Re-run
   after any source config change.
2. A real end-to-end run: `ollama serve` (with `qwen2.5:7b` pulled — see
   Setup above for why not 14b) running in the background, then
   `python -m agents.middle_east.run` with a real `ANTHROPIC_API_KEY` in
   `.env`. Check that:
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
     6. **Literal `"nan"` in GDELT actor names.** A real run's analysis
        input visibly contained the string "nan" as an actor name. Cause:
        `row.get("Actor1Name") or "unknown actor"`-style fallbacks don't
        catch pandas' missing-value representation, because a missing cell
        is a float `NaN` and `NaN` is *truthy* in Python
        (`bool(float("nan")) is True`) — the `or` never fires. Fixed with
        `_clean_gdelt_actor_name()`, which uses `x != x` (the standard NaN
        self-inequality check) to actually detect it; rows where *neither*
        actor is identifiable are now dropped entirely rather than
        described as "unknown actor -> unknown actor," which is pure noise.
        Locked in with `tests/test_gdelt_filter.py`.
     7. **An Ollama outage looked identical to a quiet news day.** A real
        run had Ollama unreachable (`ollama serve` not running) for the
        entire run — all 19 triage batches and the prediction-resolution
        batch failed with `httpx.ConnectError: Connection refused`. The
        pipeline didn't crash (by design, connection failures are caught
        per-batch), but it then proceeded anyway to a Sonnet call that
        correctly reported "no items survived triage" — a $0.02 email
        that, in isolation, is indistinguishable from a genuinely quiet
        day. Fixed: `triage_items`/`resolve_predictions` now raise
        `OllamaUnavailableError` (`pipeline/ollama_client.py`) when *every*
        batch in a run failed to even reach Ollama (a partial outage —
        some batches fine, some not — still degrades gracefully as before,
        since that's a real signal, not an infrastructure failure).
        `agents/middle_east/run.py` catches this specifically, aborts
        before the Sonnet call entirely (no spend on a run with nothing
        real to analyze), and sends a distinct plain-text alert email
        (`pipeline/deliver.py::send_ollama_outage_alert`) instead of a
        briefing, so an outage is never silently mistaken for "nothing
        happened." Locked in with `tests/test_triage.py`,
        `tests/test_predictions.py`, `tests/test_deliver.py`, and an
        orchestration-level test in `tests/test_run.py`.

     8. **Triage silently scored nothing — 0 genuine, 100% parse-fallback.**
        A real run showed `383 items scored, 383 survived ... 0 were
        genuine model scores and 383 were parse-fallback pass-throughs` —
        every batch. Worse, the inflated (unfiltered) item count also
        tripped `xhigh` effort, so the run cost $0.42 analyzing essentially
        raw, untriaged input. Root cause, confirmed via a zero-cost
        diagnostic (`scripts/debug_triage_output.py`) that calls Ollama
        directly outside the full pipeline: the old `"format": "json"`
        request only guarantees *valid* JSON, not a particular shape — on
        a 2-item batch, qwen2.5:14b returned a single `{"index": 0,
        "score": ..., "reason": ...}` object instead of a 2-element array,
        silently ignoring the prompt's "one entry per item" instruction.
        `json.loads` succeeded (so no parse error was ever logged), but
        iterating the resulting dict's keys as if they were array entries
        threw `TypeError` on every one, which the existing per-entry
        `except` swallowed — so every item in every batch fell through to
        the fallback path with no visible error at all. Fixed: `call_ollama`
        now accepts a `response_format` (a JSON Schema, not just the bare
        `"json"` string) — Ollama's actual mechanism for constraining
        output shape — and both `pipeline/triage.py` and
        `pipeline/predictions.py` now build a schema per batch with
        `minItems`/`maxItems` pinned to that batch's exact size, forcing
        one entry per input item rather than trusting the model to comply
        with a prose instruction. Locked in with schema-shape tests and a
        test asserting the schema is actually passed to `call_ollama` with
        the right batch size, in `tests/test_triage.py` and
        `tests/test_predictions.py`.
     9. **Prediction resolution: 14/14 resolved, 0/10 confirmed, 10/10
        contradicted — from a context window blowout, not bad reasoning.**
        With triage genuinely working (finding #8), a real run then showed
        every one of 14 pending predictions resolved (the system prompt
        explicitly says pending should be the default on most days), and
        the last 10 were unanimously "contradicted." Querying
        `resolution_note` directly from SQLite showed why: literally every
        "contradicted" reason cited a totally unrelated item (a film review
        at a cultural center, a football signing, mining-sector reforms) —
        the same handful of unrelated topics recurring across unrelated
        predictions, the signature of the model working from a tiny,
        essentially arbitrary leftover slice of input. Root cause:
        `resolve_predictions` was called with all ~393 **deduped** items
        (not the 62 already-**triaged** ones), and `_build_items_block` has
        no length cap — Ollama's default context window (2048-4096 tokens)
        was blown out and silently truncated, no error. Confirmed via
        `scripts/debug_prediction_context.py`: a 1-prediction, 351-item test
        prompt came to ~33,012 estimated tokens; even explicitly setting
        `num_ctx=16384` wasn't enough, and the model returned a nonsense
        out-of-range index (17, then 35) for a batch where the only valid
        index was 0. Fixed three ways: (1) `agents/middle_east/run.py`
        Stage 2c now passes `triaged`, not `deduped` — a ~6x reduction, and
        arguably more correct anyway since a prediction can only be
        legitimately resolved by something that already cleared the
        relevance bar; (2) `call_ollama` gained an explicit `num_ctx`
        parameter, and both `pipeline/triage.py` (8192) and
        `pipeline/predictions.py` (16384) now set it explicitly rather than
        trusting Ollama's small default; (3) both response schemas now
        bound `index` (and triage's `score`) to a valid range, so an
        out-of-range value is rejected at the schema level instead of
        silently corrupting a result. Locked in with
        `tests/test_run.py::test_prediction_resolution_receives_triaged_items_not_deduped`
        and schema-bounds tests in `tests/test_triage.py`/
        `tests/test_predictions.py`.
     10. **`write_back` had no protection against a malformed entry from
         Sonnet.** Found via a fresh full-project audit, not a live crash:
         unlike the Ollama calls (triage, prediction resolution), which now
         use schema-constrained structured output, Sonnet's JSON write-back
         block has no schema enforcement at all — it's prompt-following
         inside a larger prose response, so a missing field on one
         entity/event/relationship is plausible. `write_back` used direct
         dict access (`evt["date"]`, `rel["entity_a"]`, etc.) with no
         per-item error handling, so one bad entry would raise and crash
         the *entire* write-back — and since write-back runs before
         `render_briefing`/email in `run.py`, that would mean money already
         spent on the Sonnet call but no briefing rendered and no email
         sent that day. Fixed: each per-item loop in `write_back`
         (`new_entities`, `new_relationships`, `new_events`,
         `thesis_updates`, `new_predictions`) now catches
         `KeyError`/`TypeError`/`ValueError` per entry, logs a warning, and
         continues — one malformed entry no longer costs the rest of the
         write-back or the render/email steps that follow. The summary dict
         (and `data/run_log.csv`) gained a `malformed_entries_skipped`
         count so this is visible if it ever happens, not silent. Also
         removed genuinely dead code found in the same pass: `api_call_log`
         (SQLite table + `log_api_call` function) was never called anywhere
         — cost logging actually happens via `data/api_cost_log.csv`
         instead. Locked in with 5 new tests in `tests/test_memory_and_db.py`.
     11. **`analysis_max_tokens: 64000` truncated again — a second time,
         at a higher bar.** With triage genuinely working (finding #8) and
         a real heavy day (73 items survived triage — a correct `max`-
         effort escalation, not a bug), the Sonnet call hit
         `output_tokens=64000` exactly, logged `No JSON write-back block
         found in analysis output`, wrote back nothing, and cost $0.6818 —
         the single most expensive failed call of the whole project.
         `max_tokens` caps thinking + response text *combined*, and at
         `effort=max` on a heavy day, thinking alone can consume the
         entire budget before the model reaches the closing JSON block.
         64000 was itself a prior fix (see #3 above) for the same failure
         mode at a lower bar (68 items, `analysis_max_tokens: 16000` at
         the time) — it wasn't wrong then, it just wasn't the actual
         ceiling. Rather than guess a third intermediate number, checked
         Sonnet 5's documented output limit directly: **128000 tokens**,
         supported with streaming (which `pipeline/analyze.py` already
         uses via `client.messages.stream(...)`). Raised
         `analysis_max_tokens` to 128000 — the real ceiling, not another
         guess. `max_tokens` is a cap, not a spend target: this doesn't
         make ordinary days cost more, it just removes the truncation
         risk on genuinely heavy ones instead of pushing the same failure
         to a higher item count.
     12. **`effort=max` had quietly become the routine daily outcome, not
         a rare escalation.** With the 128000-token fix confirmed working
         (finding #11 — no truncation, full write-back), the next 3
         consecutive real runs still all hit `effort=max`, at climbing
         cost: $0.36 → $0.68 → $1.01. The `active_theses >= 4` gate meant
         to reserve `max` for genuinely rare heavy days had, by
         2026-07-26, been permanently satisfied — a real query against
         the live database confirmed exactly 4 theses sitting at
         `reinforced` status (Khamenei succession, escalation/
         de-escalation signaling, regional widening, and a newly-formed
         West Bank thesis), none anywhere near the 14-day dormancy
         window. That's not an anomaly; it's the expected, healthy
         outcome of a memory store maturing while watching a genuinely
         multi-front active conflict — but it meant the thesis-count gate
         could never again *un*-clear, leaving only the item-count gate
         doing any real work, which real triage volume cleared on 3
         consecutive days. This is the exact "high becomes the silent
         default" failure `compute_effort` was designed to prevent —
         just one tier up, and now permanent rather than occasional.
         Fixed: removed the `max` effort tier from `compute_effort`
         entirely (and the now-dead `max_min_triaged_items`/
         `max_min_active_theses` config keys) — `xhigh` is Sonnet 5's own
         documented top recommended tier for agentic/reasoning work, so
         the pipeline now caps there, with no code path to `max` at all
         rather than a threshold that could quietly re-permanentize
         itself again as the memory store keeps growing. Locked in with
         `tests/test_analyze.py::test_effort_never_reaches_max` (asserts
         the cap holds even at absurdly extreme inputs).
     13. **A transient Anthropic-side 529 overload threw away a whole
         run's already-completed free local work.** A real run on
         2026-07-29 got through ~10 minutes of ingest/dedup/triage/
         prediction-resolution (all free, local) and then the Sonnet call
         — the pipeline's only paid, only network-dependent-on-Anthropic
         stage — failed with `{'type': 'overloaded_error', 'message':
         'Overloaded'}` (HTTP 529: Anthropic's own servers at capacity,
         not a bug on this end). The Anthropic SDK already retries 429/5xx
         internally (`max_retries=2`, short backoff) before raising, so by
         the time this surfaced here those quick retries were already
         exhausted, and the whole pipeline aborted, discarding all the
         completed local stages with it. Fixed: wrapped just the Sonnet
         call in `pipeline/analyze.py` with a retry-once-after-a-longer-
         delay (60s, vs. `pipeline/deliver.py`'s 30s for SMTP — an API
         overload plausibly takes longer to clear than an SMTP hiccup),
         scoped to genuinely retryable errors only
         (`APIConnectionError`/`APITimeoutError`, `RateLimitError`,
         `InternalServerError`, `OverloadedError`) — a 400/401/403/404/
         413/422 means the request itself is wrong (bad prompt, bad key,
         oversized input) and retrying would just waste 60s reproducing
         the same failure. If the retry also fails, the error still
         propagates to `run.py`'s existing outer handler (same as before —
         this doesn't hide a genuine, sustained outage), it just gives one
         real overload a chance to clear before giving up on ~10 minutes
         of local work. Covered by
         `tests/test_analyze.py::test_run_analysis_retries_once_after_overloaded_error`
         (mocked: fails once, succeeds on retry),
         `::test_run_analysis_does_not_retry_forever_when_second_attempt_also_fails`
         (fails twice, propagates rather than looping), and
         `::test_run_analysis_does_not_retry_non_retryable_errors` (a 400
         is not retried at all). Not yet confirmed live against a real
         second overload — the next time this fires for real, check
         `data/run_log.csv` and the logs for the "retrying once in 60s"
         line.
     14. **Guessed RSS URL for a new source 404'd on first try.** When The
         Guardian — Middle East was added to `sources.yaml`, its URL was
         guessed by analogy to other Guardian RSS feeds
         (`.../world/middle-east/rss`, hyphenated like their article URLs)
         rather than fetched live from this build environment. A real
         `scripts/verify_sources.py` run on 2026-07-31 caught it
         immediately: `404 Not Found`. The Guardian's tag slugs (unlike
         article slugs) generally don't use hyphens —
         `.../world/middleeast/rss` was the corrected guess, and that one
         verified live. Not a code bug, but a reminder of why every
         guessed source URL in this file is explicitly flagged
         UNVERIFIED until a real `scripts/verify_sources.py` run confirms
         it — a plausible-looking URL built by analogy to a working
         pattern elsewhere on the same site can still be wrong.
     15. **Analysis prompt rewritten to a full ICD 203-style tradecraft
         standard (2026-07-31).** Not a bug fix — a deliberate upgrade,
         drafted by a separate Claude session and reviewed/merged in here
         after checking it against the actual write-back code and the
         current source roster. Adds: a bolded BLUF; an estimative-language
         lexicon (`almost no chance` 1-5% through `almost certain` 95-99%)
         with probability kept explicitly distinct from confidence; seven
         analytical lenses (geography/logistics, domestic politics,
         historical precedent with a *mandatory* stated disanalogy,
         law/legitimacy, economics, military-technical, social/religious/
         informational) feeding the second-order-implications section, each
         one skipped rather than padded when today's evidence gives it
         nothing to say; and a competing-hypotheses step with a required
         devil's-advocate sentence for the day's single most consequential
         ambiguity. Reasoning protocol grew from 8 steps to 10 (added
         explicit conflict handling and the competing-hypotheses step) —
         checked every internal cross-reference across the renumbering
         (self-critique citing "steps 1-8," confidence tagging citing
         "step 7"/"step 6," output section 4 citing "step 7"/"step 8") and
         none drifted.

         The source-tier examples in the draft as originally written were
         stale — still listing "AP, Al Jazeera, Axios" for Tier 2, "Times
         of Israel, Middle East Eye, Tehran Times" for Tier 3, and
         "LiveUAMap" for Tier 4, none of which matches the 13-source
         roster this session had just finished building (see findings
         above). Middle East Eye in particular was never actually wired
         into `sources.yaml` at all — a leftover from the original spec
         text, not a real source. Fixed before merging: Tier 2 now lists
         Al Jazeera, BBC, The Guardian, NPR, Al-Monitor, and the
         site:-scoped AP/Reuters/Axios entries; Tier 3 adds Haaretz; Tier 4
         points at the Google News query, not LiveUAMap.

         The JSON write-back contract is unchanged from the prior prompt,
         so this was a pure prompt-file swap — no changes needed to
         `pipeline/analyze.py`'s `_extract_json_block` regex or
         `pipeline/memory.py`'s write-back validation, and all 96 existing
         tests still pass untouched. This is a real, non-trivial increase
         in what's asked of the model each run (seven lenses, explicit
         hop-labeling inside lens arguments, confidence tagging over more
         content), so the concern going in was `output_tokens` creeping
         back toward Sonnet 5's 128K ceiling with no higher number left to
         raise it to.

         **Confirmed live 2026-07-31, first real run on the new prompt**:
         an 82-item day (the heaviest triaged-item count logged yet, more
         than the old prompt's previous heaviest at 73 items) produced
         `output_tokens=33723` — about 26% of the ceiling, and actually
         *lower* than the old prompt's own heaviest day (98135 tokens on
         73 items). The tighter structure (explicit "skip a lens with
         nothing to say, don't pad it," capped hop chains, evidence-gated
         thesis handling) seems to make output more disciplined, not
         longer. Cost was $0.396, in the normal range. Write-back was
         fully clean: `malformed_entries_skipped=0`,
         `thesis_updates_applied=5` (4 reinforced + 1 new), all 4
         reinforced-thesis titles matched existing SQLite rows
         character-for-character (checked directly:
         `SELECT title, status FROM theses` — no stray duplicate theses
         created), and the new thesis cited exactly 2 distinct supporting
         events, clearing the 2-event bar for real rather than being
         padded. BLUF, the estimative lexicon (with probability and
         confidence stated as separate axes), two historical analogies
         each with a genuine stated disanalogy, a full competing-
         hypotheses/devil's-advocate pass, and explicit hop-labeling all
         showed up correctly in the live output. The earlier token-budget
         concern did not materialize on this run.
     16. **Oil price snapshot added (2026-07-31), not yet confirmed
         live.** New `pipeline/oil_prices.py` fetches daily WTI/Brent spot
         prices from the EIA's free official API and injects them into
         Stage 4's prompt as a new "Oil price snapshot" section, feeding
         the "Economics & markets" analytical lens real numbers instead
         of relying on narrative claims like "oil prices rose" from
         articles — the same day's real briefing (finding #15's live run)
         made exactly that kind of claim from Guardian/Al-Monitor
         reporting on Hormuz tanker interdiction, without a number to
         ground it. Requires `EIA_API_KEY` (free, no credit card,
         register at eia.gov/opendata) — if unset or the fetch fails, the
         section is omitted entirely and the run proceeds normally, same
         degrade-gracefully pattern as missing Gmail credentials. The
         exact EIA API v2 query shape (`facets[series][]`, `frequency`,
         `sort[]` parameters) was built from EIA's published
         documentation, not confirmed against a live response, since this
         sandbox can't reach external APIs — get an `EIA_API_KEY`, run
         the pipeline, and check whether the "Oil price snapshot" section
         actually appears with real numbers before trusting it.
     17. **Satellite thermal-anomaly detection added (2026-07-31), not
         yet confirmed live.** New `pipeline/satellite_hotspots.py`
         fetches NASA FIRMS thermal-anomaly detections (VIIRS satellite
         heat signatures) for the region and injects them into Stage 4's
         prompt, answering an explicit request for "where are strikes
         hitting" analysis. Deliberately does NOT try to distinguish
         strikes from wildfires/gas flares/agricultural burning at the
         data layer — this region includes major gas-flaring
         infrastructure (Iraq, Gulf states) that would dominate a raw
         feed. Instead, filters only FIRMS' own "low" confidence tier and
         relies on the prompt's explicit corroboration-required caveat
         plus the existing Tier 1/corroboration reasoning discipline —
         the same approach that's already handled GDELT's inherent
         noisiness well in practice (finding #15's live run correctly
         tagged an uncorroborated GDELT signal as "Speculation," not
         fact). This was a deliberate scope decision, not a shortcut:
         built a local hotspot-suppression alternative was considered and
         explicitly rejected in favor of trusting the same reasoning
         process already proven to work. Requires `FIRMS_MAP_KEY` (free,
         no credit card, register at
         firms.modaps.eosdis.nasa.gov/api/map_key) — if unset or the
         fetch fails, the section is omitted entirely. The FIRMS
         area-CSV query shape (`/api/area/csv/{key}/{source}/{bbox}/
         {days}`) was built from FIRMS' published documentation, not
         confirmed against a live response — get a `FIRMS_MAP_KEY`, run
         the pipeline, and check whether the "Satellite thermal
         anomalies" section appears with real detections (or cleanly
         omits itself, if there's nothing in the bounding box that day)
         before trusting it.
     18. **`qwen2.5:14b` needed more memory than a 16GB Mac had, causing
         severe swap thrashing and likely silent triage data loss.**
         Confirmed live 2026-08-02 via Activity Monitor: with the
         pipeline mid-run, `llama-server` (Ollama's inference engine) was
         using 17.34GB of memory — more than the machine's entire 16GB of
         physical RAM — and macOS had 11.36GB swapped to disk just to
         keep it running at all. A run that should take ~15-20 minutes
         was still on triage batch 39 after 3+ hours (averaging ~5.2
         minutes/batch). That's not just slow: `pipeline/ollama_client.py`
         times out any single Ollama call at 180s (3 minutes), and the
         observed pace was already past that threshold on average — real
         batches were very likely timing out and getting their ~20 items
         silently dropped via `pipeline/triage.py`'s per-batch
         `httpx.HTTPError` catch (working as designed for a genuinely
         dead Ollama, but here masking a starved-but-alive one). Checked
         for a second, worse failure mode first: whether the hourly
         LaunchAgent retry had stacked additional overlapping pipeline
         instances on top of the slow one, since `already_ran_today()`
         only checks for a *completed* row in `run_log.csv` (written in
         `run()`'s `finally` block), so a run running long enough could
         let a retry launch a second instance competing for the same
         starved RAM. Confirmed via `ps aux` that this did NOT happen —
         only one instance was running, and `caffeinate` was confirmed
         still correctly holding the Mac awake (finding #14's fix
         holding). Fixed by switching `triage_model` from `qwen2.5:14b`
         to `qwen2.5:7b` in `config/watchlists.yaml` — well under half
         the memory footprint, should fit in 16GB without swapping at
         all. Not a code bug and not something a code fix alone could
         solve (16GB is fixed on Apple Silicon); if you're running this
         on a machine with 32GB+ RAM, switching back to `qwen2.5:14b` is
         likely better for triage judgment quality and worth trying.
         **Confirmed live 2026-08-03, partial improvement, not a full
         fix.** Swap dropped from 11.36GB to 7.49GB — real, but not
         eliminated. `llama-server` running `qwen2.5:7b` grew to 13.43GB
         resident under load (well above its ~9.5GB size right after
         loading), and combined with everything else open at the time (a
         lot of Chrome tabs/helpers, other apps), total memory used was
         still pinned at 15.37 of 16GB. Pace was still slow — 5 triage
         batches in ~90 minutes (~18 min/batch), actually *worse* than
         14b's swap-degraded average of ~5.2 min/batch — and a `top -l 1`
         snapshot caught the CPU at 86.8% idle with `llama-server` not
         even in the top 10 by usage, consistent with it stalled mid
         page-fault rather than genuinely computing. Bottom line: on a
         16GB Mac, model size alone isn't the only lever — what else is
         open when the pipeline runs matters just as much. See finding
         #19 for one real fix implemented as a result (freeing Ollama's
         memory the moment it's no longer needed); closing other
         memory-heavy apps before a run is the other practical mitigation,
         with no code fix possible for it.
     19. **Ollama's model was never explicitly unloaded, holding memory
         hostage long after it was needed.** Direct fix for part of
         finding #18: nothing in this pipeline ever told Ollama it could
         free the model. It sat loaded by Ollama's own default (~5
         minutes idle, or longer if anything else pinged it) through
         Stage 3-5 — memory query, the Sonnet call, write-back/render/
         email — none of which touch Ollama at all, and then for the rest
         of the day until the next scheduled run. Added
         `pipeline/ollama_client.py::unload_model()`, which sends
         Ollama's documented immediate-unload request (`POST /api/chat`
         with `"messages": []` and `"keep_alive": 0`) right after Stage
         2c (prediction resolution) finishes — the last stage that needs
         Ollama at all. Best-effort by design: wrapped in a broad
         try/except that only logs a warning on failure, since losing an
         already-completed day's briefing over a memory-cleanup courtesy
         call failing would be a much worse outcome than just leaving the
         model loaded a bit longer. This does NOT speed up triage/
         prediction-resolution themselves (the model has to be loaded
         while those stages genuinely run) — it only stops the pipeline
         from needlessly holding that memory hostage once it's done
         needing it. 3 new tests confirm the exact request shape, and
         that failures (connection refused, HTTP error) never raise. Not
         yet confirmed live — check `data/launchagent.log` for the
         "Unloaded ... from Ollama" line, and Activity Monitor, on the
         next real run.

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
6. `scheduler.py`'s `BlockingScheduler` has never been observed firing on
   its own cron schedule — every live run so far has been a manual
   `python -m agents.middle_east.run`.
7. The LaunchAgent boot/login path (see "Running when the Mac is off
   overnight" above) **has** now fired for real on a cold boot/login —
   but the first attempt (2026-07-30, before the `caffeinate -i` fix)
   froze mid-run when the Mac's own idle sleep kicked in with nobody at
   the keyboard, and never reached the `finally` block that writes a
   `run_log.csv` row. Confirm on a real overnight test that the
   `caffeinate`-wrapped version now runs to completion unattended, and
   that `already_ran_today()` correctly skips a second login the same
   day.
