"""Regression tests for the 2026-11-21 review fixes. No network, no real processes."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from clipper import chat, discover, ingest, llm, pipeline, watch
from clipper.chat import ChatCollector, YouTubeChat


# ---------- 1. a rescan keeps streams of a platform it could not read ----------

def test_plan_changes_keeps_streams_of_unread_platform():
    watching = {"https://kick.com/a", "https://www.twitch.tv/b"}
    top = ["https://www.twitch.tv/c"]
    add, drop = watch.plan_changes(watching, top, set(), {"kick"})
    assert add == ["https://www.twitch.tv/c"]
    assert drop == ["https://www.twitch.tv/b"]          # kick/a is kept
    add, drop = watch.plan_changes(watching, top, set())
    assert drop == ["https://kick.com/a", "https://www.twitch.tv/b"]


def test_top_streams_reports_the_platforms_it_could_not_read(monkeypatch):
    monkeypatch.setattr(discover, "log", lambda *_: None)
    monkeypatch.setattr(discover, "_creds", lambda p: ("id", "secret"))

    def broken(cat, n, now):
        raise discover.DiscoverError("HTTP 500")

    monkeypatch.setitem(discover.FINDERS, "kick", broken)
    monkeypatch.setitem(discover.FINDERS, "twitch", lambda c, n, now: [("https://www.twitch.tv/x", 5)])
    unread = set()
    top = discover.top_streams({"discover_category": "Just Chatting", "discover_top": 5}, 0, unread)
    assert top == [("https://www.twitch.tv/x", 5)]
    assert unread == {"kick"}


# ---------- 2. processed.json and cleanup cannot stop a session ----------

def test_mark_processed_is_atomic_and_survives_a_corrupt_file(tmp_path, monkeypatch):
    (tmp_path / "processed.json").write_text('["seg_0', encoding="utf-8")   # half written
    pipeline.mark_processed(tmp_path, ["a.ts"])
    assert json.loads((tmp_path / "processed.json").read_text(encoding="utf-8")) == ["a.ts"]
    assert [p.name for p in tmp_path.iterdir()] == ["processed.json"]       # no tmp left
    calls = []
    real = os.replace
    monkeypatch.setattr(pipeline.os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    pipeline.mark_processed(tmp_path, ["b.ts"])
    assert calls and Path(calls[0][1]).name == "processed.json"


def test_cleanup_tolerates_bad_json_and_vanishing_files(tmp_path):
    d = tmp_path / "src" / "s1"
    d.mkdir(parents=True)
    (d / "processed.json").write_text("{not json", encoding="utf-8")
    e = tmp_path / "src" / "s2"
    e.mkdir()
    (e / "processed.json").write_text(json.dumps(["gone.ts", 5]), encoding="utf-8")
    assert pipeline.cleanup(tmp_path, 0) == 0          # no exception


def test_cleanup_error_never_stops_a_session(monkeypatch):
    def boom(*a, **k):
        raise PermissionError("file in use")
    monkeypatch.setattr(watch, "cleanup", boom)
    monkeypatch.setattr(watch, "log", lambda *_: None)
    watch._tidy({"raw_retention_hours": 2})              # must not raise


# ---------- 3. chat reconnects, readable follows the connection ----------

class Flaky(ChatCollector):
    BACKOFF_FIRST = 0.01
    BACKOFF_MAX = 0.02

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.runs, self.states = 0, []

    def run(self):
        self.runs += 1
        self.states.append(self.readable)       # readable on entry, before this connection is up
        if self.runs == 1:
            self.readable = True
            raise ConnectionResetError("dropped")
        if self.runs == 2:
            self.readable = True
            return                              # server closed the socket
        self.readable = True
        self._stop.wait(5)


def test_chat_reconnects_and_readable_tracks_the_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "log", lambda *_: None)
    c = Flaky("https://kick.com/x", time.time(), tmp_path).start()
    deadline = time.time() + 5
    while c.runs < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert c.runs == 3
    assert c.states == [False, False, False]    # false again after every drop
    c.stop()
    c._thread.join(5)
    assert not c._thread.is_alive()


def test_chat_shutdown_error_is_not_logged(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(chat, "log", lines.append)

    class Resets(ChatCollector):
        def run(self):
            self._stop.wait(5)
            raise ConnectionResetError("reset by peer")

    c = Resets("https://kick.com/x", time.time(), tmp_path).start()
    c.stop()
    c._thread.join(5)
    assert not c._thread.is_alive()
    assert lines == []


def test_youtube_chat_file_without_messages_is_not_readable(tmp_path, monkeypatch):
    (tmp_path / "chat.live_chat.json").write_text("", encoding="utf-8")
    sleeps = []

    class FakeProc:
        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        def terminate(self):
            pass

    c = YouTubeChat("https://www.youtube.com/@x", time.time(), tmp_path)

    def sleep(_):
        sleeps.append(1)
        if len(sleeps) >= 3:
            c._stop.set()

    monkeypatch.setattr(chat.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(chat.time, "sleep", sleep)
    c.run()
    assert len(sleeps) >= 3 and c.readable is False
    (tmp_path / "chat.live_chat.json").write_text('{"a":1}\n', encoding="utf-8")
    c._stop.clear()
    sleeps.clear()
    c.run()
    assert c.readable is True


# ---------- 4. chat and lag use the time of the first data ----------

def test_recorder_origin_is_when_the_first_segment_appears(tmp_path):
    rec = ingest.Recorder("https://kick.com/x", tmp_path, 60)
    rec.t0 = time.time() - 80
    assert rec.origin() == rec.t0                # nothing recorded yet
    (tmp_path / "seg_00000.ts").write_bytes(b"x")
    o = rec.origin()
    assert o > rec.t0 + 70
    assert rec.origin() == o                     # fixed once seen


def test_chat_rebase_shifts_earlier_messages(tmp_path):
    c = Flaky("https://kick.com/x", 100.0, tmp_path)
    c.times = [105.0 - 100.0, 130.0 - 100.0]
    c.rebase(190.0)
    assert c.t0 == 190.0 and c.times == [-85.0, -60.0]


# ---------- 5. a failed rating is not cached ----------

def test_failed_rating_is_retried(monkeypatch):
    calls = []
    answers = [None, {"hook": 5, "standalone": 5, "title": "t", "description": "",
                      "hashtags": []}]

    def fake(text, model):
        calls.append(text)
        return answers[min(len(calls) - 1, 1)]

    llm.rate.cache_clear()
    monkeypatch.setattr(llm, "_rate_uncached", fake)
    assert llm.rate("x", "m") is None
    assert llm.rate("x", "m")["hook"] == 5       # asked again after the failure
    assert llm.rate("x", "m")["hook"] == 5
    assert len(calls) == 2                       # the good answer is cached
    llm.rate.cache_clear()


# ---------- 6. stopping kills the process tree on Windows ----------

class Proc:
    pid = 4242

    def __init__(self):
        self.terminated = self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_stop_kills_the_process_tree_on_windows(tmp_path, monkeypatch):
    cmds = []
    monkeypatch.setattr(ingest.sys, "platform", "win32")
    monkeypatch.setattr(ingest.subprocess, "run", lambda cmd, **k: cmds.append(cmd))
    rec = ingest.Recorder("https://kick.com/x", tmp_path, 60)
    rec.src, rec.ff = Proc(), Proc()
    rec.stop()
    assert cmds == [["taskkill", "/PID", "4242", "/T", "/F"]] * 2
    assert not rec.src.terminated


def test_stop_terminates_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.sys, "platform", "linux")
    rec = ingest.Recorder("https://kick.com/x", tmp_path, 60)
    rec.src, rec.ff = Proc(), Proc()
    rec.stop()
    assert rec.src.terminated and rec.ff.terminated


def test_shutdown_stops_recorders_of_sessions_still_alive(monkeypatch):
    release = threading.Event()
    t = threading.Thread(target=release.wait, args=(10,), daemon=True)
    t.start()

    class Rec:
        stopped = False

        def stop(self):
            self.stopped = True

    rec = Rec()
    monkeypatch.setattr(watch, "_recorders", {rec})
    ev = threading.Event()
    watch._shutdown({"k": (t, ev)}, timeout=0.05)
    release.set()
    assert ev.is_set() and rec.stopped


# ---------- 7 and 9. the last segment is processed; log wording ----------

class FakeRec:
    """completed() grows by one segment on its second call; alive() is false."""

    def __init__(self, url, sdir, seg, quality):
        self.dir, self.calls, self.t0, self.tool = Path(sdir), 0, 1000.0, "fake"
        self.first_data = None

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (self.dir / f"seg_{i:05d}.ts").write_bytes(b"x" * 10)

    def origin(self):
        return 1090.0

    def completed(self):
        self.calls += 1
        n = 1 if self.calls == 1 else 2
        return [(self.dir / f"seg_{i:05d}.ts", i * 60.0, i * 60.0 + 60.0) for i in range(n)]

    def alive(self):
        return False

    def stop(self):
        pass


class FakeChat:
    times, readable = [], False

    def stop(self):
        pass

    def rebase(self, t0):
        self.t0 = t0


def run_session(tmp_path, monkeypatch, stop):
    lines, origins, offsets = [], [], []
    monkeypatch.setattr(watch, "WORK", tmp_path)
    monkeypatch.setattr(watch, "Recorder", FakeRec)
    monkeypatch.setattr(watch, "log", lines.append)
    monkeypatch.setattr(watch, "start_chat",
                        lambda plat, url, t0, sdir: origins.append(t0) or FakeChat())
    monkeypatch.setattr(watch, "concat_segments", lambda paths, dest: dest)

    def fake_process(win, url, plat, cfg, sdir, offset, chat_times, max_clips, taken, lock):
        assert lock is watch.LOCK and not lock.locked()   # the session must not hold it
        offsets.append(offset)
        return []

    monkeypatch.setattr(watch, "process_media", fake_process)
    cfg = {"segment_seconds": 60, "live_quality": "480p", "raw_retention_hours": 2,
           "raw_max_gb": 20, "live_skip_when_behind": True}
    watch._session("https://kick.com/sam", cfg, stop)
    return lines, origins, offsets


def test_session_processes_the_segment_that_closes_as_the_stream_ends(tmp_path, monkeypatch):
    lines, origins, offsets = run_session(tmp_path, monkeypatch, threading.Event())
    assert offsets == [0.0, 0.0]                 # seg 0 alone, then the window ending at seg 1
    assert origins == [1090.0]                   # chat starts at the first data, not the spawn
    assert any("processing segment seg_00001.ts" in ln for ln in lines)
    assert not any("closed (" in ln for ln in lines)
    assert any(ln.endswith("0s behind live") or "behind live" in ln for ln in lines)
    done = json.loads(next(tmp_path.rglob("processed.json")).read_text(encoding="utf-8"))
    assert "seg_00001.ts" in done                # the leftover segment is marked too


def test_no_final_pass_when_stop_was_requested(tmp_path, monkeypatch):
    stop = threading.Event()
    stop.set()
    _, _, offsets = run_session(tmp_path, monkeypatch, stop)
    assert offsets == [0.0]


def test_lag_uses_the_first_data_time(tmp_path, monkeypatch):
    lines, _, _ = run_session(tmp_path, monkeypatch, threading.Event())
    lag = [ln for ln in lines if "behind live" in ln][0]
    # origin is fixed at 1090.0, so lag is time.time() - 1150, not time.time() - 1060
    secs = float(lag.split(", ")[-1].split("s behind")[0])
    assert abs(secs - (time.time() - 1150.0)) < 30


# ---------- 8. size cap and empty session folders ----------

def make_session(root, name, files, processed, age_hours=0.0):
    d = root / "kick-x" / name
    d.mkdir(parents=True)
    for fname, size in files.items():
        p = d / fname
        p.write_bytes(b"x" * size)
        t = time.time() - age_hours * 3600
        os.utime(p, (t, t))
    (d / "processed.json").write_text(json.dumps(processed), encoding="utf-8")
    return d


def test_size_cap_deletes_oldest_processed_first_and_keeps_unprocessed(tmp_path):
    d = make_session(tmp_path, "s1", {"seg_00000.ts": 4000, "seg_00001.ts": 4000,
                                      "seg_00002.ts": 4000, "seg_00003.ts": 4000},
                     ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts"])
    for i, h in enumerate((0.3, 0.2, 0.1, 0.0)):
        t = time.time() - h * 3600
        os.utime(d / f"seg_{i:05d}.ts", (t, t))
    pipeline.ACTIVE.add(d)
    try:
        n = pipeline.cleanup(tmp_path, 2, max_gb=9000 / 1e9)     # cap is 9000 bytes
    finally:
        pipeline.ACTIVE.discard(d)
    left = sorted(p.name for p in d.glob("seg_*.ts"))
    assert n == 2 and left == ["seg_00002.ts", "seg_00003.ts"]   # oldest two went, 8000 B left


def test_size_cap_never_deletes_unprocessed_even_over_the_cap(tmp_path):
    d = make_session(tmp_path, "s1", {"seg_00000.ts": 5000, "seg_00001.ts": 5000}, [])
    assert pipeline.cleanup(tmp_path, 2, max_gb=1 / 1e9) == 0
    assert len(list(d.glob("seg_*.ts"))) == 2


def test_finished_empty_session_folder_is_removed_but_active_one_is_not(tmp_path):
    done = make_session(tmp_path, "done", {"seg_00000.ts": 10}, ["seg_00000.ts", "window.ts"],
                        age_hours=5)
    live = make_session(tmp_path.parent / (tmp_path.name + "_b"), "live", {"seg_00000.ts": 10},
                        ["seg_00000.ts"], age_hours=5)
    pipeline.ACTIVE.add(live)
    try:
        pipeline.cleanup(tmp_path, 2)
        pipeline.cleanup(live.parent.parent, 2)
    finally:
        pipeline.ACTIVE.discard(live)
    assert not done.exists() and not done.parent.exists()
    assert live.exists()


def test_raw_max_gb_default_and_yaml():
    from clipper.config import DEFAULTS, load_config
    assert DEFAULTS["raw_max_gb"] == 20
    assert load_config()["raw_max_gb"] == 20


# ---------- 2026-11-21 later: LLM outside the lock, simulcasts, token count ----------

def test_llm_call_runs_without_the_lock_and_heavy_work_with_it(tmp_path, monkeypatch):
    lock, seen = threading.Lock(), {}
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(80)]
    monkeypatch.setattr(pipeline, "transcribe",
                        lambda p, c: seen.setdefault("transcribe", lock.locked()) and words)
    monkeypatch.setattr(pipeline, "loudness_per_second", lambda p: __import__("numpy").zeros(60))
    monkeypatch.setattr(pipeline.llm, "configure", lambda c: None)
    monkeypatch.setattr(pipeline.llm, "available", lambda: True)
    monkeypatch.setattr(pipeline.llm, "active_model", lambda: "m")
    monkeypatch.setattr(pipeline.llm, "backend", lambda: "venice")
    monkeypatch.setattr(pipeline.llm, "supports_picks", lambda: True)
    monkeypatch.setattr(pipeline, "log", lambda *_: None)

    def picks(*a):
        seen["llm"] = lock.locked()
        return []

    monkeypatch.setattr(pipeline.llm, "pick_moments", picks)
    monkeypatch.setattr(pipeline.llm, "rate", lambda t, m: None)
    from clipper.config import load_config
    cfg = dict(load_config(), min_score=2.0)          # nothing is good enough to render
    assert pipeline.process_media("x.ts", "u", "kick", cfg, tmp_path, lock=lock) == []
    assert seen == {"transcribe": True, "llm": False}


def test_simulcast_keeps_only_the_bigger_stream(monkeypatch):
    monkeypatch.setattr(discover, "log", lambda *_: None)
    monkeypatch.setattr(discover, "_creds", lambda p: ("id", "secret"))
    monkeypatch.setitem(discover.FINDERS, "twitch", lambda c, n, now: [
        ("https://www.twitch.tv/Sam", 900), ("https://www.twitch.tv/solo", 50)])
    monkeypatch.setitem(discover.FINDERS, "kick", lambda c, n, now: [
        ("https://kick.com/sam", 1200), ("https://kick.com/other", 700)])
    cfg = {"discover_category": "Just Chatting", "discover_top": 3}
    assert discover.top_streams(cfg, 0) == [
        ("https://kick.com/sam", 1200), ("https://kick.com/other", 700),
        ("https://www.twitch.tv/solo", 50)]
    cfg["discover_skip_simulcast"] = False
    assert [u for u, _ in discover.top_streams(cfg, 0)] == [
        "https://kick.com/sam", "https://www.twitch.tv/Sam", "https://kick.com/other"]


def test_venice_token_usage_is_counted(monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, *a):
            return json.dumps({"choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 120, "completion_tokens": 30}}).encode()

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: Reply())
    monkeypatch.setitem(llm.USAGE, "calls", 0)
    monkeypatch.setitem(llm.USAGE, "input_tokens", 0)
    monkeypatch.setitem(llm.USAGE, "output_tokens", 0)
    assert llm._venice_chat("m", "s", "t", 10, 5) == "ok"
    assert llm.USAGE == {"calls": 1, "input_tokens": 120, "output_tokens": 30}


def test_language_is_sent_and_other_languages_are_dropped(monkeypatch):
    urls = []

    def call(url, head):
        urls.append(url)
        if "/games" in url:
            return {"data": [{"id": "7"}]}
        return {"data": [{"user_login": "a", "viewer_count": 9, "language": "en"},
                         {"user_login": "b", "viewer_count": 8, "language": "es"},
                         {"user_login": "c", "viewer_count": 7}]}

    monkeypatch.setattr(discover, "log", lambda *_: None)
    monkeypatch.setattr(discover, "_call", call)
    monkeypatch.setattr(discover, "_creds", lambda p: ("id", "secret"))
    monkeypatch.setattr(discover, "_token", lambda p, now: "t")
    discover._categories.clear()
    cfg = {"discover_category": "X", "discover_top": 5, "discover_platforms": ["twitch"],
           "discover_language": "EN"}
    assert [u for u, _ in discover.top_streams(cfg, 0)] == [
        "https://www.twitch.tv/a", "https://www.twitch.tv/c"]
    assert "language=en" in urls[-1]
    cfg["discover_language"] = None
    assert len(discover.top_streams(cfg, 0)) == 3 and "language" not in urls[-1]
    discover._categories.clear()
