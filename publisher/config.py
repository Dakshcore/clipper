import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
SECRETS = ROOT / ".secrets"
STATE_FILE = ROOT / "state.json"
PRIVACY = ("private", "unlisted", "public")

DEFAULTS = {
    "clips_dir": "../clipper/out",
    "destinations": ["youtube"],
    "dry_run": True,
    "privacy_status": "private",
    "min_score": 0.5,
    "skip_overlapping": True,
    "overlap_fraction": 0.5,
    "max_uploads_per_day": 5,
    "min_gap_minutes": 60,
    "retry_failed": False,
    "poll_seconds": 30,
    "youtube": {"api_daily_upload_limit": 100, "category_id": "22",
                "made_for_kids": False, "notify_subscribers": False},
}


def load_env(path: Path = ROOT / ".env") -> None:
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
    cfg["youtube"] = {**DEFAULTS["youtube"], **(data.get("youtube") or {})}
    if cfg["privacy_status"] not in PRIVACY:
        raise ValueError(f"privacy_status must be one of {PRIVACY}")
    if cfg["dry_run"] is not True and cfg["dry_run"] is not False:
        raise ValueError("dry_run must be true or false")
    cfg["clips_dir"] = (ROOT / cfg["clips_dir"]).resolve()
    return cfg


def daily_cap(cfg: dict, dest: str) -> int:
    """The owner's cap, never above the platform's API limit."""
    cap = int(cfg["max_uploads_per_day"])
    limit = (cfg.get(dest) or {}).get("api_daily_upload_limit")
    return max(0, min(cap, int(limit))) if limit is not None else max(0, cap)
