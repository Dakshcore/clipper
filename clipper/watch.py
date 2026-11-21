"""`watch`: poll channels, record live streams in rolling segments, clip as it goes."""
import threading
import time

import numpy as np

from . import discover
from .chat import start_chat
from .config import norm_url
from .ingest import Recorder, check_live, concat_segments
from .pipeline import ACTIVE, LOCK, cleanup, mark_processed, process_media
from .platforms import channel_name, is_channel_url, live_url, platform_of, slug
from .signals import loudness_per_second
from .util import WORK, log, lower_own_priority


def pick_hottest(cands: list, chat_times: list, loud=loudness_per_second) -> int:
    """Of several waiting segments, the index of the one most worth processing.
    cands = [(index, path, start, end)]. Most chat messages wins; with no chat
    (or a tie) the biggest loudness spike wins, then the newest segment."""
    def heat(c):
        i, path, start, end = c
        chat = sum(1 for t in chat_times if start <= t < end) if chat_times else 0
        db = loud(path)
        spike = float(np.percentile(db, 95) - np.median(db)) if len(db) else 0.0
        return (chat, round(spike, 2), i)
    return max(cands, key=heat)[0]


def is_starved(closed: int, elapsed: float, segment_seconds: float) -> bool:
    """True when a recording has closed fewer than half the segments that
    real-time recording would have closed. Not judged before 3 segment lengths."""
    if elapsed < 3 * segment_seconds:
        return False
    return closed < 0.5 * (elapsed / segment_seconds - 1)


def _key(url: str) -> str:
    return norm_url(url).lower()


def plan_changes(watching: set, top: list, fixed: set, unread: set = ()):
    """What a rescan changes. watching = discovered URLs being watched now,
    top = URLs that should be watched, fixed = URLs from config sources (never
    touched), unread = platforms that could not be read this time: their
    running streams are kept, since absence from `top` means nothing for them.
    Returns (to_start, to_stop), both in a stable order."""
    want = [u for u in dict.fromkeys(_key(t) for t in top) if u not in fixed]

    def blind(u):
        try:
            return platform_of(u) in unread
        except ValueError:
            return False
    return ([u for u in want if u not in watching],
            sorted(u for u in watching if u not in want and not blind(u)))


_recorders = set()          # recorders of sessions that are running now
_recorders_lock = threading.Lock()


def _tidy(cfg: dict) -> None:
    """Disk cleanup. A failure here is logged and never stops a recording."""
    try:
        cleanup(WORK, float(cfg["raw_retention_hours"]), cfg.get("raw_max_gb"))
    except Exception as e:
        log(f"cleanup failed: {type(e).__name__}: {str(e)[:200]}")


def _mark(sdir, names: list) -> None:
    try:
        mark_processed(sdir, names)
    except Exception as e:
        log(f"could not update processed.json in {sdir}: {type(e).__name__}: {str(e)[:200]}")


def _session(url: str, cfg: dict, stop: threading.Event) -> None:
    plat, name = platform_of(url), channel_name(url)
    tag = f"{plat}/{name}"
    sdir = WORK / slug(url) / time.strftime("%Y%m%d-%H%M%S")
    seg_len = int(cfg["segment_seconds"])
    rec = Recorder(live_url(url), sdir, seg_len, cfg.get("live_quality", "720p"))
    with _recorders_lock:
        _recorders.add(rec)
    ACTIVE.add(sdir)
    chat = None
    processed, taken, chat_said, slow_said = 0, [], False, False

    def work() -> None:
        """Process the segments that have closed since the last pass."""
        nonlocal processed, chat_said
        segs = rec.completed()
        todo = list(range(processed, len(segs)))
        if len(todo) > 1 and cfg.get("live_skip_when_behind", True):
            # Behind live: process only the busiest waiting segment.
            best = pick_hottest([(i, *segs[i]) for i in todo],
                                list(chat.times) if chat.readable else [])
            log(f"{tag}: {len(todo)} segments waiting; processing the busiest "
                f"({segs[best][0].name}) and skipping {len(todo) - 1}")
            todo = [best]
        for i in todo:
            pair = segs[max(0, i - 1): i + 1]
            seg_path, seg_start, seg_end = segs[i]
            log(f"{tag}: processing segment {seg_path.name} "
                f"({seg_end - seg_start:.0f}s, {seg_path.stat().st_size / 1e6:.1f} MB)")
            if chat.readable and not chat_said:
                log(f"{tag}: chat is readable")
                chat_said = True
            win = concat_segments([p for p, _, _ in pair], sdir / "window.ts")
            try:
                # LOCK covers transcription and rendering only, not the LLM call
                made = process_media(
                    win, url, plat, cfg, sdir, offset=pair[0][1],
                    chat_times=list(chat.times) if chat.readable else None,
                    max_clips=1, taken=taken, lock=LOCK)
            except Exception as e:
                made = []
                log(f"{tag}: processing failed for {seg_path.name}: {str(e)[:300]}")
            lag = time.time() - (rec.origin() + seg_end)
            log(f"{tag}: processed up to {seg_end:.0f}s of stream, {len(made)} clip(s), "
                f"{len(chat.times)} chat messages so far, {lag:.0f}s behind live")
            processed = len(segs) if len(todo) == 1 else i + 1
            # segment i is still needed for the next window; earlier ones are done
            _mark(sdir, [p.name for p, _, _ in segs[:processed - 1]])
            _tidy(cfg)

    try:
        rec.start()
        chat = start_chat(plat, url, rec.origin(), sdir)
        log(f"{tag}: recording with {rec.tool} into {sdir}")
        while True:
            work()
            chat.rebase(rec.origin())
            if stop.is_set() or not rec.alive():
                break
            closed = len(rec.completed())   # fresh: work() may have taken a while
            if not slow_said and is_starved(closed, time.time() - rec.t0, seg_len):
                slow_said = True
                log(f"{tag}: recording is slower than live ({closed} segment(s) closed "
                    f"in {time.time() - rec.t0:.0f}s). Processing on this machine is likely "
                    "starving the recorder; lower live_quality or discover_top")
            stop.wait(3)
        if not stop.is_set():
            # The stream ended: the segment that closed between the last pass
            # and the recorder exiting has not been processed yet.
            work()
    finally:
        rec.stop()
        if chat is not None:
            chat.stop()
        # Everything left is done with: closed segments, and the open one the
        # recorder left behind when it stopped.
        left = sorted(p.name for p in sdir.glob("seg_*.ts")) if sdir.exists() else []
        _mark(sdir, left + ["window.ts"])
        with _recorders_lock:
            _recorders.discard(rec)
        ACTIVE.discard(sdir)
        _tidy(cfg)
        log(f"{tag}: recording stopped after {processed} segment(s)")


def _channel_loop(url: str, cfg: dict, stop: threading.Event) -> None:
    tag = f"{platform_of(url)}/{channel_name(url)}"
    while not stop.is_set():
        try:
            if check_live(live_url(url)):
                log(f"{tag}: live")
                _session(url, cfg, stop)
            else:
                log(f"{tag}: not live, checking again in {cfg['poll_seconds']}s")
        except Exception as e:
            log(f"{tag}: error: {str(e)[:300]}")
        stop.wait(float(cfg["poll_seconds"]))


def watch(cfg: dict, minutes: float = None) -> None:
    fixed = [u for u in cfg["sources"] if is_channel_url(u)]
    find = discover.enabled(cfg)
    if not fixed and not find:
        log("no channel URLs in config.yaml sources and no discover_category; "
            "nothing to watch")
        return
    _tidy(cfg)
    if lower_own_priority():
        log("processing runs below normal priority; recorders run above it")
    running = {}      # url key -> (thread, stop event)

    def start(url: str) -> None:
        ev = threading.Event()
        t = threading.Thread(target=_channel_loop, args=(url, cfg, ev), daemon=True)
        running[_key(url)] = (t, ev)
        t.start()

    for u in fixed:
        start(u)
    fixed_keys = set(running)
    log(f"watching {len(fixed)} listed channel(s)"
        + (f", plus the top {cfg['discover_top']} live streams rescanned every "
           f"{cfg['discover_rescan_minutes']} min" if find else "") + "; Ctrl+C to stop")
    deadline = time.time() + minutes * 60 if minutes else None
    next_scan = 0.0
    try:
        while True:
            if find and time.time() >= next_scan:
                next_scan = time.time() + float(cfg["discover_rescan_minutes"]) * 60
                unread = set()
                top = discover.top_streams(cfg, unread=unread)
                if top is None:
                    log("discover: no platform could be read; keeping the current channels")
                else:
                    for url, viewers in top:
                        log(f"discover: {url} {viewers} viewers")
                    if unread:
                        log(f"discover: could not read {', '.join(sorted(unread))}; "
                            "keeping its running streams")
                    add, drop = plan_changes(set(running) - fixed_keys,
                                             [u for u, _ in top], fixed_keys, unread)
                    for u in drop:
                        running.pop(u)[1].set()
                        log(f"discover: {u} left the top list; stopping")
                    for u in add:
                        start(u)
                    log(f"discover: {len(add)} started, {len(drop)} stopped, "
                        f"{len(running)} channel(s) watched")
            if deadline and time.time() > deadline:
                break
            if not find and not any(t.is_alive() for t, _ in running.values()):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    _shutdown(running)
    from . import llm
    if llm.USAGE["calls"]:
        log(f"LLM usage this run: {llm.USAGE['calls']} calls, "
            f"{llm.USAGE['input_tokens']} input tokens, {llm.USAGE['output_tokens']} output tokens")


def _shutdown(running: dict, timeout: float = 180) -> None:
    """Ask every session to stop, wait, then stop the recorder of any session
    whose thread is still busy so no streamlink or ffmpeg is left running."""
    for _, ev in running.values():
        ev.set()
    for t, _ in running.values():
        t.join(timeout=timeout)
    if any(t.is_alive() for t, _ in running.values()):
        with _recorders_lock:
            left = list(_recorders)
        for rec in left:
            rec.stop()
