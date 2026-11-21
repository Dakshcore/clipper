import os
from pathlib import Path

import yaml

from .util import ROOT

DEFAULTS = {
    "sources": [],
    "poll_seconds": 60,
    "segment_seconds": 60,
    "discover_category": None,
    "discover_platforms": ["twitch", "kick"],
    "discover_top": 10,
    "discover_rescan_minutes": 30,
    "live_skip_when_behind": True,
    "live_quality": "720p",
    "raw_retention_hours": 2,
    "raw_max_gb": 20,
    "discover_skip_simulcast": True,
    "discover_language": None,
    "min_clip_seconds": 15,
    "max_clip_seconds": 60,
    "max_clips_per_video": 3,
    "min_score": 0.45,
    "dead_air_seconds": 0.6,
    "loudness_lufs": -14,
    "caption_style": "group",
    "layout": "auto",
    "facecam_region": None,
    "whisper_model": "small",
    "whisper_device": "auto",
    "language": None,
    "scorer": "clipper.scoring:score_window",
    "llm_backend": "local",
    "local_model": "llama3.2",
    "ollama_url": "http://localhost:11434",
    "llm_model": "claude-sonnet-5-5",
    "venice_model": "zai-org-glm-5",
    "llm_pick_moments": True,
    "llm_max_candidates": 8,
    "weights": {"text": 0.55, "loudness": 0.25, "chat": 0.20},
}


def load_env(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from clipper/.env without overriding the shell."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def load_config(path=None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **data}
    cfg["weights"] = {**DEFAULTS["weights"], **(data.get("weights") or {})}
    cfg["sources"] = [str(s).strip() for s in (cfg.get("sources") or [])]
    return cfg


def norm_url(url: str) -> str:
    return url.strip().rstrip("/").replace("http://", "https://")


def is_listed(url: str, cfg: dict) -> bool:
    return norm_url(url) in {norm_url(s) for s in cfg["sources"]}
