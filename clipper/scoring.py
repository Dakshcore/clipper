"""Stage 2d: the swappable moment scorer.

The contract is one function:

    score_window(window, signals) -> float in 0..1

`window` has .text, .start, .end (see moments.Window). `signals` is the
Signals dataclass below. To use another model, write a function with the same
signature and set `scorer: your_module:your_function` in config.yaml.
"""
import importlib
import re
from dataclasses import dataclass, field

from . import llm

HOOK_WORDS = {
    "wait", "what", "why", "how", "never", "nobody", "secret", "crazy", "insane",
    "wow", "no", "stop", "listen", "imagine", "watch", "look", "omg", "actually",
    "biggest", "worst", "best", "first", "truth", "mistake",
}
WEAK_STARTS = {"and", "but", "so", "or", "because", "then", "also", "it", "he",
               "she", "they", "that", "this", "which", "well", "um", "uh"}


@dataclass
class Signals:
    loudness: float = 0.0            # 0..1 loudness spike in the window
    chat_rate: float = None          # 0..1 chat spike, None if chat not readable
    llm_model: str = None            # model id for llm.rate, None to skip the LLM
    weights: dict = field(default_factory=lambda: {"text": 0.55, "loudness": 0.25, "chat": 0.20})


def heuristic_text_score(text: str) -> float:
    """Rough stand-in for the LLM: hook strength and standalone-ness, 0..1."""
    words = re.findall(r"[A-Za-z']+", text.lower())
    if not words:
        return 0.0
    head = words[:12]
    first_sentence = re.split(r"(?<=[.?!])\s", text.strip(), maxsplit=1)[0]
    hook = 0.3
    hook += 0.25 if "?" in first_sentence else 0.0
    hook += 0.15 if "!" in first_sentence else 0.0
    hook += min(0.3, 0.15 * sum(1 for w in head if w in HOOK_WORDS))
    hook += 0.1 if re.search(r"\d", first_sentence) else 0.0
    standalone = 0.7
    standalone -= 0.3 if words[0] in WEAK_STARTS else 0.0
    standalone += 0.2 if text.strip()[-1:] in ".?!" else 0.0
    standalone -= 0.2 if len(words) < 25 else 0.0
    hook, standalone = (max(0.0, min(1.0, v)) for v in (hook, standalone))
    return 0.6 * hook + 0.4 * standalone


def score_window(window, signals: Signals) -> float:
    """Default scorer: LLM rating of the transcript (local model or Claude,
    see llm.py; text heuristic when neither is available), blended with
    loudness and chat rate."""
    rating = llm.rate(window.text, signals.llm_model) if signals.llm_model else None
    if rating:
        text = (0.6 * rating["hook"] + 0.4 * rating["standalone"]) / 10.0
    else:
        text = heuristic_text_score(window.text)
    w = signals.weights
    parts = [(w.get("text", 0.0), text), (w.get("loudness", 0.0), signals.loudness)]
    if signals.chat_rate is not None:
        parts.append((w.get("chat", 0.0), signals.chat_rate))
    total = sum(p[0] for p in parts)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(wt * v for wt, v in parts) / total))


def load_scorer(path: str):
    """Resolve 'module:function' from config into a callable."""
    mod, _, fn = path.partition(":")
    return getattr(importlib.import_module(mod), fn)
