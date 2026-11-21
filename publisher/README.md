# publisher

Uploads finished clips from `clipper/out/` to YouTube Shorts with the official
YouTube Data API v3. Personal use.

It shares no code with `clipper/`. The only interface is the output folder:
`<clip_id>.mp4` next to `<clip_id>.json` (schema in `clipper/README.md`).
The publisher only reads that folder.

## Commands

Run from the repo root.

```
publisher/.venv/Scripts/python -m publisher once     # one pass over the queue
publisher/.venv/Scripts/python -m publisher run      # keep polling (poll_seconds)
publisher/.venv/Scripts/python -m publisher status   # per-clip state and today's count
publisher/.venv/Scripts/python -m pytest publisher/tests -q
```

To keep it running outside a Claude session, double-click `publisher/run.cmd`
or run it in your own terminal. It runs `publisher run` until Ctrl+C. It does
not start at login; nothing was added to Windows for that.

Setup from scratch: `python -m venv publisher/.venv`, then
`publisher/.venv/Scripts/python -m pip install -r publisher/requirements.txt`.

## Config (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | Only print what would be uploaded. Nothing leaves the machine. |
| `privacy_status` | `private` | `private`, `unlisted` or `public`. Changing it is your edit. |
| `min_score` | `0.5` | Clips below this are marked `skipped`. |
| `max_uploads_per_day` | `5` | Daily cap, counted per Pacific-time day. |
| `min_gap_minutes` | `60` | Minimum time between two uploads. |
| `retry_failed` | `false` | Failed uploads stay failed until you set this. |
| `youtube.api_daily_upload_limit` | `100` | The cap can never exceed this. |
| `skip_overlapping` | `true` | Upload each moment once. See below. |
| `overlap_fraction` | `0.5` | How much of the shorter clip's transcript must repeat to count as the same moment. |

Same-moment check: two clips with the same `source_url` are the same moment
when their transcripts share one unbroken run of words at least
`overlap_fraction` of the shorter transcript. Transcripts under 8 words are
never matched. A clip already uploaded (or left failed) always wins. Among
waiting clips the higher score wins, then the older one. The loser is marked
`skipped` with reason `same moment as <clip_id>`. Times are not compared,
because live sessions restart their clock.

Quota facts, from Google's YouTube Data API "Quota Calculator" page (last
updated 2026-11-12): `videos.insert` has its own quota bucket with a default
of 100 calls per day at 1 unit per call; other endpoints share 10,000 units
per day; `videos.list` costs 1 unit; daily quotas reset at midnight Pacific
Time. Check your own project's quota page, since Google can set a different
value per project.

## State (`state.json`)

One status per clip per destination: `pending`, `uploaded`, `failed`, `skipped`.

```json
{"version": 1,
 "clips": {"<clip_id>": {"youtube": {"status": "uploaded", "remote_id": "<video id>",
                                     "uploaded_at": 1800000000.0, "privacy_status": "private"}}}}
```

- A clip marked `uploaded` is never uploaded again, also after a restart.
- Before each upload call the clip is marked `failed: interrupted during
  upload`. Success replaces that with `uploaded`. If the process dies in the
  middle, the clip stays failed and is not retried until you check the channel.
- Dry runs record `pending` and `skipped` only.

## Adding a destination

Subclass `adapters/base.py:Adapter` with `upload(clip, metadata) -> id` and
`status(id) -> dict`, register it in `__main__.py:make_publisher`, and add its
name to `destinations`. Only YouTube exists now.

## Owner setup for real uploads (one time)

Done on 2026-11-21. Claude did steps 1 to 6 in the owner's signed-in browser
(project `clip-publisher`, consent screen in Testing status with the owner's
account as the only test user, a Desktop app client, keys in `publisher/.env`).
The owner approved Google's consent page in step 7.

Verified by a live run that day: `once` gave `uploaded 1, would_upload 0,
skipped 9, failed 0, waiting 13`. The uploaded video is id `xJYKjzYqTG4`, and
`status()` returned `state: uploaded, privacy: private, rejection: None`.
Later that day the owner chose to go live: `config.yaml` has `dry_run: false`
and `privacy_status: private`, and `run` is uploading one clip an hour. Not
verified: a token refresh, a real quota or sign-in refusal from Google, and
any upload other than private.

### When Google refuses (fixed 2026-11-21)

Before the fix, one expired sign-in or quota error marked every waiting clip
`failed` in a single pass. Now:

- Sign-in happens before any clip is touched. If it fails, no clip changes
  state. An expired sign-in stops `run` with a message saying what to delete.
- A refusal that comes before the upload starts (HTTP 401, `quotaExceeded`,
  `uploadLimitExceeded` and similar) leaves the clip `pending` and stops `run`.
- Any other upload error marks that one clip `failed` and ends the pass. The
  next pass carries on with the next clip.

Checked by tests only (`pytest publisher/tests -q`: 16 passed), with fake
adapters. The Google error paths have not been seen live.

The steps, for doing it again:

1. Go to https://console.cloud.google.com and create a project.
2. APIs and Services -> Library -> enable "YouTube Data API v3".
3. APIs and Services -> OAuth consent screen: user type External; fill the app
   name and your email; add your own Google account under Test users.
4. APIs and Services -> Credentials -> Create credentials -> OAuth client ID ->
   Application type "Desktop app". Copy the client ID and client secret.
5. Copy `publisher/.env.example` to `publisher/.env` and fill in
   `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`. `.env` is git-ignored.
6. In `config.yaml` set `dry_run: false`. Leave `privacy_status: private`.
7. Run `publisher/.venv/Scripts/python -m publisher once`. Your browser opens
   Google's consent page. Sign in to the channel's account and approve. The
   token is saved to `publisher/.secrets/youtube_token.json` (git-ignored) and
   is never printed.

Notes:
- While the consent screen is in "Testing" status Google can expire the saved
  sign-in. If an upload fails with an auth error, delete
  `publisher/.secrets/youtube_token.json` and run `once` again to re-approve.
- If `privacyStatus` is left unset the API defaults to public, so the
  publisher always sends the value from `config.yaml`.
- YouTube decides what counts as a Short from the video itself (vertical and
  short). There is no Shorts flag in the API.

## Verified / not verified

Verified on 2026-11-17:
- `pytest publisher/tests -q`: 7 passed (no double upload after restart,
  interrupted upload not retried, daily cap, cap clamp to API limit, minimum
  gap, minimum score, dry run).
- `python -m publisher once` with `dry_run: true` against real clips in
  `clipper/out/`: printed 1 would-upload, 1 skipped (score 0.4709 below 0.5),
  9 waiting on the 60-minute gap.

Verified on 2026-11-20:
- `pytest publisher/tests -q`: 12 passed (the 7 above plus 5 for the
  same-moment check).
- `python -m publisher once` with `dry_run: true` against 19 real clips in
  `clipper/out/`: 1 would-upload, 6 skipped, 12 waiting. Of the 6 skipped,
  4 were `same moment as youtube_EJXTZ5jpSmk_000005_bd6a41` and 2 were below
  `min_score`.

Verified on 2026-11-21: OAuth consent, the real `videos.insert` call and
`status()`, see "Owner setup" above.

Review fixes on 2026-11-21 (`pytest publisher/tests -q`: 22 passed):
- One publisher at a time. `run` and `once` hold an OS lock on
  `publisher/state.lock`; a second one prints "Another publisher is already
  running" and exits with code 3. Checked live: `once` was refused while
  `run` was going. `status` needs no lock.
- An upload that was interrupted is never retried, also with
  `retry_failed: true`. Check the channel, then edit `state.json` by hand.
- A clip JSON with a non-text hashtag or a score that is not a finite number
  is reported as `ignored` and no longer crashes the pass.
- Titles and descriptions lose `<` and `>`; an empty title becomes `Clip`;
  the description is cut to 5000 bytes; empty, repeated and over-long tags
  are dropped (tags are kept under 400 characters in total).
- The best-scoring waiting clip uploads first; equal scores go oldest first.
- A failed attempt counts toward `max_uploads_per_day` for that day.
- `state.json` is flushed to disk before it replaces the old file, and the
  token file is replaced in one step.

Not fixed:
- A network error during an upload still marks that clip `failed` for good.
- The state records the privacy that was asked for; `status()` is not called
  after an upload to confirm what YouTube applied.
- Token refresh over a long run is not verified.

Network errors during an upload (2026-11-21, `pytest publisher/tests -q`: 23 passed):
- A dropped connection, a timeout or a 5xx reply no longer fails the clip at
  once. `finish_upload` in `adapters/youtube.py` waits 5, 15, 45, 120 and 300 s
  and resumes the same resumable upload each time. The Google client first
  asks how many bytes arrived, so a resume cannot create a second video.
- If the outage outlasts all five waits the clip is marked `failed` as
  before and is not retried.
- Checked by a unit test with a fake request only. No real upload has been
  interrupted to test it. A publisher that was already running keeps the old
  code until it is restarted.
