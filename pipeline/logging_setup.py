"""Shared logging setup for every entry point (agents/middle_east/run.py,
scheduler.py, scripts/*.py).

Two problems with plain `logging.basicConfig(level=logging.INFO)` alone:

1. **Noise.** httpx and sentence-transformers/huggingface_hub log an INFO
   line per HTTP request/HEAD check. A single run makes ~100 GDELT export
   requests plus the sentence-transformers model-loading chatter, burying
   the pipeline's own progress lines (triage batch N/M, stage markers)
   under hundreds of unrelated ones.
2. **Secret leakage.** httpx's INFO log line is the full request URL,
   including query-string parameters. Confirmed live 2026-08-03: the EIA
   oil-price fetch (pipeline/oil_prices.py) passes `api_key` as a query
   parameter, so a plain `basicConfig(level=INFO)` printed the live
   EIA_API_KEY straight into the run's stdout/log file. Silencing httpx to
   WARNING (errors/timeouts still surface) closes this off structurally —
   no per-call redaction to remember or forget.
"""
from __future__ import annotations

import logging

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "sentence_transformers",
    "huggingface_hub",
    "urllib3",
)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
