"""Stage 3: cut, reframe to 1080x1920, caption, normalise loudness (ffmpeg)."""
import json
import os
import re
import statistics
import subprocess
import urllib.request
from pathlib import Path

from .util import MODELS, log, probe, run

OUT_W, OUT_H = 1080, 1920
TOP_H = 640                      # facecam strip height in the stacked layout
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


# ---------- dead air ----------

def keep_intervals(words: list, cs: float, ce: float, dead_air: float) -> list:
    """Parts of [cs, ce] to keep, as (start, end) relative to cs."""
    total = ce - cs
    if not words or dead_air <= 0:
        return [(0.0, total)]
    pad = min(0.15, dead_air / 3)
    segs, cur = [], max(0.0, words[0]["start"] - cs - pad)
    for a, b in zip(words, words[1:]):
        if b["start"] - a["end"] > dead_air:
            segs.append((cur, a["end"] - cs + pad))
            cur = b["start"] - cs - pad
    segs.append((cur, min(total, words[-1]["end"] - cs + pad)))
    return [(round(s, 3), round(e, 3)) for s, e in segs if e > s]


def remap(t: float, intervals: list) -> float:
    """Map a time (relative to cs) onto the timeline after cuts."""
    acc = 0.0
    for s, e in intervals:
        if t < s:
            return acc
        if t <= e:
            return acc + (t - s)
        acc += e - s
    return acc


# ---------- faces and layout ----------

def _detector():
    import cv2
    path = MODELS / "face_detection_yunet_2023mar.onnx"
    if not path.exists():
        MODELS.mkdir(parents=True, exist_ok=True)
        log("downloading the face detection model (one time)")
        urllib.request.urlretrieve(YUNET_URL, path)
    return cv2.FaceDetectorYN.create(str(path), "", (640, 360), 0.7, 0.3, 50)


def sample_faces(src, cs: float, ce: float) -> list:
    """One sample per second: (t_rel, face) where face is (cx, cy, w, h) in
    source pixels or None. Frames come from a single ffmpeg pass."""
    import cv2
    import numpy as np
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    info = probe(src)
    W, H = info["width"], info["height"]
    sw = 640
    sh = _even(H * sw / W)
    scale = sw / W
    det = _detector()
    det.setInputSize((sw, sh))
    p = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{cs:.3f}",
         "-t", f"{ce - cs:.3f}", "-i", str(src), "-an",
         "-vf", f"fps=1,scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out, i, size = [], 0, sw * sh * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(sh, sw, 3)
        _, faces = det.detect(frame)
        face = None
        if faces is not None and len(faces):
            f = max(faces, key=lambda r: r[2] * r[3])
            fw, fh = f[2] / scale, f[3] / scale
            face = (f[0] / scale + fw / 2, f[1] / scale + fh / 2, fw, fh)
        out.append((float(i), face))
        i += 1
    p.wait()
    return out


def choose_layout(samples: list, W: int, H: int, cfg: dict) -> dict:
    region = cfg.get("facecam_region")
    mode = cfg.get("layout", "auto")
    if region:
        x, y, w, h = region
        return {"mode": "stack", "cam": (x * W, y * H, w * W, h * H)}
    faces = [f for _, f in samples if f]
    if mode == "center" or not faces or (mode == "auto" and len(faces) < len(samples) * 0.4):
        return {"mode": "center"}
    cx = statistics.median(f[0] for f in faces)
    cy = statistics.median(f[1] for f in faces)
    fw = statistics.median(f[2] for f in faces)
    spread = statistics.pstdev(f[0] for f in faces) if len(faces) > 1 else 0.0
    small = fw / W < 0.14
    off_centre = abs(cx / W - 0.5) > 0.2 and abs(cy / H - 0.5) > 0.15
    stable = spread < 0.03 * W
    if mode == "stack" or (mode == "auto" and small and off_centre and stable):
        cw = min(W, fw * 3.6)
        ch = min(H, cw * TOP_H / OUT_W)
        cw = ch * OUT_W / TOP_H
        x = min(max(0, cx - cw / 2), W - cw)
        y = min(max(0, cy - ch * 0.45), H - ch)
        return {"mode": "stack", "cam": (x, y, cw, ch)}
    return {"mode": "face"}


def follow_expr(samples: list, W: int, cw: int) -> str:
    """ffmpeg expression for the crop x that follows the face, one move per
    second at most, ignoring small jitters."""
    centre = (W - cw) / 2
    targets, last = [], None
    for t, f in samples:
        if f:
            last = min(max(0.0, f[0] - cw / 2), W - cw)
        targets.append((t, last))
    first = next((x for _, x in targets if x is not None), centre)
    cur, keys = first, []
    for t, x in targets:
        x = first if x is None else x
        if abs(x - cur) > 0.10 * cw:
            keys.append((t, x - cur))
            cur = x
    expr = f"{first:.0f}"
    for t, dx in keys:
        expr += f"+clip((t-{max(0.0, t - 1):.2f})/1,0,1)*({dx:.0f})"
    return expr


# ---------- captions ----------

def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _clean(word: str) -> str:
    return re.sub(r"[{}\\]", "", word).upper()


def build_ass(words: list, style: str, y: int) -> str:
    """words: [{'word','start','end'}] on the final (cut) timeline."""
    size = 150 if style == "word" else 92
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,"
        "100,100,0,0,1,7,2,5,40,40,40,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pos = f"{{\\an5\\pos(540,{y})}}"
    lines = []
    if style == "word":
        for i, w in enumerate(words):
            end = words[i + 1]["start"] if i + 1 < len(words) else w["end"] + 0.3
            end = min(end, w["end"] + 0.6)
            lines.append((w["start"], max(end, w["start"] + 0.05), pos + _clean(w["word"])))
    else:
        groups, cur = [], []
        for i, w in enumerate(words):
            cur.append(w)
            nxt = words[i + 1] if i + 1 < len(words) else None
            if (len(cur) == 3 or nxt is None or w["word"][-1:] in ".?!,"
                    or nxt["start"] - w["end"] > 0.5):
                groups.append(cur)
                cur = []
        for g in groups:
            for i, w in enumerate(g):
                end = g[i + 1]["start"] if i + 1 < len(g) else min(w["end"] + 0.3, g[-1]["end"] + 0.3)
                text = " ".join(
                    ("{\\c&H00FFFF&}" + _clean(x["word"]) + "{\\c&HFFFFFF&}") if j == i
                    else _clean(x["word"]) for j, x in enumerate(g))
                lines.append((w["start"], max(end, w["start"] + 0.05), pos + text))
    # no overlapping events
    for i in range(len(lines) - 1):
        s, e, t = lines[i]
        lines[i] = (s, min(e, lines[i + 1][0]), t)
    body = "".join(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{t}\n"
                   for s, e, t in lines if e > s)
    return head + body


# ---------- render ----------

def _select(intervals: list) -> str:
    return "+".join(f"between(t,{s},{e})" for s, e in intervals)


def _measure_loudness(src, cs, dur, achain, target) -> dict:
    p = run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{cs:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), "-vn",
             "-af", f"{achain}loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json",
             "-f", "null", "-"])
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, flags=re.S)
    return json.loads(m[-1]) if m else {}


def make_clip(src, window, cfg: dict, out_path: Path, work_dir: Path,
              caption_style: str = None) -> dict:
    """Render one vertical clip. Returns {'layout', 'duration', 'cuts'}."""
    src, out_path, work_dir = (Path(p).resolve() for p in (src, out_path, work_dir))
    info = probe(src)
    W, H = info["width"], info["height"]
    cs = max(0.0, window.start - 0.2)
    ce = window.end + 0.3
    if info["duration"] > 0:
        ce = min(info["duration"], ce)
    dur = ce - cs
    intervals = keep_intervals(window.words, cs, ce, float(cfg["dead_air_seconds"]))
    cut = len(intervals) > 1 or intervals[0][0] > 0.01 or intervals[0][1] < dur - 0.01

    samples = sample_faces(src, cs, ce)
    layout = choose_layout(samples, W, H, cfg)
    mode = layout["mode"]

    if mode == "stack":
        x, y, cw, ch = layout["cam"]
        gw = _even(min(W, H * OUT_W / (OUT_H - TOP_H)))
        v = (f"[0:v]fps=30,split[ca][ga];"
             f"[ca]crop={_even(cw)}:{_even(ch)}:{int(x)}:{int(y)},scale={OUT_W}:{TOP_H}[top];"
             f"[ga]crop={gw}:{_even(H)}:{int((W - gw) / 2)}:0,scale={OUT_W}:{OUT_H - TOP_H}[bot];"
             f"[top][bot]vstack,setsar=1[v0]")
        cap_y = TOP_H + 70
    else:
        if W / H > OUT_W / OUT_H:
            cw, ch = _even(H * OUT_W / OUT_H), _even(H)
        else:
            cw, ch = _even(W), _even(W * OUT_H / OUT_W)
        xexpr = follow_expr(samples, W, cw) if mode == "face" else f"{int((W - cw) / 2)}"
        v = (f"[0:v]fps=30,crop={cw}:{ch}:x='{xexpr}':y={int((H - ch) / 2)},"
             f"scale={OUT_W}:{OUT_H},setsar=1[v0]")
        cap_y = 1380

    style = caption_style or cfg.get("caption_style", "group")
    cap_words = [{"word": w["word"],
                  "start": remap(w["start"] - cs, intervals),
                  "end": remap(w["end"] - cs, intervals)} for w in window.words]
    ass = work_dir / f"captions_{os.getpid()}.ass"
    ass.write_text(build_ass(cap_words, style, cap_y), encoding="utf-8")

    sel = _select(intervals)
    vcut = f"select='{sel}',setpts=N/FRAME_RATE/TB," if cut else ""
    achain = f"aselect='{sel}',asetpts=N/SR/TB," if cut else ""
    target = cfg["loudness_lufs"]
    ln = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    try:
        m = _measure_loudness(src, cs, dur, achain, target)
        if m and "inf" not in str(m.get("input_i", "inf")):
            ln += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                   f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                   f":offset={m['target_offset']}:linear=true")
    except (RuntimeError, KeyError, ValueError):
        pass  # fall back to single-pass loudnorm

    graph = f"{v};[v0]{vcut}ass={ass.name}[v];[0:a]{achain}{ln}[a]"
    tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{cs:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-movflags", "+faststart", str(tmp)], cwd=str(work_dir))
        os.replace(tmp, out_path)
    finally:
        ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    return {"layout": mode, "duration": sum(e - s for s, e in intervals),
            "cuts": len(intervals) - 1}
