"""CLI: python -m publisher run | once | status"""
import argparse
import sys
import time
from datetime import datetime

from .config import STATE_FILE, daily_cap, load_config, load_env
from .core import Publisher
from .state import AlreadyRunning, RunLock, State


def make_publisher(cfg) -> Publisher:
    def youtube():
        from .adapters.youtube import YouTubeAdapter
        return YouTubeAdapter(cfg)
    return Publisher(cfg, State(STATE_FILE), {"youtube": youtube})


def once(cfg) -> int:
    pub = make_publisher(cfg)
    mode = ("DRY RUN (nothing is uploaded)" if cfg["dry_run"]
            else f"LIVE, privacy {cfg['privacy_status']}")
    print(f"publisher: {mode}; reading {cfg['clips_dir']}")
    try:
        s = pub.run_once()
    except Exception as e:
        if not getattr(e, "stop_run", False):
            raise
        print(e)
        return 2
    print("summary: " + ", ".join(f"{k} {v}" for k, v in s.items()))
    return 0


def run(cfg) -> int:
    while True:
        code = once(cfg)
        if code:
            return code
        try:
            time.sleep(float(cfg["poll_seconds"]))
        except KeyboardInterrupt:
            return 0


def status(cfg) -> int:
    pub = make_publisher(cfg)
    for dest in cfg["destinations"]:
        rows = pub.state.entries(dest)
        print(f"{dest}: {pub.uploads_today(dest)}/{daily_cap(cfg, dest)} uploads today, "
              f"dry_run {cfg['dry_run']}, privacy {cfg['privacy_status']}")
        for cid, e in sorted(rows):
            extra = e.get("remote_id") or e.get("reason") or e.get("error") or ""
            when = (datetime.fromtimestamp(e["uploaded_at"]).strftime("%Y-%m-%d %H:%M")
                    if e.get("uploaded_at") else "")
            print(f"  {e['status']:9} {cid}  {extra} {when}".rstrip())
        if not rows:
            print("  no clips recorded yet")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="publisher")
    ap.add_argument("cmd", choices=["run", "once", "status"])
    args = ap.parse_args()
    load_env()
    cfg = load_config()
    if args.cmd == "status":
        return status(cfg)
    try:
        with RunLock(STATE_FILE.with_suffix(".lock")):
            return {"run": run, "once": once}[args.cmd](cfg)
    except AlreadyRunning as e:
        print(e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
