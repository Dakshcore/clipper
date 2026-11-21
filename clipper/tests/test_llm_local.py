"""Local (Ollama) and Venice backends of llm.py. No live server is needed."""
import io
import json
import urllib.error

import pytest

from clipper import llm

GOOD = {"hook": 7, "standalone": 6, "title": "T", "description": "D",
        "hashtags": ["#a", "#b", "#c"]}


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(obj):
    return FakeReply(json.dumps(obj).encode("utf-8"))


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(llm, "_backend", "claude")
    monkeypatch.setattr(llm, "_local_ok", None)
    monkeypatch.setattr(llm, "_disabled", False)
    monkeypatch.setattr(llm, "_no_think", set())
    monkeypatch.setattr(llm, "_models", dict(llm._models))
    llm.rate.cache_clear()
    yield
    llm.rate.cache_clear()


def fake_server(monkeypatch, chat, models=("llama3.2:latest",)):
    """chat(payload) returns the reply dict or raises."""
    calls = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/tags"):
            return reply({"models": [{"name": m} for m in models]})
        payload = json.loads(req.data)
        calls.append(payload)
        return reply(chat(payload))

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return calls


def test_parse_clamps_and_reads_fields():
    out = llm._parse('noise {"hook": 14, "standalone": -2, "title": "T", '
                     '"description": "D", "hashtags": ["#a"]} tail')
    assert out == {"hook": 10.0, "standalone": 0.0, "title": "T",
                   "description": "D", "hashtags": ["#a"]}


def test_parse_rejects_bad_reply():
    assert llm._parse("I cannot rate this") is None
    assert llm._parse('{"hook": "high", "standalone": 3}') is None
    assert llm._parse('{"title": "no scores"}') is None


def test_configure_rejects_unknown_backend():
    with pytest.raises(ValueError):
        llm.configure({"llm_backend": "gpt"})


def test_backend_none_is_unavailable():
    llm.configure({"llm_backend": "none"})
    assert not llm.available()
    assert llm.rate("some text", "x") is None


def test_local_rating_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = fake_server(monkeypatch, lambda p: {"message": {"content": json.dumps(GOOD)}})
    llm.configure({"llm_backend": "local", "local_model": "llama3.2"})
    assert llm.available()
    assert llm.active_model() == "llama3.2"
    out = llm.rate("some text", "llama3.2")
    assert out["hook"] == 7.0 and out["standalone"] == 6.0
    assert calls[0]["model"] == "llama3.2"
    assert calls[0]["stream"] is False
    assert calls[0]["format"] == llm.SCHEMA
    assert "<transcript>\nsome text\n</transcript>" in calls[0]["messages"][1]["content"]


def test_local_unreachable_falls_back(monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    llm.configure({"llm_backend": "local", "local_model": "llama3.2"})
    assert not llm.available()
    assert llm.rate("some text", "llama3.2") is None
    assert "not reachable" in llm.why_unavailable()


def test_local_model_not_installed(monkeypatch):
    fake_server(monkeypatch, lambda p: {}, models=("other:latest",))
    llm.configure({"llm_backend": "local", "local_model": "llama3.2"})
    assert not llm.available()


def test_retries_without_think_option(monkeypatch):
    def chat(payload):
        if "think" in payload:
            raise urllib.error.HTTPError("u", 400, "no thinking", {}, None)
        return {"message": {"content": json.dumps(GOOD)}}

    calls = fake_server(monkeypatch, chat)
    llm.configure({"llm_backend": "local", "local_model": "llama3.2"})
    assert llm.rate("first", "llama3.2")["hook"] == 7.0
    assert llm.rate("second", "llama3.2")["hook"] == 7.0
    assert ["think" in c for c in calls] == [True, False, False]


def test_bad_json_from_model_gives_none(monkeypatch):
    fake_server(monkeypatch, lambda p: {"message": {"content": "sorry"}})
    llm.configure({"llm_backend": "local", "local_model": "llama3.2"})
    assert llm.rate("some text", "llama3.2") is None
    assert llm.available()      # one bad reply does not disable the backend


def venice_server(monkeypatch, chat):
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        return reply(chat(json.loads(req.data)))

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return seen


def test_venice_needs_key(monkeypatch):
    monkeypatch.delenv("VENICE_API_KEY", raising=False)
    llm.configure({"llm_backend": "venice"})
    assert not llm.available()
    assert llm.rate("some text", "zai-org-glm-5") is None
    assert "VENICE_API_KEY" in llm.why_unavailable()


def test_venice_rating(monkeypatch):
    monkeypatch.setenv("VENICE_API_KEY", "test-key")
    seen = venice_server(monkeypatch, lambda p: {
        "choices": [{"message": {"content": json.dumps(GOOD)}}]})
    llm.configure({"llm_backend": "venice", "venice_model": "some-model"})
    assert llm.available()
    assert llm.active_model() == "some-model"
    out = llm.rate("some text", "some-model")
    assert out["hook"] == 7.0 and out["standalone"] == 6.0
    req = seen[0]
    assert req.full_url == "https://api.venice.ai/api/v1/chat/completions"
    assert req.get_header("Authorization") == "Bearer test-key"
    body = json.loads(req.data)
    assert body["model"] == "some-model"
    assert "<transcript>\nsome text\n</transcript>" in body["messages"][1]["content"]


def test_venice_rejected_key_disables_backend(monkeypatch):
    monkeypatch.setenv("VENICE_API_KEY", "bad")

    def chat(payload):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    venice_server(monkeypatch, chat)
    llm.configure({"llm_backend": "venice"})
    assert llm.rate("some text", "zai-org-glm-5") is None
    assert not llm.available()


def test_venice_server_error_keeps_backend(monkeypatch):
    monkeypatch.setenv("VENICE_API_KEY", "test-key")

    def chat(payload):
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)

    venice_server(monkeypatch, chat)
    llm.configure({"llm_backend": "venice"})
    assert llm.rate("some text", "zai-org-glm-5") is None
    assert llm.available()
