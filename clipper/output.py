"""Stage 4: the JSON sidecar written next to every clip. Schema is in README.md."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PLATFORMS = ("youtube", "twitch", "kick")
FIELDS = {
    "schema_version": int, "clip_id": str, "video_file": str, "created_at": str,
    "duration": float, "source_url": str, "platform": str, "start_time": float,
    "end_time": float, "score": float, "transcript": str, "title": str,
    "description": str, "hashtags": list,
}


def make_clip_id(platform: str, source_url: str, start: float, end: float) -> str:
    tail = re.sub(r"[^A-Za-z0-9]+", "", source_url.rstrip("/").split("/")[-1].split("=")[-1])[:20]
    h = hashlib.sha1(f"{source_url}|{start:.2f}|{end:.2f}".encode()).hexdigest()[:6]
    return f"{platform}_{tail or 'src'}_{int(start):06d}_{h}"


def clean_title(title: str, transcript: str) -> str:
    t = " ".join((title or "").split())
    if not t:
        t = re.split(r"(?<=[.?!])\s", transcript.strip(), maxsplit=1)[0]
    if len(t) > 80:
        t = t[:77].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
    return t or "Clip"


def clean_hashtags(tags, platform: str) -> list:
    out = []
    for tag in list(tags or []) + ["#shorts", f"#{platform}", "#clips"]:
        tag = "#" + re.sub(r"[^\w]", "", str(tag).lstrip("#"))
        if len(tag) > 1 and tag.lower() not in [o.lower() for o in out]:
            out.append(tag)
    return out[:5]


def build_sidecar(*, clip_id, source_url, platform, start, end, duration, score,
                  transcript, title="", description="", hashtags=None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "clip_id": clip_id,
        "video_file": f"{clip_id}.mp4",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": round(float(duration), 2),
        "source_url": source_url,
        "platform": platform,
        "start_time": round(float(start), 2),
        "end_time": round(float(end), 2),
        "score": round(float(score), 4),
        "transcript": transcript,
        "title": clean_title(title, transcript),
        "description": " ".join((description or "").split()) or transcript[:200],
        "hashtags": clean_hashtags(hashtags, platform),
    }


def validate_sidecar(d: dict) -> list:
    """Return a list of problems; empty means the sidecar is valid."""
    errs = []
    for k, typ in FIELDS.items():
        if k not in d:
            errs.append(f"missing field: {k}")
        elif typ is float and not (isinstance(d[k], (int, float)) and not isinstance(d[k], bool)):
            errs.append(f"{k} must be a number")
        elif typ is not float and not isinstance(d[k], typ):
            errs.append(f"{k} must be {typ.__name__}")
    extra = set(d) - set(FIELDS)
    if extra:
        errs.append(f"unknown fields: {sorted(extra)}")
    if errs:
        return errs
    if d["schema_version"] != SCHEMA_VERSION:
        errs.append("schema_version must be 1")
    if d["platform"] not in PLATFORMS:
        errs.append("platform must be youtube, twitch or kick")
    if not d["title"] or len(d["title"]) > 80:
        errs.append("title must be 1-80 characters")
    if not 3 <= len(d["hashtags"]) <= 5:
        errs.append("hashtags must have 3-5 items")
    if any(not isinstance(h, str) or not re.fullmatch(r"#\w+", h) for h in d["hashtags"]):
        errs.append("each hashtag must look like #word")
    if not 0 <= d["score"] <= 1:
        errs.append("score must be 0..1")
    if d["end_time"] <= d["start_time"]:
        errs.append("end_time must be after start_time")
    if d["video_file"] != f"{d['clip_id']}.mp4":
        errs.append("video_file must be <clip_id>.mp4")
    return errs


def write_sidecar(out_dir: Path, d: dict) -> Path:
    errs = validate_sidecar(d)
    if errs:
        raise ValueError("invalid sidecar: " + "; ".join(errs))
    path = Path(out_dir) / f"{d['clip_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # the JSON appears last, so a JSON means a finished pair
    return path


def list_clips(out_dir: Path) -> list:
    out = []
    for p in sorted(Path(out_dir).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda d: d.get("created_at", ""))
