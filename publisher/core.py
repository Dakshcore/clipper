"""Queue + rules. Decides what to upload and records the result."""
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from .config import daily_cap
from .queue import scan
from .state import State

QUOTA_TZ = ZoneInfo("America/Los_Angeles")   # YouTube quota resets at midnight PT
INTERRUPTED = "interrupted during upload; check the channel before retrying"


def quota_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, QUOTA_TZ).strftime("%Y-%m-%d")


TITLE_MAX, DESC_MAX_BYTES, TAGS_MAX = 100, 5000, 400   # YouTube: 100 chars, 5000 bytes, 500 chars


def _clean(text: str) -> str:
    """YouTube rejects < and > in titles and descriptions."""
    return str(text).replace("<", "").replace(">", "")


def _cut_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def build_metadata(meta: dict, cfg: dict) -> dict:
    """Title, description and tags in a form YouTube accepts."""
    tags, total = [], 0
    for h in meta["hashtags"]:
        t = _clean(h).strip().lstrip("#").strip()
        if not t or t in tags or total + len(t) + 3 > TAGS_MAX:
            continue
        tags.append(t)
        total += len(t) + 3          # YouTube also counts separators and quotes
    hashtags = " ".join("#" + t.replace(" ", "") for t in tags)
    title = " ".join(_clean(meta["title"]).split())[:TITLE_MAX].strip() or "Clip"
    desc = f"{_clean(meta['description']).strip()}\n\n{hashtags}".strip()
    return {
        "title": title,
        "description": _cut_bytes(desc, DESC_MAX_BYTES),
        "tags": tags,
        "privacy_status": cfg["privacy_status"],
    }


MIN_WORDS = 8   # transcripts shorter than this are never compared


def _words(meta: dict) -> list:
    t = meta.get("transcript")
    return re.findall(r"[a-z0-9']+", t.lower()) if isinstance(t, str) else []


def same_moment(a: dict, b: dict, fraction: float) -> bool:
    """True when two clips of the same source share a run of spoken words at
    least `fraction` of the shorter clip. Compares the sidecar transcripts, so
    it works for videos and live streams alike."""
    if a.get("source_url") != b.get("source_url"):
        return False
    wa, wb = _words(a), _words(b)
    if min(len(wa), len(wb)) < MIN_WORDS:
        return False
    run = SequenceMatcher(None, wa, wb, autojunk=False).find_longest_match(
        0, len(wa), 0, len(wb)).size
    return run >= fraction * min(len(wa), len(wb))


class Publisher:
    def __init__(self, cfg: dict, state: State, adapters: dict, now=time.time, out=print):
        """`adapters` maps destination name -> Adapter, or -> a function that
        builds one (only called when a real upload is about to happen)."""
        self.cfg, self.state, self.adapters, self.now, self.out = cfg, state, adapters, now, out

    def _adapter(self, dest: str):
        a = self.adapters[dest]
        if not hasattr(a, "upload"):
            a = self.adapters[dest] = a()
        return a

    def uploads_today(self, dest: str) -> int:
        """Upload attempts today. A failed attempt counts too, because the
        platform counts it against its own quota."""
        today = quota_day(self.now())
        n = 0
        for _, e in self.state.entries(dest):
            ts = e.get("uploaded_at") if e["status"] == "uploaded" else (
                e.get("updated_at") if e["status"] == "failed" else None)
            if ts and quota_day(ts) == today:
                n += 1
        return n

    def settled(self, entry) -> bool:
        """True when a clip must not be uploaded (again). An upload that was
        interrupted may already be on the channel, so `retry_failed` never
        retries it; the owner checks the channel and edits state.json."""
        if not entry:
            return False
        if entry["status"] == "uploaded":
            return True
        if entry["status"] == "failed":
            return not self.cfg["retry_failed"] or entry.get("error") == INTERRUPTED
        if entry["status"] == "skipped" and entry.get("held"):
            return True    # the owner held this clip back; never upload it
        return False

    def last_upload(self, dest: str) -> float:
        times = [e["uploaded_at"] for _, e in self.state.entries(dest) if e["status"] == "uploaded"]
        return max(times) if times else 0.0

    def duplicates(self, clips: list, dest: str) -> dict:
        """Map clip_id -> the clip it repeats. A clip is a duplicate when it
        covers the same moment as one already uploaded (or left failed), or as
        a better-scoring clip still waiting. Oldest wins a tie."""
        cfg = self.cfg
        if not cfg.get("skip_overlapping", True):
            return {}
        fraction = float(cfg.get("overlap_fraction", 0.5))
        kept, waiting = [], []
        for clip in clips:
            entry = self.state.get(clip.clip_id, dest)
            if self.settled(entry):
                kept.append(clip)
            elif clip.meta["score"] >= float(cfg["min_score"]):
                waiting.append(clip)
        dup = {}
        for clip in sorted(waiting, key=lambda c: -c.meta["score"]):   # stable: oldest first
            other = next((k for k in kept if same_moment(clip.meta, k.meta, fraction)), None)
            if other:
                dup[clip.clip_id] = other.clip_id
            else:
                kept.append(clip)
        return dup

    def run_once(self) -> dict:
        cfg, dry = self.cfg, bool(self.cfg["dry_run"])
        clips, problems = scan(cfg["clips_dir"])
        for name, why in problems:
            self.out(f"ignored {name}: {why}")
        summary = {"uploaded": 0, "would_upload": 0, "skipped": 0, "failed": 0, "waiting": 0}
        for dest in cfg["destinations"]:
            cap = daily_cap(cfg, dest)
            used = self.uploads_today(dest)
            last = self.last_upload(dest)
            gap = float(cfg["min_gap_minutes"]) * 60
            dup = self.duplicates(clips, dest)
            # best score first; equal scores keep oldest first
            for clip in sorted(clips, key=lambda c: -c.meta["score"]):
                entry = self.state.get(clip.clip_id, dest)
                status = entry["status"] if entry else None
                if self.settled(entry):
                    continue
                if clip.meta["score"] < float(cfg["min_score"]):
                    if status != "skipped":
                        self.state.set(clip.clip_id, dest, "skipped",
                                       reason=f"score {clip.meta['score']} below min_score")
                    summary["skipped"] += 1
                    self.out(f"[{dest}] skipped {clip.clip_id}: score {clip.meta['score']} "
                             f"below min_score {cfg['min_score']}")
                    continue
                if clip.clip_id in dup:
                    why = f"same moment as {dup[clip.clip_id]}"
                    if status != "skipped" or entry.get("reason") != why:
                        self.state.set(clip.clip_id, dest, "skipped", reason=why)
                    summary["skipped"] += 1
                    self.out(f"[{dest}] skipped {clip.clip_id}: {why}")
                    continue
                if status != "pending":
                    self.state.set(clip.clip_id, dest, "pending")
                if used >= cap:
                    summary["waiting"] += 1
                    self.out(f"[{dest}] waiting {clip.clip_id}: daily cap reached ({used}/{cap})")
                    continue
                if last and self.now() - last < gap:
                    summary["waiting"] += 1
                    mins = (gap - (self.now() - last)) / 60
                    self.out(f"[{dest}] waiting {clip.clip_id}: next upload allowed in {mins:.0f} min")
                    continue
                md = build_metadata(clip.meta, cfg)
                if dry:
                    summary["would_upload"] += 1
                    self.out(f"[{dest}] DRY RUN would upload {clip.video.name} "
                             f"({clip.video.stat().st_size} bytes) as {md['privacy_status']}\n"
                             f"    title: {md['title']}\n"
                             f"    description: {md['description']!r}\n"
                             f"    tags: {md['tags']}  score: {clip.meta['score']}")
                    used, last = used + 1, self.now()   # simulate the rules, nothing is saved
                    continue
                # Sign-in happens here, before any clip is touched: a failure
                # is not this clip's fault and must not mark it failed.
                try:
                    adapter = self._adapter(dest)
                except Exception as e:
                    if getattr(e, "stop_run", False):
                        raise
                    self.out(f"[{dest}] cannot reach {dest}, nothing uploaded this pass: "
                             f"{type(e).__name__}: {str(e)[:300]}")
                    break
                # write-ahead: if we die mid-upload the clip is not retried blindly
                self.state.set(clip.clip_id, dest, "failed", error=INTERRUPTED,
                               updated_at=self.now())
                try:
                    remote_id = adapter.upload(clip.video, md)
                except Exception as e:
                    msg = f"{type(e).__name__}: {str(e)[:300]}"
                    if getattr(e, "not_started", False):
                        self.state.set(clip.clip_id, dest, "pending")
                        self.out(f"[{dest}] BLOCKED {clip.clip_id} stays pending: {msg}")
                    else:
                        self.state.set(clip.clip_id, dest, "failed", error=msg,
                                       updated_at=self.now())
                        summary["failed"] += 1
                        self.out(f"[{dest}] FAILED {clip.clip_id}: {msg}")
                    if getattr(e, "stop_run", False):
                        raise
                    break   # the next clip would most likely fail the same way
                ts = self.now()
                self.state.set(clip.clip_id, dest, "uploaded", remote_id=remote_id,
                               uploaded_at=ts, privacy_status=md["privacy_status"])
                used, last = used + 1, ts
                summary["uploaded"] += 1
                self.out(f"[{dest}] uploaded {clip.clip_id} -> id {remote_id} ({md['privacy_status']})")
        return summary
