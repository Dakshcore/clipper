# clipper

Turns your own videos and live streams (YouTube, Twitch, Kick) into vertical
short clips with burned-in captions. Personal use. Sources are limited to the
URLs in `config.yaml`, which is meant for channels you run or have permission
to clip.

No web UI, no database, no accounts, no uploading. Uploading is a separate
project (`publisher/`) that only reads `clipper/out/`.

## Commands

Run from the repo root.

```
clipper/.venv/Scripts/python -m clipper video <url>      # clip a finished video (URL must be in config.yaml)
clipper/.venv/Scripts/python -m clipper watch            # poll the channels in config.yaml, clip while live
clipper/.venv/Scripts/python -m clipper watch --minutes 5
clipper/.venv/Scripts/python -m clipper list             # list clips in clipper/out
clipper/.venv/Scripts/python -m clipper --config other.yaml video <url>
clipper/.venv/Scripts/python -m pytest clipper/tests -q
```

Setup from scratch: `python -m venv clipper/.venv`, then
`clipper/.venv/Scripts/python -m pip install -r clipper/requirements.txt`.
ffmpeg must be on PATH. The two `nvidia-*` packages are only needed for GPU
transcription; without a CUDA GPU the clipper falls back to CPU.

No API key is needed by default. The text rating uses a local model served
by Ollama (`llm_backend: local`). Install Ollama, then run
`ollama pull llama3.2`. If Ollama or the model is not there, the clipper
falls back to a built-in text heuristic and says so in its log.

To use Claude instead, set `llm_backend: claude`, copy `.env.example` to
`.env` (git-ignored) and set `ANTHROPIC_API_KEY`.

## Pipeline

1. **Ingest** (`ingest.py`). A video URL is downloaded with yt-dlp. A channel
   URL is polled every `poll_seconds`; when live it is recorded with
   streamlink (yt-dlp as fallback) into rolling `segment_seconds` segments in
   `clipper/work/<source>/<session>/`.
2. **Find moments** (`transcribe.py`, `moments.py`, `signals.py`, `chat.py`,
   `scoring.py`, `llm.py`). faster-whisper gives word timestamps. Every
   15-60 s window that starts and ends on a sentence boundary is scored from
   loudness spikes, chat rate (when chat is readable) and a text rating.
   Live streams are processed as each segment closes, over a two-segment
   sliding window.
3. **Edit** (`edit.py`). ffmpeg cuts the window, removes pauses longer than
   `dead_air_seconds`, reframes to 1080x1920, burns word-by-word captions and
   normalises to `loudness_lufs` (two-pass loudnorm).
   Layouts: `stack` (facecam over gameplay) when a small, off-centre, steady
   face is found or `facecam_region` is set; `face` (crop follows the
   speaker) when a face is found; `center` otherwise.
   Caption styles: `group` (three words, spoken word highlighted) or `word`
   (one word at a time).
4. **Output** (`output.py`). `clipper/out/<clip_id>.mp4` and
   `clipper/out/<clip_id>.json`. The JSON is written last, so a JSON file
   always has a finished video next to it.

Disk cap: raw files in `clipper/work/` that are recorded as processed are
deleted once older than `raw_retention_hours` (default 2), and, when `work/`
is over `raw_max_gb` (default 20), the oldest processed ones go first.
Unprocessed and open segments are never deleted. Nothing outside
`clipper/work/` is ever deleted.

## Sidecar schema (version 1)

`clipper/out/<clip_id>.json`. Stable: fields are not renamed or removed
without raising `schema_version`. Exactly these fields are present.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | `1` |
| `clip_id` | string | `<platform>_<source tail>_<start seconds, 6 digits>_<6 hex>`; also the file stem |
| `video_file` | string | `<clip_id>.mp4`, in the same folder |
| `created_at` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `duration` | number | seconds of the rendered clip, from ffprobe |
| `source_url` | string | the URL from `config.yaml` |
| `platform` | string | `youtube`, `twitch` or `kick` |
| `start_time` | number | seconds from the start of the video, or of the recording session for live |
| `end_time` | number | same clock as `start_time`; greater than it |
| `score` | number | 0 to 1 |
| `transcript` | string | words spoken in the clip |
| `title` | string | 1 to 80 characters |
| `description` | string | |
| `hashtags` | list of strings | 3 to 5, each starting with `#` |

`duration` can be shorter than `end_time - start_time` because dead air is cut.

Example (a real clip from the live test):

```json
{
  "schema_version": 1,
  "clip_id": "kick_sam_000160_d85231",
  "video_file": "kick_sam_000160_d85231.mp4",
  "created_at": "2026-11-17T14:43:17Z",
  "duration": 15.1,
  "source_url": "https://kick.com/sam",
  "platform": "kick",
  "start_time": 160.17,
  "end_time": 179.01,
  "score": 0.7576,
  "transcript": "...",
  "title": "...",
  "description": "...",
  "hashtags": ["#shorts", "#kick", "#clips"]
}
```

`output.py:validate_sidecar(d)` returns a list of problems (empty when valid).

## Swapping the scorer

The moment score comes from one function:

```python
def score_window(window: Window, signals: Signals) -> float  # 0..1
```

- `window` (`moments.py:Window`): `start`, `end`, `text`, `words`.
- `signals` (`scoring.py:Signals`): `loudness` (0..1), `chat_rate` (0..1 or
  `None` when chat is not readable), `llm_model` (model id, or `None` for
  windows that should not call the LLM), `weights`.

To replace it, write your own function with that signature and set
`scorer: your_module:your_function` in `config.yaml`.

The built-in scorer asks an LLM for hook strength, standalone-ness, a title,
a description and hashtags, for the best `llm_max_candidates` windows only.
`llm_backend` in `config.yaml` picks the model:

| `llm_backend` | What runs | Needs |
|---|---|---|
| `local` (default) | `local_model` (default `llama3.2`) through Ollama at `ollama_url` | Ollama running, model pulled. No key, no network. |
| `claude` | `llm_model` (default `claude-sonnet-5-5`) through the official `anthropic` SDK | `ANTHROPIC_API_KEY` |
| `venice` | `venice_model` (default `zai-org-glm-5`) through the OpenAI-compatible Venice API, standard library only | `VENICE_API_KEY` |
| `none` | text heuristic only | nothing |

With `llm_backend: venice` and `llm_pick_moments: true` (the default), the
model does not rate fixed windows. It gets the whole transcript in one call,
as numbered sentences with times, and returns the moments it would clip as
sentence ranges, each with scores, title, description and hashtags
(`llm.py:pick_moments`). A moment longer than `max_clip_seconds` is cut back
and one shorter than `min_clip_seconds` is extended to whole sentences
(`moments.py:windows_from_picks`); adjusted moments are rated again on their
final text. Loudness and chat still count in the final score. If the call
fails, the reply is not valid JSON, or the transcript is over 400,000
characters, the run falls back to rating the best `llm_max_candidates`
windows one by one.

The local backend sends one `POST /api/chat` per window with a JSON schema as
`format`, temperature 0, and `think: false` (dropped for models that reject
it). It uses only the Python standard library. Small models get an extra
scoring guide in the prompt (`llm.py:LOCAL_GUIDE`), because without it
`llama3.2` gave hook 8 to every sample. Any model from `ollama list` can be
set as `local_model`.

The Claude request opts in to server-side refusal fallbacks; if the installed
SDK rejects that option it sends a plain request.

## Discovery: the biggest live streams in a category

Set `discover_category` in `config.yaml` (for example `Just Chatting`) and
`watch` also finds and clips the biggest live streams in that category on
Twitch and Kick (`discover.py`).

- At start and every `discover_rescan_minutes` (default 30) it asks each
  platform for its live streams in the category, merges both lists and keeps
  the `discover_top` (default 10) with the most viewers.
- Streams new to the list are started. Streams that dropped out are stopped.
  Streams still on the list keep recording without a restart. Channels in
  `sources` are never stopped.
- If no platform can be read, the current channels are kept.
- Both use the official API with an app token. Twitch: `GET /helix/games`,
  then `GET /helix/streams`. Kick: `GET /public/v1/categories`, then
  `GET /public/v1/livestreams` with `sort=viewer_count`. The Kick category
  name must match exactly; the log lists the closest names if it does not.
- Keys go in `clipper/.env`: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`,
  `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`. A platform without keys is skipped.

Channels found this way are not checked for permission. Clipping and
re-uploading someone else's stream is the owner's decision and risk.

Load: one transcription or render runs at a time, so ten streams cannot all
be processed in full. With `live_skip_when_behind: true` (the default), a
stream that has several segments waiting processes only the busiest one, by
chat messages first, then loudness spike, and skips the rest.

Verified on 2026-11-21: `pytest clipper/tests -q`, 55 passed, with fake
servers for both platforms. With `discover_category` set and no keys, `watch`
logged both platforms as skipped and kept running.

Live run on 2026-11-21 with real keys, `discover_category: Just Chatting`,
`watch --minutes 6`:

- Both APIs answered: 10 live streams each. The merged top 10 was 7 Kick and
  3 Twitch channels, 21,367 down to 4,092 viewers. All 10 were started.
- 4 clips were made (3 Twitch, 1 Kick), 21.7 s to 29.3 s, scores 0.460 to 0.560.
- Kick returned HTTP 403 to Python's default user agent. `discover.py` now
  sends `clipper-discovery/1.0`.
- 5 of the 7 Kick streams wrote data to `seg_00000.ts` but never closed a
  segment in 6 minutes, so nothing from them was processed. Fixed later that
  day, see "Kick fixes" below.
- Kick chat failed on all 7 Kick streams (`JSONDecodeError`). Twitch chat was
  readable on all 3. Fixed later that day, see "Kick fixes" below.
- Lag behind live at each processing step ran from 54 s to 263 s.
- The "several segments waiting" skip never triggered in this run.

Not verified: a rescan after 30 minutes on real streams (the run was 6
minutes), and a run long enough for the lag to settle.

### Kick fixes (2026-11-21)

Chat. The cause was not the channel API reply. The Pusher app key in the code
was out of date: Pusher answered error 4001 ("App key not in this cluster"),
closed the socket, and the empty frame raised `JSONDecodeError`. Kick's site
now asks `web.kick.com/api/v1/realtime/channels/<id>/chat/connection` for the
key and cluster, so `chat.py` does the same at the start of each session and
keeps no key in the code. This is Kick's own web endpoint, not the documented
public API, so it can change again. A Pusher error is now logged as such.
Checked on one live channel: readable, 8 messages in 30 s.

Recording. Each Kick stream alone recorded at live speed (48.2 MB in 45 s with
streamlink). Seven at once at `best` (1080p60) got 3 to 8 MB each in 40 s, and
the same seven at 720p got 8 to 17 MB. The cause of the slowdown (this
connection or Kick's servers) was not measured. Two changes:

- `live_quality` in `config.yaml` sets the recorded quality. The code default
  is 720p; `config.yaml` is set to 480p.
- The recorder waits 90 s (was 30 s) for streamlink's first data before it
  falls back to yt-dlp.

`watch` also logs once per stream when a recording has closed fewer than half
the segments that live speed would give.

Two 6 minute runs of the top 10 `Just Chatting` streams after the fix:

| | 720p | 480p |
|---|---|---|
| Streams (Kick, Twitch) | 5, 5 | 6, 4 |
| Recorded with streamlink, no fallback | 10 of 10 | 10 of 10 |
| Chat readable | 10 of 10 | 10 of 10 |
| Segments closed per Kick stream | 1 to 4 | 5 to 7 |
| Segments closed per Twitch stream | 1 to 3 | 1 to 7 |
| Clips made | 4 | 10 |

At 720p no stream closed more than 4 segments in the 6 minutes (live speed is about 6). At 480p all 6 Kick streams kept
up; two Twitch streams closed only 1 and 2. One Twitch stream wrote
382 MB in the run, which suggests it had no 480p stream and was recorded at
its best quality instead (not checked). Logs: `work/discover-live2.log`, `work/discover-live3.log`.

Not verified: how 480p clips look next to 1080p ones, a 30 minute rescan, and
a long run. Two Twitch streams falling behind at 480p is not fixed.

### Why recordings fall behind (measured 2026-11-21, partly fixed)

The connection is not the cause. Two 6 minute runs of the top 5 `Just
Chatting` streams at 720p, about half an hour apart, on different channels:

| | `watch` (recording, chat and processing) | recording only |
|---|---|---|
| Segments per stream after 6 minutes | 1 to 2 processed | 5 closed, all 5 streams |
| streamlink and ffmpeg errors | not kept | none (ffmpeg log empty) |

So five 720p streams record at live speed when nothing else runs. The
recorders fall behind when transcription and rendering run on the same
machine. Which part starves them (CPU, disk) was not measured. Logs:
`work/discover-live4.log`, `work/diag-720-5.log`, `work/diag-720-5/`.

Priority change (same day): streamlink and ffmpeg now start above normal
priority and the `watch` process drops itself below normal (Windows only).
A third 6 minute run, again top 5 at 720p on other channels
(`work/discover-live5.log`):

- 180 s in, each stream had 1 to 2 segments closed. The run before the
  change had 0 to 1.
- No stream logged "recording is slower than live". All 5 did before.
- Recording still slows while clips are processed: one 60 s segment took up
  to 132 s to write. After processing ended, segments were written in 31 to
  39 s, so the recorder caught up. The times in `segments.csv` have no gaps.

So the change helps but does not make five 720p streams keep live speed
during processing. Whether a long run keeps catching up was not tested.

Also found in review that day (all but the 480p item fixed later, see "Review fixes
(2026-11-21)"): a rescan where one platform
fails stops that platform's recordings; chat collectors do not reconnect;
the chat clock starts up to 90 s before the first video frame; channels with
no 480p stream record at best quality; the disk cap is by age, not size; the
"segment closed" log line is printed when processing starts, not when the
segment closes.

## Chat

- Twitch: anonymous IRC read. No login.
- YouTube: yt-dlp live chat download. No login.
- Kick: public channel API plus the public Pusher websocket. No login.

When chat cannot be read, `chat_rate` is `None` and its weight is shared
among the other signals.

## Verified on 2026-11-17

Machine: Windows 11, Python 3.13, ffmpeg 9.0.2, RTX 3060. No
`ANTHROPIC_API_KEY` was set, so all text scoring below used the heuristic.

**`video`** on `https://www.youtube.com/watch?v=EJXTZ5jpSmk` (93.2 s source):
2 clips. ffprobe:

| Clip | Resolution | Duration | Size |
|---|---|---|---|
| `youtube_EJXTZ5jpSmk_000005_bd6a41.mp4` | 1080x1920 | 53.50 s | 18,727,990 bytes |
| `youtube_EJXTZ5jpSmk_000061_5e7048.mp4` | 1080x1920 | 25.30 s | 10,790,520 bytes |

Layout `center`. A 20.4 s sample measured -14.2 LUFS integrated. Both caption
styles were rendered and checked by eye
(`work/youtube-watch-v-ejxtz5jpsmk/sample_group.mp4`, `sample_word.mp4`).

**Unit tests**: `pytest clipper/tests -q` gives 19 passed (scoring function
and sidecar schema).

**`watch`**, three channels live at test time, 5.5 minutes
(`work/live-test.log`):

| Platform | Channel | Recording | Chat | Clips | Lag behind live |
|---|---|---|---|---|---|
| YouTube | `@SkyNews` | tested: 5 segments of 60 s | readable (85 messages) | 0 | under 3 s |
| Twitch | `nickmercs` | tested: 5 segments of 60 s | readable (502 messages) | 5, layout `face` | 23 to 44 s |
| Kick | `sam` | tested: 5 segments of 59 to 61 s | not readable | 4 (3 `face`, 1 `center`) | 5 to 31 s |

- YouTube made no clip because the Sky News stream was showing a silent
  "This live stream will resume shortly" slate (audio mean -74.0 dB, 0 words).
  A second 3.3-minute YouTube run on `@aljazeeraenglish`
  (`work/live-test2.log`) recorded 3 segments, read chat (84 messages) and
  made 1 clip: `youtube_aljazeeraenglish_000018_568c29.mp4`, 1080x1920,
  21.1 s, 6,436,835 bytes, layout `face`, 2 to 10 s behind live.
- Kick chat failed at start with `JSONDecodeError`. Kick clips were scored
  without chat. Fixed on 2026-11-21, see "Kick fixes" above.
- Four of the nine live clips came out shorter than 15 s (6.6 to 11.8 s)
  because dead-air trimming ran after the window was chosen. `pipeline.py`
  now drops windows that would fall under `min_clip_seconds` after trimming.
  The second YouTube run used the fix and its one clip was 21.1 s. The fix
  has not been rerun on Twitch or Kick.

## Local model, verified on 2026-11-18

Ollama 0.40.17-rc0, RTX 3060 Laptop GPU (6 GB), no `ANTHROPIC_API_KEY` set.

Three sample transcripts (a strong hook, filler, a clear explainer), scored
as hook / standalone, with seconds per call (first call includes model load):

| Model | Strong hook | Filler | Explainer | Seconds |
|---|---|---|---|---|
| `llama3.2` | 9 / 8 | 0 / 0 | 8 / 6 | 6.7, 2.5, 2.6 |
| `huihui_ai/qwen3.5-abliterated:4b` | 8 / 6 | 2 / 3 | 2 / 3 | 9.5, 3.2, 3.4 |
| `huihui_ai/qwen3-abliterated:8b-v2` | 8 / 9 | 2 / 2 | 7 / 8 | 12.9, 5.0, 5.1 |

All nine replies were valid JSON. `llama3.2` is the default: fastest, and it
fits on the GPU next to whisper. The 8b model ran 27% on CPU.

**`video`** on `https://www.youtube.com/watch?v=EJXTZ5jpSmk` with
`llm_backend: local`, `local_model: llama3.2`: log line
`text scored by local model llama3.2`, 31 candidate windows, 2 clips, whole
run 1 min 57 s.

| Clip | Resolution | Duration | Size | Score | Title from the model |
|---|---|---|---|---|---|
| `youtube_EJXTZ5jpSmk_000032_166eb6.mp4` | 1080x1920 | 53.10 s | 20,484,114 bytes | 0.5927 | Supermassive Black Holes |
| `youtube_EJXTZ5jpSmk_000005_25fd64.mp4` | 1080x1920 | 20.40 s | 7,443,143 bytes | 0.5581 | Black Holes |

**Unit tests**: `pytest clipper/tests -q` gives 28 passed. The 9 new ones
(`tests/test_llm_local.py`) cover reply parsing, backend selection, rating
with no API key, fallback when Ollama is unreachable or the model is missing,
and the retry without `think`. They use a fake server.

**`watch`** on `https://www.youtube.com/@aljazeeraenglish`, 4 minutes,
`llm_backend: local` (`work/live-test3.log`): log line
`text scored by local model llama3.2`, 3 segments (65 s, 60 s, 60 s), chat
readable (89 messages), 1 clip:
`youtube_aljazeeraenglish_000000_a67b7f.mp4`, 1080x1920, 29.40 s,
14,673,149 bytes, layout `face`, score 0.6299, title from the model
"Dirty Water in Camp". The sidecar passes `validate_sidecar`.
llama3.2 ran 100% on the GPU next to whisper (5,633 MiB of 6,144 MiB used).

Lag behind live was 68 to 86 s. The 2026-11-17 heuristic run on the same
channel was 2 to 10 s behind. This lag figure is not a clean measurement:
right after the run, another Python process on the machine (not the
clipper) held the GPU at 100% with about 4.9 GB in use. Timed on a recorded
60 s segment under that load, transcription alone took 22 to 26 s with no
local model loaded, and llama3.2 calls took 4.3 to 6.4 s each (2.5 s in the
earlier sample test). The lag needs a rerun on an idle GPU.

Not verified for the local backend: `watch` on Twitch or Kick, and how well
the scores match what performs as a short. Three samples is a sanity
check, not an evaluation.

## Venice backend, verified on 2026-11-20

`pytest clipper/tests -q`: 43 passed. The Venice tests use a fake server.

Live runs of `python -m clipper video` on
`https://www.youtube.com/watch?v=EJXTZ5jpSmk` (93.2 s, 221 words, 31
candidate windows), `venice_model: zai-org-glm-5`:

- One-by-one rating (`work/venice-run.log`): 2 picked, best score 0.658.
  Clips of 38.4 s (score 0.658) and 20.9 s (score 0.495).
- Whole-transcript picking (`work/picks-run.log`): log line `3 moments
  chosen by the model from the whole transcript`, 3 picked, best score
  0.627. Two new clips were rendered: 32.2 s (score 0.616) and 26.8 s
  (score 0.593). The third pick already existed in `out/` and was skipped.

Not verified for Venice: `watch` on a live stream, a transcript long enough
to hit the 400,000 character limit, and whether whole-transcript picks
perform better as shorts. One 93 s video is a sanity check, not an
evaluation.

## Not verified

- The Claude backend. No API key was set, so the Claude call and the
  refusal-fallback option have never run.
- The `stack` layout (facecam over gameplay). No test source triggered it.
- The 2-hour disk cleanup deleting real files. Nothing was old enough.

## Verified later on 2026-11-21

- Kick chat: readable on a live channel (8 messages in 30 s), and chat was readable on all 10 streams (Kick and Twitch) in the
  two 6 minute runs after the key fix, see "Kick fixes".
- yt-dlp recording fallback: in `work/discover-live.log` streamlink produced no
  data on 6 Kick channels and the log shows `recording with yt-dlp` for each;
  for `kick-odedsvr` the session folder `20261121-170209` holds `seg_00001.ts`.
  Whether every one of those six produced usable clips was not checked.

## Review fixes (2026-11-21)

Fixes for the defects listed under "Why recordings fall behind". Checked by
unit tests (`pytest clipper/tests -q`, 87 passed, `tests/test_review_fixes.py`)
and one 5 minute live run of the top 3 streams at 720p (`work/discover-live6.log`).

That run: segments closed every 46 to 70 s on all 3 streams, processing was
0 to 77 s behind live, no "slower than live" warning, chat readable on all 3,
2 clips made. After it ended every segment and `window.ts` was listed in
`processed.json`, and no streamlink or ffmpeg process from it was left.
`config.yaml` is therefore set to `live_quality: 720p`, `discover_top: 3`.
Four and five streams at 720p were not tested on this code.

Not seen live: a chat reconnect, a rescan with one platform down, the size
cap deleting files, the yt-dlp path, and a stream ending by itself.

1. A rescan no longer stops the running streams of a platform it could not
   read (`discover.top_streams(..., unread=set())`, `watch.plan_changes(..., unread)`).
2. `processed.json` is written to a temp file and swapped in with `os.replace`.
   `cleanup` ignores a corrupt or missing file, and a cleanup error is logged
   and never ends a recording.
3. Twitch and Kick chat reconnect with a 5 s to 60 s backoff until stopped.
   `readable` is false while disconnected. A reset during shutdown is not
   logged. YouTube chat is readable only after at least one message.
4. Chat times and the "behind live" figure start at the first data
   (`Recorder.origin()`, when the first `seg_*.ts` appears), not at process spawn.
5. `llm.rate` no longer caches a `None`.
6. `Recorder.stop()` runs `taskkill /PID <pid> /T /F` on Windows. `watch` stops the
   recorders of sessions still alive after the 180 s wait.
7. The segment that closes as a stream ends is processed once after the
   recorder exits, unless `watch` was stopped.
8. New `raw_max_gb` (default 20). Empty finished session folders are removed;
   the open segment left by a finished session is marked processed.
9. The log line now reads "processing segment X".

Not changed here: the "recording is slower than live" warning and transcript
caching. Quality fallback and simulcast dedupe were done later, see "Later
fixes". If the first data arrives after a
yt-dlp fallback, chat times already taken are shifted when `Recorder.origin()`
is first known; this path was not tested.

## Later fixes (2026-11-21)

`pytest clipper/tests -q`: 90 passed.

Measured first (`small` model, one 120 s window, two rounds): the whole window
took 37.3 s and 25.6 s. Transcription 8.9 and 6.7 s, the Venice pick call 17.1
and 9.1 s, the render 10.9 and 9.5 s, face sampling 1.5 and 1.4 s. Transcription
runs on the GPU (RTX 3060 Laptop, ctranslate2 sees 1 CUDA device; a 60 s
segment took 5.8 s, then 3.3 s). No log shows a fall back to CPU.

1. The LLM calls no longer hold the lock. `process_media(..., lock=LOCK)`
   holds it for transcription, loudness and the render only, so one stream
   waiting on Venice does not block the others.
2. Quality fallback. `stream_spec("480p")` now asks for 480p, then 360p, then
   the next heights up, and `best` last. Before, a channel without 480p got
   `best`. Unit tested; not seen live on a channel without that height.
3. Simulcasts. When the same channel name is live on Twitch and Kick, only the
   one with more viewers is recorded (`discover_skip_simulcast`, default true).
   The match is by name only. Unit tested; not seen live.
4. Token count. `watch` logs "LLM usage this run" when it ends.

Five streams at 720p still fall behind. A 6 minute run of the top 5 with the
lock change (`work/discover-live7.log`): 4 of 5 streams logged "recording is
slower than live" (1 segment closed in 180 to 182 s), and processing ended
172 to 270 s behind live on the four streams that got to a second window.
10 windows were processed and 6 clips made; the streams closed 2 to 4
segments each. No recorder process was left. So `discover_top: 3` stays. One
Kick chat drop (`Pusher error 4201`) and one Venice network error (the window
used the text heuristic) were logged in this run.

Not done: overlapping windows are still transcribed twice. That costs 6.7 to
8.9 s per 120 s window on the GPU, and a per-segment cache could split words
at the segment edge.

### Cost of the LLM

Venice lists `zai-org-glm-5` at $1 per 1M input tokens and $3.2 per 1M output
tokens (`https://api.venice.ai/api/v1/models?type=text`, read 2026-11-21). The
run above used 27 calls, 13024 input and 11035 output tokens: $0.048336 for
10 processed windows, $0.0048336 per window. At one window per stream per
minute that is an estimate of $0.29 per stream per hour. Tokens were not
counted before this change, so the total spent so far is only on the Venice
dashboard.

### Language filter (2026-11-21)

`discover_language: en` keeps only streams tagged with that language (sent to
Twitch and Kick as `language`, and checked again on each row). `config.yaml`
is set to `Grand Theft Auto VI` on Twitch and `Grand Theft Auto VI (GTA)` on
Kick, English. A live lookup returned 3 English Twitch streams; with no
language the top 10 held other languages too. No English Kick stream was in
the top 3, so the Kick filter was not seen to pick one. `pytest clipper/tests
-q`: 91 passed.
