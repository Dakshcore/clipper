"""Stage 2a: speech to text with word timestamps (faster-whisper)."""
import os
import site
import subprocess
from pathlib import Path

from .util import log

_model = None
_device = None


def _add_cuda_dlls() -> None:
    """Make pip-installed NVIDIA DLLs (cublas, cudnn) findable on Windows."""
    if os.name != "nt":
        return
    for sp in site.getsitepackages():
        for d in Path(sp).glob("nvidia/*/bin"):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass


def _load(cfg: dict, device: str):
    global _model, _device
    from faster_whisper import WhisperModel
    compute = "float16" if device == "cuda" else "int8"
    _model = WhisperModel(cfg["whisper_model"], device=device, compute_type=compute)
    _device = device
    return _model


def _audio(path):
    """Decode to 16 kHz mono float32 with ffmpeg (avoids the PyAV decoder)."""
    import numpy as np
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0:
        raise OSError("ffmpeg could not decode audio: " + p.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _run(path, cfg) -> list:
    segments, _info = _model.transcribe(
        _audio(path), word_timestamps=True, vad_filter=True,
        language=cfg.get("language") or None)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"word": text, "start": float(w.start), "end": float(w.end)})
    return words


def transcribe(path, cfg: dict) -> list:
    """Return [{'word', 'start', 'end'}] for the whole file."""
    global _model
    if _model is None:
        want = cfg.get("whisper_device", "auto")
        if want in ("auto", "cuda"):
            _add_cuda_dlls()
            try:
                _load(cfg, "cuda")
            except Exception as e:  # no usable GPU runtime
                log(f"GPU model load failed ({type(e).__name__}), using CPU")
                _load(cfg, "cpu")
        else:
            _load(cfg, "cpu")
    try:
        return _run(path, cfg)
    except RuntimeError as e:
        if _device != "cuda":
            raise
        log(f"GPU transcription failed ({str(e)[:80]}), retrying on CPU")
        _load(cfg, "cpu")
        return _run(path, cfg)


def device() -> str:
    return _device or "unloaded"
