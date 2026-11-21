import pytest

from clipper import llm, scoring
from clipper.moments import Window
from clipper.scoring import Signals, load_scorer, score_window

TEXT = ("Wait, why does nobody talk about this? The first time I tried it the whole "
        "thing fell apart, and then one small change fixed everything for good.")


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "rate", lambda text, model: None)


def win(text=TEXT):
    return Window(10.0, 40.0, text)


def test_score_is_between_0_and_1():
    for loud in (0.0, 0.5, 1.0):
        for chat in (None, 0.0, 1.0):
            s = score_window(win(), Signals(loudness=loud, chat_rate=chat))
            assert 0.0 <= s <= 1.0


def test_louder_and_busier_chat_score_higher():
    quiet = score_window(win(), Signals(loudness=0.1, chat_rate=0.1))
    loud = score_window(win(), Signals(loudness=0.9, chat_rate=0.1))
    busy = score_window(win(), Signals(loudness=0.9, chat_rate=0.9))
    assert quiet < loud < busy


def test_missing_chat_is_left_out_not_counted_as_zero():
    weights = {"text": 0.5, "loudness": 0.5, "chat": 1.0}
    s = score_window(win(), Signals(loudness=1.0, chat_rate=None, weights=weights))
    text = scoring.heuristic_text_score(TEXT)
    assert s == pytest.approx((0.5 * text + 0.5 * 1.0) / 1.0)


def test_llm_rating_is_used_when_present(monkeypatch):
    monkeypatch.setattr(llm, "rate", lambda text, model: {"hook": 10, "standalone": 10})
    s = score_window(win("meh"), Signals(loudness=0.0, llm_model="some-model",
                                         weights={"text": 1.0, "loudness": 0.0, "chat": 0.0}))
    assert s == pytest.approx(1.0)


def test_scorer_is_swappable_by_path():
    fn = load_scorer("clipper.scoring:score_window")
    assert fn is score_window
    other = load_scorer("clipper.scoring:heuristic_text_score")
    assert callable(other)
