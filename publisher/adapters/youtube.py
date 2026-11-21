"""YouTube Shorts via the official YouTube Data API v3 (videos.insert, OAuth).

YouTube has no separate Shorts endpoint: a short vertical video uploaded with
videos.insert is classified as a Short by YouTube itself. No browser
automation is used. The only browser step is Google's own consent page,
which the owner completes."""
import json
import os
import time
from pathlib import Path

from ..config import SECRETS
from .base import Adapter, UploadBlocked

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILE = SECRETS / "youtube_token.json"

SETUP = """YouTube is not set up yet. One-time steps (you do these, in your own browser):
  1. Google Cloud console -> create a project -> enable "YouTube Data API v3".
  2. OAuth consent screen: user type External, add your Google account as a test user.
  3. Credentials -> Create credentials -> OAuth client ID -> type "Desktop app".
  4. Put the client id and secret in publisher/.env as YOUTUBE_CLIENT_ID and
     YOUTUBE_CLIENT_SECRET (see .env.example).
  5. Run `python -m publisher once` again. Your browser opens Google's consent
     page; sign in and approve. The token is saved in publisher/.secrets/."""


class SetupNeeded(RuntimeError):
    stop_run = True


EXPIRED = ("Google sign-in has expired or was revoked. Delete "
           "publisher/.secrets/youtube_token.json and run `python -m publisher once` "
           "to approve again. No clip was marked failed.")

# Google refuses these before a video is created.
BLOCKED_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                   "rateLimitExceeded", "userRateLimitExceeded", "authError",
                   "youtubeSignupRequired", "forbidden"}


def http_reason(content) -> str:
    """The `reason` of a Google API error body, or "" when it has none."""
    try:
        data = json.loads(content)
        return str(data["error"]["errors"][0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def is_blocked(status: int, reason: str) -> bool:
    return status == 401 or reason in BLOCKED_REASONS


RETRY_WAITS = (5, 15, 45, 120, 300)   # seconds between tries after a network error


def is_transient(e) -> bool:
    """A dropped connection, a timeout or a 5xx from Google: worth resuming."""
    status = getattr(getattr(e, "resp", None), "status", None)
    if status is not None:
        return int(status) >= 500
    return isinstance(e, OSError) or type(e).__module__.startswith("httplib2")


def finish_upload(req, waits=RETRY_WAITS, sleep=time.sleep, say=print):
    """Send a resumable upload to the end and return Google's reply.
    After a network error the same upload is resumed: the next `next_chunk`
    first asks Google how many bytes arrived (or whether the video is already
    complete), so a retry can never create a second video."""
    left = list(waits)
    while True:
        try:
            _, resp = req.next_chunk(num_retries=3)
        except Exception as e:
            if not is_transient(e) or not left:
                raise
            wait = left.pop(0)
            say(f"[youtube] network error during upload ({type(e).__name__}); "
                f"resuming in {wait}s")
            sleep(wait)
            continue
        if resp is not None:
            return resp


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise SetupNeeded(EXPIRED) from e
    else:
        cid, secret = os.environ.get("YOUTUBE_CLIENT_ID"), os.environ.get("YOUTUBE_CLIENT_SECRET")
        if not cid or not secret:
            raise SetupNeeded(SETUP)
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(
            {"installed": {"client_id": cid, "client_secret": secret,
                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                           "token_uri": "https://oauth2.googleapis.com/token",
                           "redirect_uris": ["http://localhost"]}}, SCOPES)
        print("Opening Google's consent page in your browser. Sign in and approve there.")
        creds = flow.run_local_server(port=0, authorization_prompt_message="",
                                      success_message="Done. You can close this tab.")
    SECRETS.mkdir(exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")   # never printed
    os.replace(tmp, TOKEN_FILE)   # a crash mid-write must not corrupt the token
    return creds


class YouTubeAdapter(Adapter):
    name = "youtube"

    def __init__(self, cfg: dict):
        creds = get_credentials()
        from googleapiclient.discovery import build
        self.opts = cfg["youtube"]
        self.api = build("youtube", "v3", credentials=creds, cache_discovery=False)

    def upload(self, clip: Path, metadata: dict) -> str:
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        body = {
            "snippet": {"title": metadata["title"], "description": metadata["description"],
                        "tags": metadata["tags"], "categoryId": str(self.opts["category_id"])},
            "status": {"privacyStatus": metadata["privacy_status"],
                       "selfDeclaredMadeForKids": bool(self.opts["made_for_kids"])},
        }
        media = MediaFileUpload(str(clip), mimetype="video/mp4", resumable=True,
                                chunksize=8 * 1024 * 1024)
        req = self.api.videos().insert(
            part="snippet,status", body=body, media_body=media,
            notifySubscribers=bool(self.opts["notify_subscribers"]))
        try:
            resp = finish_upload(req)
        except RefreshError as e:
            raise UploadBlocked(EXPIRED) from e
        except HttpError as e:
            reason = http_reason(e.content)
            if is_blocked(e.resp.status, reason):
                raise UploadBlocked(f"YouTube refused the upload (HTTP {e.resp.status} "
                                    f"{reason}). The clip stays pending.") from e
            raise
        return resp["id"]

    def status(self, id: str) -> dict:
        r = self.api.videos().list(part="status", id=id).execute()
        if not r.get("items"):
            return {"state": "not found"}
        s = r["items"][0]["status"]
        return {"state": s.get("uploadStatus"), "privacy": s.get("privacyStatus"),
                "rejection": s.get("rejectionReason")}
