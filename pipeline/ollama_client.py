"""Shared local Ollama call helper — used by triage (Stage 2b) and the
prediction resolution check (Stage 2c). Neither calls the Anthropic API;
this module never touches the network except to `host` (default
localhost).
"""
from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class OllamaUnavailableError(Exception):
    """Raised when every batch in a triage or prediction-resolution run
    failed to reach Ollama at all (connection-level failure, not a parse
    error) — signals a local infrastructure outage rather than a
    genuinely quiet day, so the caller should abort before spending on the
    Sonnet call rather than silently reporting zero survivors."""


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def call_ollama(
    host: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 180.0,
    response_format: str | dict = "json",
    num_ctx: int | None = None,
) -> str:
    """`response_format` defaults to the bare `"json"` mode (valid JSON, any
    shape) but callers doing batch scoring should pass a JSON Schema dict
    instead — confirmed live 2026-07-25 that bare "json" mode lets the model
    return a single object instead of a per-item array (it silently scored
    only item 0 of a 2-item batch), which is indistinguishable from a parse
    failure downstream. A schema with minItems/maxItems pinned to the batch
    size (see pipeline/triage.py, pipeline/predictions.py) is Ollama's actual
    mechanism for enforcing "one entry per input item," not a prompt
    instruction the model can choose to ignore.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": response_format,
        "stream": False,
    }
    if num_ctx is not None:
        # Ollama's default context window (2048-4096 tokens depending on
        # version) is silently applied if not overridden here — a large
        # prompt just gets truncated server-side with no error, no warning.
        # Confirmed live 2026-07-25: dumping ~393 items into one prediction-
        # resolution prompt with no num_ctx override caused the model to
        # "see" only a tiny leftover slice of the real input, producing
        # confident-sounding verdicts about news that was never actually in
        # its context.
        payload["options"] = {"num_ctx": num_ctx}

    resp = httpx.post(
        f"{host}/api/chat",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def unload_model(host: str, model: str, timeout: float = 30.0) -> None:
    """Tells Ollama to free `model` from memory immediately, instead of
    leaving it loaded for its default ~5-minute idle keep-alive (or longer,
    if anything else pings it in the meantime). Confirmed live 2026-08-02:
    on a 16GB Mac, qwen2.5:14b's llama-server process alone used more
    memory than the machine's total physical RAM, and even qwen2.5:7b grew
    to ~13GB resident under load — holding that through Stage 3-5 (memory
    query, the Sonnet call, write-back/render/email — none of which touch
    Ollama at all) serves no purpose and just keeps the rest of the run,
    and the rest of the day until the next scheduled run, fighting for
    memory it doesn't need to be using.

    Call this once, right after the last Ollama-dependent stage
    (prediction resolution, Stage 2c) finishes. Best-effort: failure here
    is never worth failing the run over, so this logs a warning and moves
    on rather than raising — an already-paid-for day's briefing shouldn't
    be lost because a memory-cleanup courtesy call failed.
    """
    try:
        resp = httpx.post(
            f"{host}/api/chat",
            json={"model": model, "messages": [], "keep_alive": 0},
            timeout=timeout,
        )
        resp.raise_for_status()
        logger.info("Unloaded %s from Ollama — freeing memory for the rest of the run.", model)
    except Exception:
        logger.warning("Failed to explicitly unload %s from Ollama — non-fatal, continuing.", model, exc_info=True)
