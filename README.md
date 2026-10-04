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

Python packages beyond the basics: `faster-whisper`, `onnxruntime`, `onnx`, `tokenizers`, `rapidocr_onnxruntime`,
`pypinyin`, `opencc-python-reimplemented`.

- `grabber.py web` – page + API (waitress); `grabber.py worker` – runs queued jobs, follows channels
- `grabber.service`, `grabber-worker.service` – systemd units; config in `/etc/grabber.env`
  (template: `grabber.env.example`)
- `refresh_metadata.py` – re-run the classifier on finished jobs
