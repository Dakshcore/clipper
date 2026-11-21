"""Find moments in one media file and render them. Shared by `video` and `watch`."""
import contextlib
import json
import os
import threading
import time
from pathlib import Path

from . import llm
from .edit import keep_intervals, make_clip
from .moments import candidate_windows, pick, sentences, windows_from_picks
from .output import build_sidecar, make_clip_id, write_sidecar
from .scoring import Signals, heuristic_text_score, load_scorer
from .signals import chat_score, loudness_per_second, loudness_score
from .transcribe import device, transcribe
from .util import OUT, done, log, probe

LOCK = threading.Lock()   # one transcription/render at a time
_warned = False


def _held(lock):
    """Hold `lock` for the heavy local work only. The LLM calls wait on the
    network (measured 9 to 17 s each) and must not block other streams."""
    return lock if lock is not None else contextlib.nullcontext()


def process_media(path, source_url: str, platform: str, cfg: dict, work_dir: Path, *,
                  offset: float = 0.0, chat_times=None, max_clips: int = 1,
                  taken: list = None, verbose: bool = False, lock=None) -> list:
    """Return the sidecars of the clips made. `offset` is where this file
    starts on the source timeline; `taken` holds source-timeline ranges that
    already became clips and is updated in place."""
    global _warned
    taken = taken if taken is not None else []
    with _held(lock):
        words = transcribe(path, cfg)
        db = loudness_per_second(path)
    cands = candidate_windows(words, cfg["min_clip_seconds"], cfg["max_clip_seconds"])
    # drop windows that would fall under the minimum once dead air is trimmed
    gap, min_s = float(cfg["dead_air_seconds"]), float(cfg["min_clip_seconds"])
    cands = [w for w in cands
             if sum(b - a for a, b in keep_intervals(w.words, w.start, w.end, gap)) >= min_s]
    if verbose:
        done("transcribe", f"{len(words)} words on {device()}, {len(cands)} candidate windows")
    if not cands:
        return []

    llm.configure(cfg)
    use_llm = llm.available()
    model = llm.active_model()
    if not _warned:
        log(f"text scored by {llm.backend()} model {model}" if use_llm else
            f"{llm.why_unavailable()}: scoring text with the built-in heuristic, not an LLM")
        _warned = True
    scorer = load_scorer(cfg["scorer"])

    # Whole-transcript pass: the model reads every sentence once and chooses
    # the moments itself. Used when the backend supports it; otherwise, or
    # when it returns nothing usable, the pre-cut windows are rated one by one.
    chosen_by_llm = []
    if use_llm and cfg.get("llm_pick_moments", True) and llm.supports_picks():
        sents = sentences(words)
        picks = llm.pick_moments(
            [(x[0]["start"], x[-1]["end"], " ".join(w["word"] for w in x)) for x in sents],
            min_s, float(cfg["max_clip_seconds"]), max_clips + 2, model)
        for w, pk, adjusted in windows_from_picks(sents, picks or [], min_s,
                                                  float(cfg["max_clip_seconds"])):
            if sum(b - a for a, b in keep_intervals(w.words, w.start, w.end, gap)) < min_s:
                continue
            if not adjusted:      # an adjusted window is re-rated on its own text
                llm.remember(w.text, model, {k: v for k, v in pk.items()
                                             if k not in ("first", "last")})
            chosen_by_llm.append(w)
    if chosen_by_llm:
        pool, llm_set = chosen_by_llm, {id(w) for w in chosen_by_llm}
        loud = {id(w): loudness_score(db, w.start, w.end) for w in pool}
    else:
        pool = cands
        loud = {id(w): loudness_score(db, w.start, w.end) for w in pool}
        cheap = sorted(pool, key=lambda w: -(0.5 * loud[id(w)]
                                             + 0.5 * heuristic_text_score(w.text)))
        llm_set = ({id(w) for w in cheap[: int(cfg["llm_max_candidates"])]}
                   if use_llm else set())

    scored = []
    for w in pool:
        sig = Signals(
            loudness=loud[id(w)],
            chat_rate=chat_score(chat_times, offset + w.start, offset + w.end) if chat_times else None,
            llm_model=model if id(w) in llm_set else None,
            weights=cfg["weights"])
        scored.append((float(scorer(w, sig)), w))
    local_taken = [(a - offset, b - offset) for a, b in taken]
    chosen = pick(scored, max_clips, float(cfg["min_score"]), local_taken)
    if verbose:
        best = max(s for s, _ in scored)
        how = (f"{len(pool)} moments chosen by the model from the whole transcript"
               if chosen_by_llm else f"{len(llm_set)} windows rated one by one")
        done("find moments", f"{len(chosen)} picked, best score {best:.3f}, "
             f"text scored by {llm.backend() + ' model ' + model if use_llm else 'heuristic (no LLM)'}"
             f"{', ' + how if use_llm else ''}")

    out = []
    for score, w in chosen:
        start, end = offset + w.start, offset + w.end
        clip_id = make_clip_id(platform, source_url, start, end)
        mp4 = OUT / f"{clip_id}.mp4"
        if (OUT / f"{clip_id}.json").exists():
            taken.append((start, end))
            continue
        t = time.time()
        with _held(lock):
            res = make_clip(path, w, cfg, mp4, work_dir)
        meta = (llm.rate(w.text, model) if use_llm else None) or {}
        side = build_sidecar(
            clip_id=clip_id, source_url=source_url, platform=platform, start=start, end=end,
            duration=probe(mp4)["duration"], score=score, transcript=w.text,
            title=meta.get("title", ""), description=meta.get("description", ""),
            hashtags=meta.get("hashtags"))
        write_sidecar(OUT, side)
        taken.append((start, end))
        out.append(side)
        log(f"clip {clip_id}: {side['duration']}s, layout {res['layout']}, "
            f"{res['cuts']} dead-air cuts, score {score:.3f}, rendered in {time.time() - t:.0f}s")
    return out


# ---------- disk cap ----------

ACTIVE = set()   # session folders of recordings that are running now; cleanup leaves them whole
_META = {"processed.json", "segments.csv", "window.txt"}


def _read_processed(f: Path) -> list:
    """File names listed in a processed.json. A missing, half-written or
    corrupt file counts as empty."""
    try:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [n for n in data if isinstance(n, str)] if isinstance(data, list) else []


def mark_processed(directory: Path, names: list) -> None:
    f = Path(directory) / "processed.json"
    have = set(_read_processed(f))
    tmp = f.with_name(f"processed.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(sorted(have | set(names))), encoding="utf-8")
    os.replace(tmp, f)     # readers see the old file or the new one, never half


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def cleanup(work_root: Path, hours: float, max_gb: float = None) -> int:
    """Delete processed raw files older than `hours`, then, when `work_root`
    is bigger than `max_gb`, the oldest processed ones until it fits. Only
    files a processed.json lists are deleted, so an unprocessed or open
    segment is never touched. Finished sessions left with nothing but
    bookkeeping files are removed. Never raises on bad or missing files."""
    work_root = Path(work_root).resolve()
    cutoff, n = time.time() - hours * 3600, 0
    cands = []          # (mtime, path) of processed files still on disk
    markers = list(work_root.rglob("processed.json"))
    for marker in markers:
        for name in _read_processed(marker):
            p = (marker.parent / name).resolve()
            if work_root not in p.parents:
                continue
            try:
                if not p.is_file():
                    continue
                mtime = p.stat().st_mtime
                if mtime < cutoff:
                    p.unlink(missing_ok=True)
                    n += 1
                else:
                    cands.append((mtime, p))
            except OSError:
                pass
    if max_gb:
        cap = float(max_gb) * 1e9
        total = sum(_size(f) for f in work_root.rglob("*") if f.is_file())
        for _, p in sorted(cands):
            if total <= cap:
                break
            size = _size(p)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            n += 1
    for marker in markers:
        d = marker.parent
        if d.resolve() in {Path(a).resolve() for a in ACTIVE}:
            continue
        try:
            rest = [e for e in d.iterdir() if e.name not in _META
                    and not e.name.startswith(("processed.", "chat."))]
            if rest:
                continue
            for e in d.iterdir():
                e.unlink(missing_ok=True)
            d.rmdir()
            if d.parent != work_root and not any(d.parent.iterdir()):
                d.parent.rmdir()
        except OSError:
            pass
    return n
