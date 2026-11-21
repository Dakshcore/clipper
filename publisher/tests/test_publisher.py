import json
import pytest

from publisher.core import Publisher
from publisher.state import State

DAY = 86400
T0 = 1_800_000_000.0   # fixed clock


class FakeAdapter:
    name = "youtube"

    def __init__(self):
        self.uploaded = []

    def upload(self, clip, metadata):
        self.uploaded.append(clip.stem)
        return f"vid{len(self.uploaded)}"

    def status(self, id):
        return {"state": "uploaded"}


def make_clips(folder, n, score=0.9):
    for i in range(n):
        cid = f"youtube_test_{i:06d}_abc123"
        (folder / f"{cid}.mp4").write_bytes(b"x")
        (folder / f"{cid}.json").write_text(json.dumps({
            "schema_version": 1, "clip_id": cid, "video_file": f"{cid}.mp4",
            "created_at": f"2026-11-17T10:00:{i:02d}Z", "duration": 20.0,
            "source_url": "https://www.youtube.com/watch?v=test", "platform": "youtube",
            "start_time": 0.0, "end_time": 20.0, "score": score, "transcript": "hi",
            "title": f"Clip {i}", "description": "d", "hashtags": ["#a", "#b", "#c"]}))


def cfg(folder, **over):
    c = {"clips_dir": folder, "destinations": ["youtube"], "dry_run": False,
         "privacy_status": "private", "min_score": 0.5, "max_uploads_per_day": 5,
         "min_gap_minutes": 0, "retry_failed": False,
         "youtube": {"api_daily_upload_limit": 100}}
    c.update(over)
    return c


def publisher(tmp_path, adapter, clock, **over):
    # a new State object each time = a process restart reading state.json
    return Publisher(cfg(tmp_path / "out", **over), State(tmp_path / "state.json"),
                     {"youtube": adapter}, now=lambda: clock[0], out=lambda *_: None)


def test_restart_never_uploads_twice(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 3)
    adapter, clock = FakeAdapter(), [T0]
    assert publisher(tmp_path, adapter, clock).run_once()["uploaded"] == 3
    for _ in range(3):   # three restarts
        assert publisher(tmp_path, adapter, clock).run_once()["uploaded"] == 0
    assert len(adapter.uploaded) == 3
    assert len(set(adapter.uploaded)) == 3


def test_interrupted_upload_is_not_retried_blindly(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 1)
    clock = [T0]

    class Dies(FakeAdapter):
        def upload(self, clip, metadata):
            raise KeyboardInterrupt   # process killed mid-upload

    try:
        publisher(tmp_path, Dies(), clock).run_once()
    except KeyboardInterrupt:
        pass
    adapter = FakeAdapter()
    assert publisher(tmp_path, adapter, clock).run_once()["uploaded"] == 0
    assert adapter.uploaded == []


def test_daily_cap(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 5)
    adapter, clock = FakeAdapter(), [T0]
    s = publisher(tmp_path, adapter, clock, max_uploads_per_day=2).run_once()
    assert s["uploaded"] == 2 and s["waiting"] == 3
    # same quota day, after a restart: still capped
    clock[0] = T0 + 3600
    assert publisher(tmp_path, adapter, clock, max_uploads_per_day=2).run_once()["uploaded"] == 0
    assert len(adapter.uploaded) == 2
    # next day: two more
    clock[0] = T0 + DAY + 3600
    assert publisher(tmp_path, adapter, clock, max_uploads_per_day=2).run_once()["uploaded"] == 2
    assert len(adapter.uploaded) == 4


def test_cap_cannot_exceed_api_limit(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 4)
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter, [T0], max_uploads_per_day=500,
                  youtube={"api_daily_upload_limit": 3}).run_once()
    assert s["uploaded"] == 3


def test_min_gap(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 2)
    adapter, clock = FakeAdapter(), [T0]
    s = publisher(tmp_path, adapter, clock, min_gap_minutes=60).run_once()
    assert s["uploaded"] == 1 and s["waiting"] == 1
    clock[0] = T0 + 3601
    assert publisher(tmp_path, adapter, clock, min_gap_minutes=60).run_once()["uploaded"] == 1


def test_min_score_skips(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 2, score=0.3)
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter, [T0]).run_once()
    assert s["skipped"] == 2 and adapter.uploaded == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert all(d["youtube"]["status"] == "skipped" for d in state["clips"].values())


def test_dry_run_uploads_nothing(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 2)
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter, [T0], dry_run=True).run_once()
    assert s["would_upload"] == 2 and adapter.uploaded == []


def _statuses(tmp_path):
    data = json.loads((tmp_path / "state.json").read_text())["clips"]
    return sorted(d["youtube"]["status"] for d in data.values())


def test_sign_in_failure_marks_no_clip_failed(tmp_path):
    # An expired Google sign-in fails while the adapter is built, before any upload.
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 3)

    def factory():
        raise RuntimeError("invalid_grant: Token has been expired or revoked.")

    s = publisher(tmp_path, factory, [T0]).run_once()
    assert s["failed"] == 0
    assert "failed" not in _statuses(tmp_path)


def test_one_upload_error_fails_one_clip_not_the_queue(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 3)

    class Broken(FakeAdapter):
        def upload(self, clip, metadata):
            raise ConnectionError("network down")

    s = publisher(tmp_path, Broken(), [T0]).run_once()
    assert s["failed"] == 1
    assert _statuses(tmp_path).count("failed") == 1


def test_blocked_upload_stays_pending_and_stops_the_run(tmp_path):
    import pytest
    from publisher.adapters.base import UploadBlocked
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 3)

    class Quota(FakeAdapter):
        def upload(self, clip, metadata):
            raise UploadBlocked("quotaExceeded")

    with pytest.raises(UploadBlocked):
        publisher(tmp_path, Quota(), [T0]).run_once()
    assert "failed" not in _statuses(tmp_path)


def test_youtube_refusals_that_leave_the_clip_pending():
    from publisher.adapters.youtube import http_reason, is_blocked
    body = json.dumps({"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}})
    assert http_reason(body) == "quotaExceeded"
    assert http_reason(b"not json") == "" and http_reason("{}") == ""
    assert is_blocked(403, "quotaExceeded")
    assert is_blocked(401, "")
    assert not is_blocked(400, "invalidTitle")
    assert not is_blocked(500, "")


def test_second_publisher_is_refused(tmp_path):
    import pytest
    from publisher.state import AlreadyRunning, RunLock
    with RunLock(tmp_path / "state.lock"):
        with pytest.raises(AlreadyRunning):
            with RunLock(tmp_path / "state.lock"):
                pass
    with RunLock(tmp_path / "state.lock"):   # free again after release
        pass


def test_interrupted_upload_is_not_retried_even_with_retry_failed(tmp_path):
    from publisher.core import INTERRUPTED
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 1)
    State(tmp_path / "state.json").set("youtube_test_000000_abc123", "youtube", "failed",
                                       error=INTERRUPTED, updated_at=T0 - 2 * DAY)
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter, [T0], retry_failed=True).run_once()
    assert s["uploaded"] == 0 and adapter.uploaded == []


def test_metadata_is_made_acceptable_to_youtube():
    from publisher.core import build_metadata
    md = build_metadata({"title": "  <b>Big</b>   win  ", "description": "a <3 b" + "é" * 6000,
                         "hashtags": ["#", "#fun", "fun", "#" + "x" * 600, "# two words"]},
                        {"privacy_status": "private"})
    assert md["title"] == "bBig/b win"
    assert "<" not in md["description"] and ">" not in md["description"]
    assert len(md["description"].encode("utf-8")) <= 5000
    assert md["tags"] == ["fun", "two words"]
    assert build_metadata({"title": "<>", "description": "", "hashtags": []},
                          {"privacy_status": "private"})["title"] == "Clip"


def test_bad_hashtags_and_scores_are_ignored_not_fatal(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_clips(out, 3)
    ids = [f"youtube_test_{i:06d}_abc123" for i in range(3)]
    for cid, key, val in ((ids[0], "hashtags", ["#a", 5]), (ids[1], "score", float("nan"))):
        p = out / f"{cid}.json"
        meta = json.loads(p.read_text())
        meta[key] = val
        p.write_text(json.dumps(meta))
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter, [T0]).run_once()
    assert s["uploaded"] == 1 and adapter.uploaded == [ids[2]]


def test_best_score_uploads_first(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    make_clips(out, 3)
    p = out / "youtube_test_000002_abc123.json"
    meta = json.loads(p.read_text())
    meta["score"] = 0.99
    p.write_text(json.dumps(meta))
    adapter = FakeAdapter()
    publisher(tmp_path, adapter, [T0], max_uploads_per_day=1).run_once()
    assert adapter.uploaded == ["youtube_test_000002_abc123"]


def test_failed_attempts_count_toward_the_daily_cap(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 3)
    State(tmp_path / "state.json").set("youtube_test_000000_abc123", "youtube", "failed",
                                       error="HttpError: boom", updated_at=T0)
    adapter = FakeAdapter()   # T0 is midnight Pacific, so stay on the same day
    s = publisher(tmp_path, adapter, [T0 + 60], max_uploads_per_day=2).run_once()
    assert s["uploaded"] == 1


def test_network_error_resumes_the_same_upload():
    from publisher.adapters.youtube import finish_upload

    class Req:
        def __init__(self, script):
            self.script, self.calls = list(script), 0

        def next_chunk(self, num_retries=0):
            self.calls += 1
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return None, step

    waits = []
    req = Req([ConnectionResetError("reset"), None, TimeoutError("slow"), {"id": "vid1"}])
    resp = finish_upload(req, waits=(1, 2, 3), sleep=waits.append, say=lambda m: None)
    assert resp == {"id": "vid1"} and req.calls == 4 and waits == [1, 2]   # one request object throughout

    # the outage outlasts every wait: the error comes out and the clip is failed as before
    req = Req([ConnectionResetError("reset")] * 3)
    with pytest.raises(ConnectionResetError):
        finish_upload(req, waits=(1, 2), sleep=waits.append, say=lambda m: None)
    assert req.calls == 3

    # an error that is not the network is never retried
    req = Req([ValueError("bad request")])
    with pytest.raises(ValueError):
        finish_upload(req, waits=(1, 2), sleep=waits.append, say=lambda m: None)
    assert req.calls == 1


def test_a_held_clip_is_never_uploaded(tmp_path):
    (tmp_path / "out").mkdir()
    make_clips(tmp_path / "out", 2)
    held = "youtube_test_000000_abc123"
    State(tmp_path / "state.json").set(held, "youtube", "skipped", reason="held by owner", held=True)
    adapter, clock = FakeAdapter(), [T0]
    for _ in range(2):
        publisher(tmp_path, adapter, clock).run_once()
    assert adapter.uploaded == ["youtube_test_000001_abc123"]
    assert State(tmp_path / "state.json").get(held, "youtube")["held"] is True
