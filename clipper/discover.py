"""Find the biggest live streams in a category on Twitch and Kick.

Both use the platform's official API with an app access token (client
credentials). The client id and secret come from the environment and are
only sent to the platform's own token endpoint.
  Twitch: TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
  Kick:   KICK_CLIENT_ID,   KICK_CLIENT_SECRET
A platform with no credentials, or one that fails, is skipped with a log line.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import log

TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"
TWITCH_API = "https://api.twitch.tv/helix"
KICK_TOKEN = "https://id.kick.com/oauth/token"
KICK_API = "https://api.kick.com/public/v1"

_tokens = {}       # platform -> (token, expires_at)
_categories = {}   # (platform, lowercased name) -> id


class DiscoverError(Exception):
    pass


def _creds(platform: str):
    cid = os.environ.get(f"{platform.upper()}_CLIENT_ID", "").strip()
    secret = os.environ.get(f"{platform.upper()}_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def _call(url: str, headers: dict = None, form: dict = None):
    data = urllib.parse.urlencode(form).encode("utf-8") if form else None
    # Kick answers 403 to the default "Python-urllib" agent, so name ourselves.
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "clipper-discovery/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise DiscoverError(f"HTTP {e.code} from {urllib.parse.urlparse(url).hostname}")
    except (OSError, ValueError) as e:
        raise DiscoverError(f"{type(e).__name__} from {urllib.parse.urlparse(url).hostname}")


def _token(platform: str, now: float) -> str:
    tok = _tokens.get(platform)
    if tok and tok[1] > now + 60:
        return tok[0]
    cid, secret = _creds(platform)
    reply = _call(TWITCH_TOKEN if platform == "twitch" else KICK_TOKEN,
                  form={"client_id": cid, "client_secret": secret,
                        "grant_type": "client_credentials"})
    try:
        token = str(reply["access_token"])
    except (KeyError, TypeError):
        raise DiscoverError(f"{platform} gave no access token")
    _tokens[platform] = (token, now + float(reply.get("expires_in") or 3600))
    return token


def _q(params) -> str:
    return urllib.parse.urlencode(params)


def _speaks(row: dict, language: str) -> bool:
    """True when no language is asked for, or the stream's own language tag
    matches. A row without a tag is kept: the platform already filtered it."""
    tag = str(row.get("language") or "").lower()
    return not language or not tag or tag == language.lower()


def twitch_top(category: str, n: int, now: float, language: str = "") -> list:
    """[(channel_url, viewers)] for the n biggest live streams in a Twitch category."""
    head = {"Client-Id": _creds("twitch")[0],
            "Authorization": "Bearer " + _token("twitch", now)}
    key = ("twitch", category.lower())
    if key not in _categories:
        games = _call(f"{TWITCH_API}/games?{_q({'name': category})}", head).get("data") or []
        if not games:
            raise DiscoverError(f"Twitch has no category named {category!r}")
        _categories[key] = games[0]["id"]
    # Twitch returns live streams sorted by viewer count, biggest first.
    params = {"game_id": _categories[key], "type": "live", "first": max(1, min(n, 100))}
    if language:
        params["language"] = language
    rows = _call(f"{TWITCH_API}/streams?{_q(params)}", head).get("data") or []
    return [(f"https://www.twitch.tv/{r['user_login']}", int(r.get("viewer_count") or 0))
            for r in rows if r.get("user_login") and _speaks(r, language)][:n]


def kick_top(category: str, n: int, now: float, language: str = "") -> list:
    """[(channel_url, viewers)] for the n biggest live streams in a Kick category."""
    head = {"Authorization": "Bearer " + _token("kick", now)}
    key = ("kick", category.lower())
    if key not in _categories:
        cats = _call(f"{KICK_API}/categories?{_q({'q': category})}", head).get("data") or []
        exact = [c for c in cats if str(c.get("name", "")).lower() == category.lower()]
        if not exact:
            near = ", ".join(str(c.get("name")) for c in cats[:5]) or "none"
            raise DiscoverError(f"Kick has no category named {category!r} (closest: {near})")
        _categories[key] = exact[0]["id"]
    params = {"category_id": _categories[key], "sort": "viewer_count",
              "limit": max(1, min(n, 100))}
    if language:
        params["language"] = language
    rows = _call(f"{KICK_API}/livestreams?{_q(params)}", head).get("data") or []
    out = [(f"https://kick.com/{r['slug']}", int(r.get("viewer_count") or 0))
           for r in rows if r.get("slug") and _speaks(r, language)]
    return sorted(out, key=lambda x: -x[1])[:n]


FINDERS = {"twitch": twitch_top, "kick": kick_top}


def category_for(cfg: dict, platform: str) -> str:
    """discover_category is one name, or a {twitch: ..., kick: ...} mapping."""
    cat = cfg.get("discover_category")
    if isinstance(cat, dict):
        cat = cat.get(platform)
    return str(cat or "").strip()


def enabled(cfg: dict) -> bool:
    return any(category_for(cfg, p) for p in FINDERS)


def top_streams(cfg: dict, now: float = None, unread: set = None):
    """The biggest live streams across the configured platforms, biggest first,
    as [(channel_url, viewers)]. None when no platform could be read, so the
    caller can keep what it is already watching. When `unread` (a set) is
    given, the platforms that were configured but could not be read this time
    are added to it, so the caller can keep their running streams."""
    now = time.time() if now is None else now
    n = int(cfg.get("discover_top", 10))
    found, ok = [], False
    for plat in cfg.get("discover_platforms") or list(FINDERS):
        cat = category_for(cfg, plat)
        if plat not in FINDERS or not cat:
            continue
        if not _creds(plat):
            log(f"discover: {plat} skipped, {plat.upper()}_CLIENT_ID and "
                f"{plat.upper()}_CLIENT_SECRET are not set")
            if unread is not None:
                unread.add(plat)
            continue
        try:
            lang = str(cfg.get("discover_language") or "").strip().lower()
            rows = FINDERS[plat](cat, n, now, **({"language": lang} if lang else {}))
        except Exception as e:     # a bad reply must not end the rescan loop
            log(f"discover: {plat} failed: {e if isinstance(e, DiscoverError) else type(e).__name__}")
            if unread is not None:
                unread.add(plat)
            continue
        ok = True
        log(f"discover: {plat} {cat!r}: {len(rows)} live stream(s)")
        found += rows
    if not ok:
        return None
    found = sorted(found, key=lambda x: -x[1])
    if cfg.get("discover_skip_simulcast", True):
        # The same channel name live on two platforms is taken to be one
        # person streaming to both: record only the bigger audience.
        seen, kept = set(), []
        for url, viewers in found:
            name = url.rstrip("/").rsplit("/", 1)[-1].lower()
            if name not in seen:
                seen.add(name)
                kept.append((url, viewers))
        if len(kept) < len(found):
            log(f"discover: skipped {len(found) - len(kept)} stream(s) that are "
                "live under the same name on another platform")
        found = kept
    return found[:n]
