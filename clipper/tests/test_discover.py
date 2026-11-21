"""Top-stream discovery and the rescan logic. No network is used."""
import io
import json
import urllib.error
import urllib.parse

import pytest

from clipper import discover
from clipper.watch import pick_hottest, plan_changes


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(discover, "_tokens", {})
    monkeypatch.setattr(discover, "_categories", {})
    monkeypatch.setattr(discover, "log", lambda *_: None)
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
              "KICK_CLIENT_ID", "KICK_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)


def server(monkeypatch, routes):
    """routes: {url prefix: reply dict or Exception}. Records every request."""
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        for prefix, reply in routes.items():
            if req.full_url.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return FakeReply(json.dumps(reply).encode("utf-8"))
        raise AssertionError("unexpected request " + req.full_url)

    monkeypatch.setattr(discover.urllib.request, "urlopen", urlopen)
    return seen


TWITCH = {
    discover.TWITCH_TOKEN: {"access_token": "tw-token", "expires_in": 5000},
    discover.TWITCH_API + "/games": {"data": [{"id": "509658", "name": "Just Chatting"}]},
    discover.TWITCH_API + "/streams": {"data": [
        {"user_login": "big", "viewer_count": 900},
        {"user_login": "mid", "viewer_count": 500},
        {"user_login": "small", "viewer_count": 100}]},
}
KICK = {
    discover.KICK_TOKEN: {"access_token": "kick-token", "expires_in": 5000},
    discover.KICK_API + "/categories": {"data": [
        {"id": 15, "name": "Just Chatting"}, {"id": 16, "name": "Just Chatting 2"}]},
    discover.KICK_API + "/livestreams": {"data": [
        {"slug": "k-low", "viewer_count": 50},
        {"slug": "k-top", "viewer_count": 700}]},
}
CFG = {"discover_category": "Just Chatting", "discover_platforms": ["twitch", "kick"],
       "discover_top": 3}


def both_keys(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "tw-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "tw-secret")
    monkeypatch.setenv("KICK_CLIENT_ID", "k-id")
    monkeypatch.setenv("KICK_CLIENT_SECRET", "k-secret")


def test_top_streams_merges_platforms_biggest_first(monkeypatch):
    both_keys(monkeypatch)
    seen = server(monkeypatch, {**TWITCH, **KICK})
    assert discover.top_streams(CFG, now=1000.0) == [
        ("https://www.twitch.tv/big", 900),
        ("https://kick.com/k-top", 700),
        ("https://www.twitch.tv/mid", 500)]
    by_url = {r.full_url.split("?")[0]: r for r in seen}
    streams = by_url[discover.TWITCH_API + "/streams"]
    assert streams.get_header("Client-id") == "tw-id"
    assert streams.get_header("Authorization") == "Bearer tw-token"
    assert "game_id=509658" in streams.full_url
    live = by_url[discover.KICK_API + "/livestreams"]
    assert "category_id=15" in live.full_url and "sort=viewer_count" in live.full_url
    form = urllib.parse.parse_qs(by_url[discover.TWITCH_TOKEN].data.decode())
    assert form == {"client_id": ["tw-id"], "client_secret": ["tw-secret"],
                    "grant_type": ["client_credentials"]}


def test_token_and_category_are_reused(monkeypatch):
    both_keys(monkeypatch)
    seen = server(monkeypatch, {**TWITCH, **KICK})
    discover.top_streams(CFG, now=1000.0)
    discover.top_streams(CFG, now=2800.0)
    urls = [r.full_url.split("?")[0] for r in seen]
    assert urls.count(discover.TWITCH_TOKEN) == 1
    assert urls.count(discover.TWITCH_API + "/games") == 1
    assert urls.count(discover.TWITCH_API + "/streams") == 2


def test_expired_token_is_fetched_again(monkeypatch):
    both_keys(monkeypatch)
    seen = server(monkeypatch, {**TWITCH, **KICK})
    discover.top_streams(CFG, now=1000.0)
    discover.top_streams(CFG, now=1000.0 + 5000)
    assert [r.full_url for r in seen].count(discover.TWITCH_TOKEN) == 2


def test_platform_without_keys_is_skipped(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "tw-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "tw-secret")
    seen = server(monkeypatch, TWITCH)
    out = discover.top_streams(CFG, now=1000.0)
    assert [u for u, _ in out] == ["https://www.twitch.tv/big", "https://www.twitch.tv/mid",
                                  "https://www.twitch.tv/small"]
    assert all("kick" not in r.full_url for r in seen)


def test_no_keys_at_all_gives_none(monkeypatch):
    server(monkeypatch, {})
    assert discover.top_streams(CFG, now=1000.0) is None


def test_one_platform_failing_keeps_the_other(monkeypatch):
    both_keys(monkeypatch)
    err = urllib.error.HTTPError("u", 500, "boom", {}, None)
    server(monkeypatch, {**TWITCH, **KICK, discover.TWITCH_API + "/streams": err})
    assert discover.top_streams(CFG, now=1000.0) == [
        ("https://kick.com/k-top", 700), ("https://kick.com/k-low", 50)]


def test_every_platform_failing_gives_none(monkeypatch):
    both_keys(monkeypatch)
    err = urllib.error.HTTPError("u", 401, "no", {}, None)
    server(monkeypatch, {discover.TWITCH_TOKEN: err, discover.KICK_TOKEN: err})
    assert discover.top_streams(CFG, now=1000.0) is None


def test_unknown_category_fails_that_platform(monkeypatch):
    both_keys(monkeypatch)
    server(monkeypatch, {**TWITCH, **KICK, discover.TWITCH_API + "/games": {"data": []}})
    out = discover.top_streams(CFG, now=1000.0)
    assert [u for u, _ in out] == ["https://kick.com/k-top", "https://kick.com/k-low"]


def test_kick_needs_an_exact_category_name(monkeypatch):
    both_keys(monkeypatch)
    server(monkeypatch, {**KICK, discover.KICK_API + "/categories": {
        "data": [{"id": 16, "name": "Just Chatting 2"}]}})
    with pytest.raises(discover.DiscoverError, match="Just Chatting 2"):
        discover.kick_top("Just Chatting", 3, 1000.0)


def test_category_per_platform_and_off_switch():
    cfg = {"discover_category": {"twitch": "VALORANT"}}
    assert discover.category_for(cfg, "twitch") == "VALORANT"
    assert discover.category_for(cfg, "kick") == ""
    assert discover.enabled(cfg)
    assert not discover.enabled({"discover_category": None})
    assert not discover.enabled({})


def test_plan_changes():
    watching = {"https://www.twitch.tv/a", "https://www.twitch.tv/b"}
    top = ["https://www.twitch.tv/B", "https://kick.com/c/", "https://www.twitch.tv/mine"]
    fixed = {"https://www.twitch.tv/mine"}
    add, drop = plan_changes(watching, top, fixed)
    assert add == ["https://kick.com/c"]            # b is kept, mine is already listed
    assert drop == ["https://www.twitch.tv/a"]
    assert plan_changes(watching, [], fixed) == ([], sorted(watching))


def test_pick_hottest_prefers_chat_then_loudness_then_newest():
    cands = [(3, "quiet", 180.0, 240.0), (4, "loud", 240.0, 300.0), (5, "flat", 300.0, 360.0)]
    levels = {"quiet": [-40.0] * 60, "loud": [-40.0] * 50 + [-10.0] * 10,
              "flat": [-40.0] * 60}

    def loud(path):
        import numpy as np
        return np.array(levels[path], dtype=np.float32)

    assert pick_hottest(cands, [], loud) == 4                       # loudness spike
    assert pick_hottest(cands, [190.0, 200.0, 250.0], loud) == 3    # most chat
    assert pick_hottest([cands[0], cands[2]], [], loud) == 5        # tie: newest
