"""Find finished clips. The only interface to the clipper is its output
folder: <clip_id>.mp4 next to <clip_id>.json (schema in clipper/README.md).
This module only reads."""
import json
import math
from dataclasses import dataclass
from pathlib import Path

REQUIRED = {"schema_version": int, "clip_id": str, "video_file": str, "created_at": str,
            "score": (int, float), "title": str, "description": str, "hashtags": list,
            "source_url": str, "platform": str}


@dataclass
class Clip:
    clip_id: str
    video: Path
    meta: dict


def scan(clips_dir: Path) -> tuple:
    """Return (clips oldest first, [(file name, problem)])."""
    clips, problems = [], []
    for j in sorted(Path(clips_dir).glob("*.json")):
        try:
            meta = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append((j.name, f"unreadable JSON: {e}"))
            continue
        if not isinstance(meta, dict):
            problems.append((j.name, "not a JSON object"))
            continue
        bad = [k for k, t in REQUIRED.items() if not isinstance(meta.get(k), t)]
        if bad:
            problems.append((j.name, f"missing or wrong fields: {bad}"))
            continue
        if not all(isinstance(h, str) for h in meta["hashtags"]):
            problems.append((j.name, "hashtags must all be text"))
            continue
        if isinstance(meta["score"], bool) or not math.isfinite(meta["score"]):
            problems.append((j.name, f"score is not a number: {meta['score']}"))
            continue
        if meta["schema_version"] != 1:
            problems.append((j.name, f"unsupported schema_version {meta['schema_version']}"))
            continue
        video = j.parent / Path(meta["video_file"]).name
        if meta["clip_id"] != j.stem or not video.is_file():
            problems.append((j.name, "clip id mismatch or video file missing"))
            continue
        clips.append(Clip(meta["clip_id"], video, meta))
    clips.sort(key=lambda c: (c.meta["created_at"], c.clip_id))
    return clips, problems
