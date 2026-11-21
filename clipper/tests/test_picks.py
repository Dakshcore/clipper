"""Whole-transcript moment picking. No live server is needed."""
import io
import json

import pytest

from clipper import llm
from clipper.moments import sentences, windows_from_picks

RATING = {"hook": 8, "standalone": 7, "title": "T", "description": "D",
          "hashtags": ["#a", "#b", "#c"]}


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(llm, "_backend", "claude")
    monkeypatch.setattr(llm, "_disabled", False)
    monkeypatch.setattr(llm, "_known", {})
    monkeypatch.setattr(llm, "_models", dict(llm._models))
    monkeypatch.setenv("VENICE_API_KEY", "test-key")
    llm.rate.cache_clear()
    yield
    llm.rate.cache_clear()


def venice(monkeypatch, content):
    """Fake Venice that answers every chat call with `content`."""
    calls = []

    def urlopen(req, timeout=None):
        calls.append(json.loads(req.data))
        return FakeReply(json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode("utf-8"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    llm.configure({"llm_backend": "venice", "venice_model": "m"})
    return calls


def make_sents(n, seconds=10.0):
    """n sentences, each `seconds` long, one word per second."""
    words = []
    for i in range(n):
        for k in range(int(seconds)):
            t = i * seconds + k
            last = k == int(seconds) - 1
            words.append({"word": f"w{i}_{k}" + ("." if last else ""),
                          "start": t, "end": t + 0.9})
    return sentences(words)


SENTS3 = [(0.0, 10.0, "one."), (10.0, 20.0, "two."), (20.0, 30.0, "three.")]


def test_pick_moments_sends_numbered_sentences_and_parses(monkeypatch):
    calls = venice(monkeypatch, json.dumps(
        {"moments": [{**RATING, "first": 0, "last": 1}]}))
    out = llm.pick_moments(SENTS3, 15, 60, 3, "m")
    assert out == [{"hook": 8.0, "standalone": 7.0, "title": "T", "description": "D",
                    "hashtags": ["#a", "#b", "#c"], "first": 0, "last": 1}]
    user = calls[0]["messages"][1]["content"]
    assert "[0] 0.0-10.0 one." in user and "[2] 20.0-30.0 three." in user
    assert "between 15 and 60 seconds" in calls[0]["messages"][0]["content"]


def test_pick_moments_drops_bad_moments_keeps_good(monkeypatch):
    venice(monkeypatch, json.dumps({"moments": [
        {**RATING, "first": 2, "last": 1},        # reversed
        {**RATING, "first": 0, "last": 9},        # out of range
        {"first": 0, "last": 1, "title": "no scores"},
        {**RATING, "first": 1, "last": 2}]}))
    out = llm.pick_moments(SENTS3, 15, 60, 3, "m")
    assert [(m["first"], m["last"]) for m in out] == [(1, 2)]


def test_pick_moments_bad_reply_gives_none(monkeypatch):
    venice(monkeypatch, "sorry, I cannot do that")
    assert llm.pick_moments(SENTS3, 15, 60, 3, "m") is None


def test_pick_moments_empty_list_is_not_a_failure(monkeypatch):
    venice(monkeypatch, '{"moments": []}')
    assert llm.pick_moments(SENTS3, 15, 60, 3, "m") == []


def test_pick_moments_only_on_venice(monkeypatch):
    llm.configure({"llm_backend": "none"})
    assert not llm.supports_picks()
    assert llm.pick_moments(SENTS3, 15, 60, 3, "m") is None


def test_pick_moments_too_long_falls_back(monkeypatch):
    calls = venice(monkeypatch, '{"moments": []}')
    monkeypatch.setattr(llm, "PICK_MAX_CHARS", 10)
    assert llm.pick_moments(SENTS3, 15, 60, 3, "m") is None
    assert calls == []


def test_remembered_rating_needs_no_second_call(monkeypatch):
    calls = venice(monkeypatch, "should not be asked")
    llm.remember("some text", "m", {"hook": 8.0, "standalone": 7.0, "title": "T",
                                    "description": "D", "hashtags": ["#a"]})
    assert llm.rate("some text", "m")["title"] == "T"
    assert calls == []


def test_windows_from_picks_keeps_a_valid_pick():
    sents = make_sents(6)
    [(w, pk, adjusted)] = windows_from_picks(sents, [{"first": 1, "last": 3}], 15, 60)
    assert (w.start, round(w.end, 1), adjusted) == (10.0, 39.9, False)
    assert w.text.startswith("w1_0") and w.text.endswith("w3_9.")
    assert pk == {"first": 1, "last": 3}


def test_windows_from_picks_shortens_a_long_pick():
    sents = make_sents(10)                      # pick covers 100 s, max is 60
    [(w, _, adjusted)] = windows_from_picks(sents, [{"first": 0, "last": 9}], 15, 60)
    assert adjusted and w.end - w.start <= 60 and w.end - w.start >= 50


def test_windows_from_picks_extends_a_short_pick():
    sents = make_sents(6)                       # one sentence is 10 s, min is 15
    [(w, _, adjusted)] = windows_from_picks(sents, [{"first": 2, "last": 2}], 15, 60)
    assert adjusted and (w.start, round(w.end, 1)) == (20.0, 39.9)


def test_windows_from_picks_drops_what_cannot_be_repaired():
    sents = make_sents(1)                       # 10 s in total, min is 15
    assert windows_from_picks(sents, [{"first": 0, "last": 0}], 15, 60) == []
    assert windows_from_picks(sents, [{"first": 3, "last": 4}], 15, 60) == []
