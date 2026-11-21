"""Chat message rate for live streams, read anonymously where the platform
allows it. Each collector records one timestamp per chat message, in seconds
since the recording started. Nothing is posted and no account is used."""
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from .platforms import channel_name, live_url
from .util import log

KICK_SITE = "https://kick.com/api/v2/channels/"
KICK_REALTIME = "https://web.kick.com/api/v1/realtime/channels/{}/chat/connection"
AGENT = "clipper-chat/1.0"


class ChatError(Exception):
    pass


class ChatCollector:
    def __init__(self, url: str, t0: float, work_dir: Path):
        self.url, self.t0, self.dir = url, t0, work_dir
        self.times = []
        self.readable = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._safe_run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def rebase(self, t0: float) -> None:
        """Move the time origin (the recorder learns its first-data time late)
        and shift the timestamps already taken."""
        d = t0 - self.t0
        if abs(d) < 0.5:
            return
        self.t0 = t0
        n = len(self.times)
        self.times[:n] = [t - d for t in self.times[:n]]

    def _mark(self, n: int = 1):
        self.readable = True
        self.times.extend([time.time() - self.t0] * n)

    RECONNECT = True      # reconnect after a dropped connection, until stopped
    BACKOFF_FIRST = 5     # seconds before the first reconnect; doubles up to BACKOFF_MAX
    BACKOFF_MAX = 60

    def _safe_run(self):
        """Run the collector until stopped. `readable` is true only while a
        connection is up. Chat is optional; nothing here may take the recorder down."""
        delay = self.BACKOFF_FIRST
        while not self._stop.is_set():
            began = time.time()
            try:
                self.run()
            except Exception as e:
                if not self._stop.is_set():   # a reset during shutdown is expected
                    log(f"chat: connection lost ({type(e).__name__}: {str(e)[:100]})")
            if self.RECONNECT:
                self.readable = False
            if not self.RECONNECT or self._stop.is_set():
                break
            if time.time() - began > 60:
                delay = self.BACKOFF_FIRST    # it had been working; start over
            log(f"chat: reconnecting in {delay:.0f}s")
            self._stop.wait(delay)
            delay = min(delay * 2, self.BACKOFF_MAX)

    def run(self):
        raise NotImplementedError


class TwitchChat(ChatCollector):
    def run(self):
        chan = channel_name(self.url).lower()
        s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=20)
        try:
            s.sendall(f"NICK justinfan{int(time.time()) % 90000 + 10000}\r\nJOIN #{chan}\r\n".encode())
            s.settimeout(5)
            buf = b""
            while not self._stop.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for line in lines:
                    if line.startswith(b"PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif b" JOIN #" in line or b" 366 " in line:
                        self.readable = True
                    elif b" PRIVMSG #" in line:
                        self._mark()
        finally:
            try:
                s.close()
            except OSError:
                pass


def _kick_json(url: str, body: dict = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": AGENT, "Accept": "application/json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def kick_pusher(reply: dict) -> tuple:
    """(app_key, cluster) from Kick's chat connection reply. Kick changes the
    key, so it is asked for each session and never kept in the code."""
    for c in (reply.get("data") or {}).get("connections") or []:
        cred = c.get("credentials") or {}
        if c.get("provider") == "pusher" and cred.get("app_key") and cred.get("cluster"):
            return str(cred["app_key"]), str(cred["cluster"])
    raise ChatError("Kick offered no Pusher chat connection")


def kick_event(raw: str) -> str:
    """What one websocket frame means: ready | ping | message | other.
    Raises ChatError when Pusher reports an error or the frame is not JSON."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        raise ChatError("Kick chat closed the connection")
    ev = str(msg.get("event", ""))
    if ev == "pusher:error":
        data = msg.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}
        raise ChatError(f"Pusher error {(data or {}).get('code')}")
    if ev == "pusher_internal:subscription_succeeded":
        return "ready"
    if ev == "pusher:ping":
        return "ping"
    if ev.endswith("ChatMessageEvent"):
        return "message"
    return "other"


class KickChat(ChatCollector):
    def run(self):
        import websocket
        chan = _kick_json(KICK_SITE + channel_name(self.url))
        room = chan["chatroom"]["id"]
        key, cluster = kick_pusher(_kick_json(
            KICK_REALTIME.format(chan["id"]),
            {"client": {"id": str(uuid.uuid4()), "type": "web"},
             "capabilities": {"accepted_providers": [{"provider": "pusher"}]}}))
        ws = websocket.create_connection(
            f"wss://ws-{cluster}.pusher.com/app/{key}"
            "?protocol=7&client=js&version=8.4.0&flash=false", timeout=20)
        ws.send(json.dumps({"event": "pusher:subscribe",
                            "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
        ws.settimeout(5)
        try:
            while not self._stop.is_set():
                try:
                    kind = kick_event(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if kind == "ready":
                    self.readable = True
                elif kind == "ping":
                    ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif kind == "message":
                    self._mark()
        finally:
            try:
                ws.close()
            except Exception:
                pass


class YouTubeChat(ChatCollector):
    RECONNECT = False     # the download ends with the stream
    def run(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download",
             "--write-subs", "--sub-langs", "live_chat", "-o", "chat", live_url(self.url)],
            cwd=str(self.dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seen = 0
        try:
            while not self._stop.is_set() and p.poll() is None:
                time.sleep(2)
                files = list(self.dir.glob("chat.live_chat.json*"))
                if not files:
                    continue
                with open(files[0], "rb") as f:
                    n = sum(1 for _ in f)
                if n > seen:
                    self._mark(n - seen)
                    seen = n
                elif seen > 0:     # a file with no messages is not readable chat
                    self.readable = True
        finally:
            if p.poll() is None:
                p.terminate()


def start_chat(platform: str, url: str, t0: float, work_dir: Path) -> ChatCollector:
    cls = {"twitch": TwitchChat, "kick": KickChat, "youtube": YouTubeChat}[platform]
    return cls(url, t0, work_dir).start()
