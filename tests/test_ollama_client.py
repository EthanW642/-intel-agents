from unittest.mock import MagicMock

import httpx

from pipeline import ollama_client


class _FakeResponse:
    def __init__(self, status_ok=True):
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            request = httpx.Request("POST", "http://localhost:11434/api/chat")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)


def test_unload_model_sends_empty_messages_and_zero_keep_alive(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(ollama_client.httpx, "post", fake_post)

    ollama_client.unload_model("http://localhost:11434", "qwen2.5:7b")

    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["model"] == "qwen2.5:7b"
    assert captured["json"]["messages"] == []
    assert captured["json"]["keep_alive"] == 0


def test_unload_model_never_raises_on_connection_failure(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ollama_client.httpx, "post", fake_post)

    # Best-effort cleanup -- must not raise even if Ollama is unreachable,
    # since that would turn a courtesy memory-cleanup call into a reason
    # to lose an already-completed day's run.
    ollama_client.unload_model("http://localhost:11434", "qwen2.5:7b")


def test_unload_model_never_raises_on_http_error_status(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(status_ok=False)

    monkeypatch.setattr(ollama_client.httpx, "post", fake_post)

    ollama_client.unload_model("http://localhost:11434", "qwen2.5:7b")
