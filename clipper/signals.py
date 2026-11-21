"""Stage 2b: cheap signals - audio loudness and chat message rate."""
import subprocess

import numpy as np

SR = 16000


def loudness_per_second(path) -> np.ndarray:
    """RMS level in dB for each second of audio."""
    p = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    while True:
        buf = p.stdout.read(SR * 2)
        if not buf:
            break
        x = np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        out.append(20 * np.log10(max(rms, 1.0) / 32768.0))
    p.wait()
    return np.array(out, dtype=np.float32)


def loudness_score(db: np.ndarray, start: float, end: float) -> float:
    """0..1: how far the loud parts of the window sit above the file's median."""
    if len(db) == 0:
        return 0.0
    win = db[int(start): max(int(start) + 1, int(np.ceil(end)))]
    if len(win) == 0:
        return 0.0
    spike = float(np.percentile(win, 95) - np.median(db))
    return float(np.clip(spike / 12.0, 0.0, 1.0))


def chat_score(times: list, start: float, end: float):
    """0..1 for chat rate in the window against the overall rate.
    None when chat was not readable or too thin to mean anything."""
    if not times or len(times) < 5:
        return None
    span = max(times) - min(times)
    if span <= 0 or end <= start:
        return None
    base = len(times) / span
    n = sum(1 for t in times if start <= t <= end)
    rate = n / (end - start)
    return float(np.clip((rate / base - 1.0) / 2.0, 0.0, 1.0))
