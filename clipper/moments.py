"""Stage 2c: build candidate windows on sentence boundaries and pick the best."""
from dataclasses import dataclass, field


@dataclass
class Window:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list, repr=False)

    @property
    def duration(self) -> float:
        return self.end - self.start


def sentences(words: list, gap: float = 0.7, max_len: float = 15.0) -> list:
    """Group words into sentences: split on . ? !, on long pauses, or when a
    run-on passes max_len seconds."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = w["word"][-1:] in ".?!"
        pause = nxt is not None and nxt["start"] - w["end"] > gap
        long = w["end"] - cur[0]["start"] > max_len
        if nxt is None or ends or pause or long:
            out.append(cur)
            cur = []
    return out


def candidate_windows(words: list, min_s: float, max_s: float) -> list:
    """Windows that start and end on sentence boundaries, min_s..max_s long."""
    sents = sentences(words)
    targets = (min_s + 5, (min_s + max_s) / 2, max_s - 5)
    out = []
    for i in range(len(sents)):
        start = sents[i][0]["start"]
        options = []
        for j in range(i, len(sents)):
            dur = sents[j][-1]["end"] - start
            if dur > max_s:
                break
            if dur >= min_s:
                options.append((j, dur))
        chosen = {min(options, key=lambda o: abs(o[1] - t))[0] for t in targets} if options else set()
        for j in sorted(chosen):
            ws = [w for s in sents[i: j + 1] for w in s]
            out.append(Window(start, ws[-1]["end"], " ".join(w["word"] for w in ws), ws))
    return out


def windows_from_picks(sents: list, picks: list, min_s: float, max_s: float) -> list:
    """Turn model picks into windows on sentence boundaries.

    `picks` is [{'first': i, 'last': j, ...}] over `sents` (see sentences()).
    A pick outside min_s..max_s is repaired by moving its end: shortened when
    too long, extended when too short. Returns [(Window, pick, adjusted)];
    picks that cannot be repaired are dropped."""
    out = []
    for pk in picks:
        i, j = pk["first"], pk["last"]
        if not 0 <= i <= j < len(sents):
            continue
        start, j0 = sents[i][0]["start"], j
        while j > i and sents[j][-1]["end"] - start > max_s:
            j -= 1
        while j + 1 < len(sents) and sents[j][-1]["end"] - start < min_s \
                and sents[j + 1][-1]["end"] - start <= max_s:
            j += 1
        if not min_s <= sents[j][-1]["end"] - start <= max_s:
            continue
        ws = [w for sent in sents[i: j + 1] for w in sent]
        out.append((Window(start, ws[-1]["end"], " ".join(w["word"] for w in ws), ws),
                    pk, j != j0))
    return out


def overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def pick(scored: list, max_n: int, min_score: float, taken: list = None) -> list:
    """Greedy best-first, no overlaps. `scored` is [(score, Window)];
    `taken` is [(start, end)] already used."""
    taken = list(taken or [])
    chosen = []
    for score, w in sorted(scored, key=lambda x: -x[0]):
        if len(chosen) >= max_n or score < min_score:
            break
        if any(overlap(w.start, w.end, a, b) > 0 for a, b in taken):
            continue
        chosen.append((score, w))
        taken.append((w.start, w.end))
    return chosen
