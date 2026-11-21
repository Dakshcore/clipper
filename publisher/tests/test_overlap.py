"""Clips that repeat the same moment are uploaded once."""
import json

from publisher.core import Publisher, same_moment
from publisher.state import State

T0 = 1_800_000_000.0
SRC = "https://www.youtube.com/watch?v=test"

A = ("what is a black hole well the name is a little misleading because black holes "
     "are not actually holes they are regions in space with a pull so strong that "
     "nothing can escape not even light")
B = ("supermassive black holes can have a mass of millions to tens of billions of "
     "stars and scientists believe that every galaxy in the universe contains one")


class FakeAdapter:
    name = "youtube"

    def __init__(self):
        self.uploaded = []

    def upload(self, clip, metadata):
        self.uploaded.append(clip.stem)
        return f"vid{len(self.uploaded)}"


def clip(folder, cid, transcript, score, second, source=SRC):
    (folder / f"{cid}.mp4").write_bytes(b"x")
    (folder / f"{cid}.json").write_text(json.dumps({
        "schema_version": 1, "clip_id": cid, "video_file": f"{cid}.mp4",
        "created_at": f"2026-11-17T10:00:{second:02d}Z", "duration": 20.0,
        "source_url": source, "platform": "youtube", "start_time": 0.0,
        "end_time": 20.0, "score": score, "transcript": transcript,
        "title": cid, "description": "d", "hashtags": ["#a", "#b", "#c"]}))


def publisher(tmp_path, adapter, **over):
    c = {"clips_dir": tmp_path / "out", "destinations": ["youtube"], "dry_run": False,
         "privacy_status": "private", "min_score": 0.5, "max_uploads_per_day": 5,
         "min_gap_minutes": 0, "retry_failed": False,
         "youtube": {"api_daily_upload_limit": 100}}
    c.update(over)
    return Publisher(c, State(tmp_path / "state.json"), {"youtube": adapter},
                     now=lambda: T0, out=lambda *_: None)


def test_same_moment():
    a = {"source_url": SRC, "transcript": A}
    longer = {"source_url": SRC, "transcript": A + " " + B}
    assert same_moment(a, longer, 0.5)                      # A is inside the longer clip
    assert not same_moment(a, {"source_url": SRC, "transcript": B}, 0.5)
    assert not same_moment(a, {"source_url": "https://other", "transcript": A}, 0.5)
    assert not same_moment({"source_url": SRC, "transcript": "hi"},
                           {"source_url": SRC, "transcript": "hi"}, 0.5)   # too short
    assert not same_moment(a, {"source_url": SRC}, 0.5)     # no transcript


def test_better_clip_of_same_moment_wins(tmp_path):
    (tmp_path / "out").mkdir()
    clip(tmp_path / "out", "old_low", A, 0.6, 1)
    clip(tmp_path / "out", "new_high", A + " " + B, 0.8, 2)
    clip(tmp_path / "out", "other", B.replace("supermassive", "giant") + " and more", 0.7, 3)
    adapter = FakeAdapter()
    s = publisher(tmp_path, adapter).run_once()
    assert "new_high" in adapter.uploaded and "old_low" not in adapter.uploaded
    assert s["skipped"] >= 1
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["clips"]["old_low"]["youtube"]["reason"] == "same moment as new_high"


def test_uploaded_clip_blocks_later_repeat_even_if_better(tmp_path):
    (tmp_path / "out").mkdir()
    clip(tmp_path / "out", "first", A, 0.6, 1)
    adapter = FakeAdapter()
    assert publisher(tmp_path, adapter).run_once()["uploaded"] == 1
    clip(tmp_path / "out", "repeat", A, 0.95, 2)
    s = publisher(tmp_path, adapter).run_once()
    assert s["uploaded"] == 0 and s["skipped"] == 1
    assert adapter.uploaded == ["first"]


def test_different_moments_both_upload(tmp_path):
    (tmp_path / "out").mkdir()
    clip(tmp_path / "out", "a", A, 0.6, 1)
    clip(tmp_path / "out", "b", B, 0.7, 2)
    adapter = FakeAdapter()
    assert publisher(tmp_path, adapter).run_once()["uploaded"] == 2


def test_can_be_turned_off(tmp_path):
    (tmp_path / "out").mkdir()
    clip(tmp_path / "out", "one", A, 0.6, 1)
    clip(tmp_path / "out", "two", A, 0.8, 2)
    adapter = FakeAdapter()
    assert publisher(tmp_path, adapter, skip_overlapping=False).run_once()["uploaded"] == 2
