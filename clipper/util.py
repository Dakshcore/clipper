import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
OUT = ROOT / "out"
MODELS = ROOT / "models"


def log(msg: str) -> None:
    print(f"[clipper] {msg}", flush=True)


def done(stage: str, evidence: str) -> None:
    print(f"Done: {stage} - {evidence}", flush=True)


# Windows priority classes; 0 elsewhere, where the flag is not used.
RECORDER_PRIORITY = getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def lower_own_priority() -> bool:
    """Put this process (transcription, face detection, and the render
    commands it starts) below normal priority, so the recorders, which are
    started above normal, get the CPU first. Windows only; False elsewhere."""
    if not BELOW_NORMAL:
        return False
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return bool(k.SetPriorityClass(k.GetCurrentProcess(), BELOW_NORMAL))


def run(cmd, **kw) -> subprocess.CompletedProcess:
    """Run a command, capture output, raise with stderr tail on failure."""
    p = subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kw
    )
    if p.returncode != 0:
        tail = "\n".join((p.stderr or "").strip().splitlines()[-15:])
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed ({p.returncode}):\n{tail}")
    return p


def probe(path) -> dict:
    """Width, height, duration and audio presence of a media file."""
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    data = json.loads(p.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    dur = float(data["format"].get("duration") or (v or {}).get("duration") or 0)
    return {
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "duration": dur,
        "has_audio": a is not None,
        "size": int(data["format"].get("size") or 0),
    }
