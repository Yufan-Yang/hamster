# 拾光 (hamster)

A home download box for a Raspberry Pi. Send it a link (web page, iOS share sheet, Android HTTP Shortcuts,
bookmarklet) and it downloads the video (yt-dlp / headless Chromium sniffing / aria2 for files and torrents),
makes it Plex-friendly, transcribes it when useful, asks an LLM (DeepSeek, OpenAI-compatible) for a summary
and tags, and files it into the Plex library. The web UI (`index.html`) lists and plays everything.

Pasting a B站 uploader space or a YouTube channel link follows it (追更): its latest videos are cached and new
ones are picked up every week.

随记 is a private diary inside the page: text, photos, videos and voice. Videos and voice are transcribed
(faster-whisper `small`) so what was said can be searched; files live in `MEDIA_ROOT/.notes`.

Search covers titles, summaries, tags, subtitles (with the moment they're said: results open the video there),
text on covers and photos (RapidOCR) and what pictures look like (Chinese-CLIP, ONNX int8, split into a text
half for queries and an image half for indexing under `STATE_DIR/models/clip`). Notes also match typos and
pinyin. The slow part is done ahead of time by the worker in idle time (no download being processed): covers,
a video frame every `FRAME_EVERY` s, and full `.srt` subtitles for videos that have none (`IDLE_WHISPER_MODEL`,
default `small`; resumable, pauses as soon as a download needs the CPU). `IDLE_WORK=0` turns it off.
「资源使用」 on the page sums up traffic (Xray outbounds + Wi-Fi), downloads, processing and LLM tokens by purpose.

Slow work is a task on one board (`tasks` table, on the Pi): the Pi publishes when something happens (download
finished, note added), a finished task publishes the next (transcribe → save_subs → summarize; cover/frames →
save_*), the Mac publishes too. Workers claim what they can do with a lease kept by heartbeats: the Pi's light
worker (library writes, AI calls), its heavy worker (CPU, idle time only) and the Mac. Only the Pi writes the
library. `mac/`: the Mac mini worker (Whisper large-v3-turbo on its GPU, CLIP pictures, macOS text recognition,
drop folder; it stops while a game is in front), over the LAN with `COMPUTE_TOKEN`. What it installs and how to
undo it: `mac/README.md`.
LLM costs: each DeepSeek call is priced from its token counts (cache hits, peak/off-peak) and the account balance is
recorded, so 「资源使用」 shows both the list-price estimate and what was really charged.

Also: 继续观看 across devices (position per account), Chinese subtitles for English videos (DeepSeek without
thinking, off-peak; the player shows both), 追更周报 (Monday mornings, or on demand), 随记 那年今天 and places
(photo/video GPS named offline from GeoNames cities15000 in `STATE_DIR/models/geo`), re-uploads and clips found by
what's said (MinHash) or, for videos with little speech, by their frames. `pi/disk-health*`: a root timer that
writes the disks' SMART data to /run/disk-health.json (needs smartmontools); 资源使用 shows it with the CPU temperature.

Python packages beyond the basics: `faster-whisper`, `onnxruntime`, `onnx`, `tokenizers`, `rapidocr_onnxruntime`,
`pypinyin`, `opencc-python-reimplemented`.

- `grabber.py web` – page + API (waitress); `grabber.py worker` – runs queued jobs, follows channels.
  The code is the `shiguang` package: `core` (config, database, wake-ups), `migrations` (numbered schema steps,
  one-time data jobs), `download`, `library` (media files, Plex, playback), `llm`, `pipeline` (a job from link to
  library), `channels` (追更), `search` (subtitles, text on pictures, CLIP), `board` (the task board), `tasks`
  (what each kind of task does, declared with `@task`), `notes`, `usage`, `web` (routes), `main` (processes).
- `./deploy.sh` – lint, smoke test on the Pi against a copy of the database (`tests/smoke.py`), swap in, restart,
  and put the previous code back if the page or the workers don't come up
- `grabber.service`, `grabber-worker.service` – systemd units; config in `/etc/grabber.env`
  (template: `grabber.env.example`)
- `refresh_metadata.py` – re-run the classifier on finished jobs
