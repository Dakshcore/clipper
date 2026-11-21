from abc import ABC, abstractmethod
from pathlib import Path


class UploadBlocked(RuntimeError):
    """The destination refused before the upload started (sign-in expired,
    quota used up). Nothing was created there, so the clip stays pending, and
    the run stops because every other clip would hit the same wall."""
    stop_run = True
    not_started = True


class Adapter(ABC):
    """One destination. Add a platform by subclassing this."""

    name = ""

    @abstractmethod
    def upload(self, clip: Path, metadata: dict) -> str:
        """Upload the clip. `metadata` has title, description, tags,
        privacy_status. Return the platform's id for the upload."""

    @abstractmethod
    def status(self, id: str) -> dict:
        """Return the platform's view of an upload, e.g. {'state': ...}."""
