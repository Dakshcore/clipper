import re
from urllib.parse import urlparse


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("twitch.tv"):
        return "twitch"
    if host.endswith("kick.com"):
        return "kick"
    raise ValueError(f"Unsupported platform for URL: {url}")


def _parts(url: str):
    return [p for p in urlparse(url).path.split("/") if p]


def is_channel_url(url: str) -> bool:
    plat, parts = platform_of(url), _parts(url)
    if plat == "youtube":
        return bool(parts) and (
            parts[0].startswith("@") or parts[0] in ("channel", "c", "user")
        )
    if plat == "twitch":
        return len(parts) == 1 and parts[0] != "videos"
    return len(parts) == 1  # kick.com/<slug>


def channel_name(url: str) -> str:
    plat, parts = platform_of(url), _parts(url)
    if plat == "youtube":
        return parts[1] if parts[0] in ("channel", "c", "user") else parts[0]
    return parts[0]


def live_url(url: str) -> str:
    """URL to hand to the recorder for a channel."""
    if platform_of(url) == "youtube" and not url.rstrip("/").endswith("/live"):
        return url.rstrip("/") + "/live"
    return url


def slug(url: str) -> str:
    u = urlparse(url)
    raw = (u.path + ("_" + u.query if u.query else "")).strip("/")
    s = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return f"{platform_of(url)}-{s}"[:80]
