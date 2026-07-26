"""Shared local Ollama call helper — used by triage (Stage 2b) and the
prediction resolution check (Stage 2c). Neither calls the Anthropic API;
this module never touches the network except to `host` (default
localhost).
"""
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


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
