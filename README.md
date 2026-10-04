# 拾光 (hamster)

A home download box for a Raspberry Pi. Send it a link (web page, iOS share sheet, Android HTTP Shortcuts,
bookmarklet) and it downloads the video (yt-dlp / headless Chromium sniffing / aria2 for files and torrents),
makes it Plex-friendly, transcribes it when useful, asks an LLM (DeepSeek, OpenAI-compatible) for a summary
and tags, and files it into the Plex library. The web UI (`index.html`) lists and plays everything.

Pasting a B站 uploader space or a YouTube channel link follows it (追更): its latest videos are cached and new
ones are picked up every week.

- `grabber.py web` – page + API (waitress); `grabber.py worker` – runs queued jobs, follows channels
- `grabber.service`, `grabber-worker.service` – systemd units; config in `/etc/grabber.env`
  (template: `grabber.env.example`)
- `refresh_metadata.py` – re-run the classifier on finished jobs
