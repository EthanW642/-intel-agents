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
def call_ollama(host: str, model: str, system_prompt: str, user_prompt: str, timeout: float = 180.0) -> str:
    resp = httpx.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]
