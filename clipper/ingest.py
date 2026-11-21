"""Stage 1: get media. Finished videos via yt-dlp, live streams via streamlink
(yt-dlp as fallback) cut into rolling segments by ffmpeg."""
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

from .util import RECORDER_PRIORITY, log, run

PY = sys.executable


def download_video(url: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    p = run([
        PY, "-m", "yt_dlp", "--no-playlist", "--no-progress", "-q",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(dest / "source.%(ext)s"),
        "--print", "after_move:filepath", url,
    ])
    path = Path(p.stdout.strip().splitlines()[-1])
    if not path.exists():
        raise RuntimeError(f"yt-dlp reported {path} but it is missing")
    return path


def check_live(url: str) -> bool:
    """True when the channel has a live stream right now."""
    try:
        p = subprocess.run([PY, "-m", "streamlink", "--json", url],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout or "{}")
        if data.get("streams"):
            return True
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    try:
        p = subprocess.run(
            [PY, "-m", "yt_dlp", "--no-playlist", "-q", "--no-warnings",
             "--print", "is_live", url],
            capture_output=True, text=True, timeout=90)
        return p.stdout.strip().splitlines()[-1:] == ["True"]
    except subprocess.TimeoutExpired:
        return False


RUNGS = (1080, 936, 900, 720, 540, 480, 360)   # heights live platforms offer, high to low


def stream_spec(quality: str) -> tuple:
    """(streamlink stream list, yt-dlp format) for a live_quality value.
    "best" or empty records the top quality; "720p" records 720p (60 fps when
    offered). A channel without that height gets the nearest lower one, then
    the nearest higher one, and the best stream only as a last resort."""
    q = str(quality or "best").strip().lower()
    digits = q.rstrip("p")
    if q == "best" or not digits.isdigit():
        return "best", "b"
    want = int(digits)
    lower = [r for r in RUNGS if r < want]
    higher = sorted(r for r in RUNGS if r > want)
    names = [f"{want}p60", f"{want}p"]
    for r in lower + higher:
        names += [f"{r}p", f"{r}p60"] if r < want else [f"{r}p60", f"{r}p"]
    return ",".join(names + ["best"]), f"b[height<={want}]/b"


def _terminate(p) -> None:
    """Stop a process. On Windows terminate() leaves child processes (yt-dlp
    starts its own ffmpeg) running, so the whole tree is killed there."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    p.terminate()


class Recorder:
    """Record a live stream into `seg_00000.ts`, `seg_00001.ts`, ..."""

    START_WAIT = 90   # seconds to wait for streamlink's first data

    def __init__(self, url: str, out_dir: Path, segment_seconds: int = 60,
                 quality: str = "best"):
        self.url, self.dir, self.seg = url, out_dir, segment_seconds
        self.streams, self.fmt = stream_spec(quality)
        self.src = self.ff = None
        self.t0 = 0.0
        self.first_data = None   # wall time the first segment file appeared
        self.tool = ""

    def _spawn(self, tool: str) -> None:
        if tool == "streamlink":
            src_cmd = [PY, "-m", "streamlink", "--stdout", self.url, self.streams]
        else:
            src_cmd = [PY, "-m", "yt_dlp", "-q", "--no-warnings", "-f", self.fmt,
                       "-o", "-", self.url]
        # Recorders must keep up with live; processing can wait. Measured:
        # five 720p streams record at live speed alone and fall behind when
        # transcription and rendering compete at the same priority.
        self.src = subprocess.Popen(src_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=RECORDER_PRIORITY)
        self.ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-c", "copy", "-f", "segment", "-segment_time", str(self.seg),
             "-reset_timestamps", "1", "-segment_format", "mpegts",
             "-segment_list", "segments.csv", "-segment_list_type", "csv",
             "seg_%05d.ts"],
            stdin=self.src.stdout, cwd=str(self.dir),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=RECORDER_PRIORITY)
        self.src.stdout.close()
        self.tool = tool
        self.t0 = time.time()
        self.first_data = None

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._spawn("streamlink")
        # With many streams starting together streamlink can take well over
        # 30 s to deliver data; falling back early only makes it slower.
        deadline = time.time() + self.START_WAIT
        while time.time() < deadline:
            if any(self.dir.glob("seg_*.ts")) and self.alive():
                self.origin()
                return
            if not self.alive():
                break
            time.sleep(1)
        if not any(self.dir.glob("seg_*.ts")):
            log("streamlink produced no data, falling back to yt-dlp")
            self.stop()
            self._spawn("yt-dlp")

    def origin(self) -> float:
        """Wall time of the media timeline's zero: the first data written, or
        the spawn time while nothing has arrived yet."""
        if self.first_data is None and any(self.dir.glob("seg_*.ts")):
            self.first_data = time.time()
        return self.first_data if self.first_data is not None else self.t0

    def alive(self) -> bool:
        return bool(self.ff and self.ff.poll() is None
                    and self.src and self.src.poll() is None)

    def completed(self) -> list:
        """[(path, start, end)] of closed segments, on the stream timeline."""
        f = self.dir / "segments.csv"
        if not f.exists():
            return []
        out, t = [], 0.0
        for row in csv.reader(f.read_text(encoding="utf-8").splitlines()):
            if len(row) < 3:
                continue
            d = max(0.0, float(row[2]) - float(row[1]))
            out.append((self.dir / row[0], t, t + d))
            t += d
        return out

    def stop(self) -> None:
        for p in (self.src, self.ff):
            if p and p.poll() is None:
                _terminate(p)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()


def concat_segments(paths: list, dest: Path) -> Path:
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.name}'\n" for p in paths), encoding="utf-8")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", lst.name, "-c", "copy", dest.name], cwd=str(dest.parent))
    lst.unlink(missing_ok=True)
    return dest
