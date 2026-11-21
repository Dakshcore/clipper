"""Kick chat parsing, recording quality and the slow-recording check. No network."""
import json

import pytest

from clipper.chat import ChatError, kick_event, kick_pusher
from clipper.ingest import Recorder, stream_spec
from clipper.watch import is_starved


def test_kick_pusher_reads_key_and_cluster():
    reply = {"data": {"connections": [
        {"provider": "centrifugo", "credentials": {"url": "wss://x"}},
        {"provider": "pusher", "credentials": {"app_key": "abc", "cluster": "us2"}}],
        "mode": "websocket"}, "message": "success"}
    assert kick_pusher(reply) == ("abc", "us2")


@pytest.mark.parametrize("reply", [
    {}, {"data": {"connections": []}},
    {"data": {"connections": [{"provider": "centrifugo", "credentials": {"url": "w"}}]}},
    {"data": {"connections": [{"provider": "pusher", "credentials": {"app_key": "abc"}}]}}])
def test_kick_pusher_without_a_pusher_connection(reply):
    with pytest.raises(ChatError, match="no Pusher"):
        kick_pusher(reply)


def _frame(name):
    return json.dumps({"event": name, "data": "{}"})


def test_kick_event_kinds():
    assert kick_event(_frame("pusher_internal:subscription_succeeded")) == "ready"
    assert kick_event(_frame("pusher:ping")) == "ping"
    assert kick_event(_frame("App\\Events\\ChatMessageEvent")) == "message"
    assert kick_event(_frame("pusher:connection_established")) == "other"


def test_kick_event_reports_a_stale_key_instead_of_a_json_error():
    # What Pusher sent for the old hard-coded key, then the empty closing frame.
    err = json.dumps({"event": "pusher:error",
                      "data": {"code": 4001, "message": "App key x not in this cluster."}})
    with pytest.raises(ChatError, match="4001"):
        kick_event(err)
    with pytest.raises(ChatError, match="closed"):
        kick_event("")


def test_stream_spec():
    s720 = "720p60,720p,540p,540p60,480p,480p60,360p,360p60,900p60,900p,936p60,936p,1080p60,1080p,best"
    assert stream_spec("720p") == (s720, "b[height<=720]/b")
    names = stream_spec("480p")[0].split(",")
    # no 480p: take 360p before anything bigger, and "best" only last
    assert names[:4] == ["480p60", "480p", "360p", "360p60"] and names[-1] == "best"
    assert names.index("360p") < names.index("540p") < names.index("720p") < names.index("1080p")
    assert stream_spec("1080")[0].startswith("1080p60,1080p,936p,")
    assert stream_spec("best") == ("best", "b")
    assert stream_spec(None) == ("best", "b")
    assert stream_spec("nonsense") == ("best", "b")


def test_recorder_passes_quality_to_both_tools(tmp_path, monkeypatch):
    import clipper.ingest as ingest
    cmds = []

    class FakeOut:
        def close(self):
            pass

    class FakeProc:
        def __init__(self, cmd, **kw):
            cmds.append(cmd)
            self.stdout = FakeOut()

    monkeypatch.setattr(ingest.subprocess, "Popen", FakeProc)
    rec = Recorder("https://kick.com/x", tmp_path, 60, "720p")
    rec._spawn("streamlink")
    rec._spawn("yt-dlp")
    assert cmds[0][-1] == stream_spec("720p")[0]
    assert cmds[2][cmds[2].index("-f") + 1] == "b[height<=720]/b"


def test_recorder_processes_get_recorder_priority(tmp_path, monkeypatch):
    import clipper.ingest as ingest
    flags = []

    class FakeOut:
        def close(self):
            pass

    class FakeProc:
        def __init__(self, cmd, **kw):
            flags.append(kw.get("creationflags"))
            self.stdout = FakeOut()

    monkeypatch.setattr(ingest.subprocess, "Popen", FakeProc)
    Recorder("https://kick.com/x", tmp_path, 60, "720p")._spawn("streamlink")
    assert flags == [ingest.RECORDER_PRIORITY, ingest.RECORDER_PRIORITY]


def test_is_starved():
    assert not is_starved(0, 170, 60)      # too early to judge
    assert is_starved(0, 200, 60)          # nothing closed after 200 s
    assert not is_starved(2, 200, 60)      # keeping up
    assert is_starved(1, 400, 60)          # 1 closed where about 5 were due
    assert not is_starved(5, 400, 60)
