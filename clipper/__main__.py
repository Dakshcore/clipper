"""CLI: python -m clipper video <url> | watch | list"""
import argparse
import sys

from .config import is_listed, load_config, load_env
from .util import OUT, WORK, done, log, probe


def cmd_video(args, cfg) -> int:
    from .ingest import download_video
    from .pipeline import cleanup, mark_processed, process_media
    from .platforms import platform_of, slug
    if not is_listed(args.url, cfg):
        log("that URL is not in config.yaml sources. Add it there first; the list is "
            "meant for channels you run or have permission to clip.")
        return 2
    cleanup(WORK, float(cfg["raw_retention_hours"]))
    wdir = WORK / slug(args.url)
    src = download_video(args.url, wdir)
    info = probe(src)
    done("ingest", f"{src.name} {info['width']}x{info['height']}, {info['duration']:.1f}s, "
         f"{info['size'] / 1e6:.1f} MB")
    taken = []     # filled with every picked range, including ones already clipped
    made = process_media(src, args.url, platform_of(args.url), cfg, wdir,
                         max_clips=int(cfg["max_clips_per_video"]), taken=taken,
                         verbose=True)
    mark_processed(wdir, [src.name])
    if not made and taken:
        log(f"all {len(taken)} picked windows are already clips in {OUT}; no new clip made")
        return 0
    if not made:
        log(f"no window scored at or above min_score ({cfg['min_score']}); no clip made")
        return 1
    for s in made:
        p = probe(OUT / s["video_file"])
        done("edit", f"{s['video_file']} {p['width']}x{p['height']}, {p['duration']:.2f}s, "
             f"{p['size']} bytes")
        done("output", f"{OUT / (s['clip_id'] + '.json')} title: {s['title']}")
    return 0


def cmd_watch(args, cfg) -> int:
    from .watch import watch
    watch(cfg, args.minutes)
    return 0


def cmd_list(args, cfg) -> int:
    from .output import list_clips
    clips = list_clips(OUT)
    if not clips:
        print("no clips in", OUT)
        return 0
    for c in clips:
        print(f"{c['clip_id']}  {c['platform']:8} {c['duration']:6.1f}s  "
              f"score {c['score']:.3f}  {c['title']}")
    print(f"{len(clips)} clip(s) in {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="clipper")
    ap.add_argument("--config", help="path to a config file (default clipper/config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("video", help="clip a finished video listed in config.yaml")
    v.add_argument("url")
    w = sub.add_parser("watch", help="watch the channels in config.yaml for live streams")
    w.add_argument("--minutes", type=float, help="stop after this many minutes")
    sub.add_parser("list", help="list clips in clipper/out")
    args = ap.parse_args()
    # Clip titles can hold characters the Windows console cannot print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    load_env()
    cfg = load_config(args.config)
    return {"video": cmd_video, "watch": cmd_watch, "list": cmd_list}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
