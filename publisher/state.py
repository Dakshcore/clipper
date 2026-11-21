"""state.json: one status per clip per destination, so a restart never
uploads the same clip twice."""
import json
import os
from pathlib import Path

STATUSES = ("pending", "uploaded", "failed", "skipped")


class AlreadyRunning(RuntimeError):
    stop_run = True


class RunLock:
    """One publisher at a time. Two at once could upload the same clip twice.
    The lock is an OS lock on an open file, so it goes away when the process
    dies and can never be left stale."""

    def __init__(self, path: Path):
        self.path, self.fh = Path(path), None

    def __enter__(self):
        self.fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise AlreadyRunning(
                "Another publisher is already running (it holds "
                f"{self.path.name}). Nothing was uploaded by this one.") from None
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()   # closing releases the OS lock
            self.fh = None


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"version": 1, "clips": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, clip_id: str, dest: str):
        return self.data["clips"].get(clip_id, {}).get(dest)

    def set(self, clip_id: str, dest: str, status: str, **fields) -> dict:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        entry = {"status": status, **fields}
        self.data["clips"].setdefault(clip_id, {})[dest] = entry
        self.save()
        return entry

    def entries(self, dest: str) -> list:
        return [(cid, d[dest]) for cid, d in self.data["clips"].items() if dest in d]

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.data, indent=2))
            f.flush()
            os.fsync(f.fileno())   # a power cut must not leave an empty state file
        os.replace(tmp, self.path)
