#!/opt/grabber/venv/bin/python
"""Grabber: download anything sent from the web page or Telegram, analyze it, file it for Plex.

Sources: video sites (yt-dlp), pages with an embedded player (headless Chromium sniffing),
direct file URLs (aria2) and magnets/.torrent files (aria2 as user `bt`, which bypasses the proxy).
After download: make it Plex-friendly, get a transcript (subtitles or Whisper),
ask an LLM (DeepSeek) for a summary + category, and move it into the media library.
"""
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

import requests
from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session

MEDIA = Path(os.environ.get("MEDIA_ROOT", "/mnt/media"))
INCOMPLETE = MEDIA / ".incomplete"
STATE = Path(os.environ.get("STATE_DIR", "/var/lib/grabber"))
DB_PATH = STATE / "grabber.db"
COOKIES = STATE / "cookies.txt"  # optional Netscape cookies file for sites that need a login
PORT = int(os.environ.get("PORT", "8088"))
# Access from outside the home arrives through a reverse tunnel from the VPS to this loopback-only port
# (nothing on the LAN can reach it), so requests on it are known to be from the internet.
EXTERNAL_PORT = int(os.environ.get("EXTERNAL_PORT", "8090"))
ADMIN_PASSWORD = os.environ.get("GRABBER_PASSWORD", "")  # password of the "admin" account, which sees everything
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
LLM_EFFORT = os.environ.get("LLM_EFFORT", "low")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
TRANSCRIBE_MAX_MIN = int(os.environ.get("TRANSCRIBE_MAX_MIN", "6"))
FORMAT_SORT = os.environ.get("YTDLP_FORMAT_SORT", "vcodec:h264,res:1080,acodec:aac,ext:mp4:m4a")
# Jobs running at once. Most of a job is waiting on the network, so several fit; the CPU/memory-heavy
# steps (headless browser, speech-to-text, re-encoding) each take a slot below so they never pile up.
DOWNLOAD_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "4"))
HEAVY_SLOTS = {"browser": 1, "whisper": 1, "encode": 1}
CHROMIUM = "/usr/bin/chromium"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".m4v", ".ts", ".mpg", ".mpeg", ".rmvb", ".3gp"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav", ".aac", ".ape", ".wma"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
VIDEO_FOLDERS = ["Music Videos", "Tutorials", "Talks", "News", "Gaming", "Sports", "Documentaries",
                 "Vlogs", "Comedy", "Clips", "Other"]
URL_RE = re.compile(r"(magnet:\?[^\s<>\"]+|https?://[^\s<>\"]+)")


def find_urls(text):
    """Links in pasted text; links pasted back-to-back without a space are split apart."""
    out = []
    for u in URL_RE.findall(text):
        out += [p for p in re.split(r"(?<=.)(?=https?://|magnet:\?)", u) if p] if not u.startswith("magnet:") else [u]
    return list(dict.fromkeys(out))

app = Flask(__name__, static_folder=None)
db_lock = threading.Lock()
# Process layout (so one busy download can't freeze the page; Python uses ~one core per process):
#   grabber.py web       the page and API (waitress)
#   grabber.py worker    picks queued jobs and runs each in its own process, plus housekeeping
#   grabber.py run-job N one download/analysis job, at lower priority
# They share the SQLite database; cancelling is a flag in it that the job process checks.
_cancel_checked = {}  # job id -> last time the cancel flag was read


class Cancelled(Exception):
    pass


finishing_threads = []  # work started after a job is done (Plex metadata, tag tidy-up, browser copy)


def finishing_touch(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    finishing_threads.append(t)


import contextlib
import fcntl


@contextlib.contextmanager
def heavy_slot(kind, job_id=None):
    """Hold one of HEAVY_SLOTS[kind] across all job processes (a file lock), waiting if they're all busy."""
    (STATE / "locks").mkdir(parents=True, exist_ok=True)
    handles = [open(STATE / "locks" / f"{kind}-{i}.lock", "w") for i in range(HEAVY_SLOTS[kind])]
    held = None
    try:
        while held is None:
            for h in handles:
                try:
                    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = h
                    break
                except BlockingIOError:
                    pass
            if held is None:
                if job_id is not None:
                    update(job_id, speed="排队等资源")
                    check_cancel(job_id)
                time.sleep(2)
        if job_id is not None:
            update(job_id, speed="")
        yield
    finally:
        for h in handles:
            h.close()  # closing releases the lock


# ---------------------------------------------------------------- database

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # readers and the writer in other processes don't block each other
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


DB = None


def init_db(reset=False):
    global DB
    STATE.mkdir(parents=True, exist_ok=True)
    DB = db()
    DB.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        source TEXT, chat_id INTEGER, msg_id INTEGER,
        status TEXT DEFAULT 'queued', stage TEXT DEFAULT '', progress REAL DEFAULT 0, speed TEXT DEFAULT '',
        kind TEXT DEFAULT '', title TEXT DEFAULT '', files TEXT DEFAULT '[]', thumb TEXT DEFAULT '',
        analysis TEXT DEFAULT '{}', error TEXT DEFAULT '', transcript TEXT DEFAULT '', owner TEXT,
        created REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, pw TEXT, admin INTEGER DEFAULT 0, created REAL);
    -- browsers, by their cookie id; `user` is set while the browser is logged in
    CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, user TEXT, ip TEXT, seen REAL);
    -- iPhones sending through the share-sheet shortcut, by device name, tied to the browser on that phone
    CREATE TABLE IF NOT EXISTS phones (name TEXT PRIMARY KEY, device TEXT, created REAL);
    -- privacy mode: per account (or anonymous device) tags and items to hide
    CREATE TABLE IF NOT EXISTS privacy (owner TEXT PRIMARY KEY, tags TEXT DEFAULT '[]', ids TEXT DEFAULT '[]', pin TEXT);
    -- followed uploaders (追更); `seen` = link keys of videos already queued, `everything` = "缓存全部" requested
    CREATE TABLE IF NOT EXISTS probes (path TEXT PRIMARY KEY, mtime REAL, duration REAL, vcodec TEXT);
    CREATE TABLE IF NOT EXISTS subs (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, platform TEXT, key TEXT, url TEXT,
        name TEXT, avatar TEXT DEFAULT '', total INTEGER DEFAULT 0, backfill INTEGER, seen TEXT DEFAULT '[]',
        everything INTEGER DEFAULT 0, checked REAL, error TEXT DEFAULT '', device TEXT, created REAL);
    """)
    cols = [r[1] for r in DB.execute("PRAGMA table_info(jobs)")]
    if "transcript" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN transcript TEXT DEFAULT ''")
    if "owner" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
    if "pin" not in [r[1] for r in DB.execute("PRAGMA table_info(privacy)")]:
        DB.execute("ALTER TABLE privacy ADD COLUMN pin TEXT")  # privacy password for devices without an account
    if "attempts" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 0")
        DB.execute("ALTER TABLE jobs ADD COLUMN retry_at REAL")
    if "ref" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN ref INTEGER")  # job whose files this entry shares
    if "key" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN key TEXT")
        for r in DB.execute("SELECT id, url FROM jobs").fetchall():
            DB.execute("UPDATE jobs SET key=? WHERE id=?", (link_key(r[1]), r[0]))
    if "device" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN device TEXT")  # which device sent it, e.g. "Mac · Chrome"
    if "label" not in [r[1] for r in DB.execute("PRAGMA table_info(devices)")]:
        DB.execute("ALTER TABLE devices ADD COLUMN label TEXT")
    if "cancel" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN cancel INTEGER DEFAULT 0")  # set by the page, read by the job
    if reset:
        # Worker start-up: anything interrupted goes back in the queue (partial downloads resume)
        DB.execute("UPDATE jobs SET status='queued', stage='', progress=0 WHERE status IN ('downloading','processing')")
    DB.commit()


def q(sql, args=(), one=False):
    with db_lock:
        cur = DB.execute(sql, args)
        DB.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def update(job_id, **fields):
    fields["updated"] = time.time()
    for k in ("files", "analysis"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    q(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def kv_get(k, default=None):
    row = q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return json.loads(row["v"]) if row else default


def kv_set(k, v):
    q("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))


TRACKING_PARAMS = re.compile(r"^(utm_.*|si|spm_id_from|vd_source|share_.*|from|from_spmid|feature|fbclid|gclid|igsh|"
                             r"unique_k|is_story_h5|mid|plat_id|timestamp|xsec_.*|_t|ref|ref_src)$", re.I)
SHORTENERS = ("b23.tv", "v.douyin.com", "t.co", "bit.ly", "xhslink.com", "youtu.be")


def link_key(url):
    """Identity of a link for spotting duplicates: share links of the same video differ only in tracking
    parameters (YouTube's ?si=, Bilibili's ?spm_id_from=...), so those are dropped."""
    if url.startswith("torrent-file:"):
        return url
    if url.startswith("magnet:"):
        m = re.search(r"btih:([0-9a-zA-Z]+)", url)
        return f"btih:{m.group(1).lower()}" if m else url
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host in SHORTENERS and host != "youtu.be":
        try:  # follow the short link once to see where it points
            url = requests.head(url, allow_redirects=True, timeout=6, headers={"User-Agent": UA}).url
            parts = urllib.parse.urlsplit(url)
            host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
        except requests.RequestException:
            pass
    qs = urllib.parse.parse_qs(parts.query)
    if host in ("youtube.com", "music.youtube.com") and "v" in qs:
        return f"youtube:{qs['v'][0]}"
    m = re.match(r"/(?:shorts|live|embed)/([\w-]{6,})", parts.path)
    if host in ("youtube.com", "music.youtube.com") and m:
        return f"youtube:{m.group(1)}"
    if host == "youtu.be":
        return f"youtube:{parts.path.strip('/')}"
    m = re.match(r"/video/(BV\w+|av\d+)", parts.path, re.I)
    if host == "bilibili.com" and m:
        return f"bilibili:{m.group(1)}:p{qs.get('p', ['1'])[0]}"
    query = urllib.parse.urlencode(sorted((k, v) for k, vs in qs.items() if not TRACKING_PARAMS.match(k) for v in vs))
    return f"{host}{parts.path.rstrip('/')}" + (f"?{query}" if query else "")


SHARED_FIELDS = ("status", "kind", "title", "files", "thumb", "analysis", "error", "transcript", "progress")


def add_job(url, source="web", chat_id=None, msg_id=None, owner=None, device=None):
    return add_job_ex(url, source, chat_id, msg_id, owner, device)[0]


def add_job_ex(url, source="web", chat_id=None, msg_id=None, owner=None, device=None):
    """Queue a link. Returns (job id, how): "new", "duplicate" when this owner already has it queued,
    downloading or downloaded (the existing job is returned), or "linked" when someone else already has it:
    then this owner gets their own entry that points at the same files instead of a second download."""
    url = url.strip()
    key = link_key(url)
    existing = q("SELECT id FROM jobs WHERE key=? AND owner IS ? AND status NOT IN ('failed','cancelled') "
                 "ORDER BY id DESC LIMIT 1", (key, owner), one=True)
    if existing:
        return existing["id"], "duplicate"
    source_job = q("SELECT * FROM jobs WHERE key=? AND ref IS NULL AND status IN ('queued','downloading','processing','done') "
                   "ORDER BY status='done' DESC, id DESC LIMIT 1", (key,), one=True)
    with db_lock:
        cur = DB.execute("INSERT INTO jobs (url, key, source, chat_id, msg_id, owner, device, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (url, key, source, chat_id, msg_id, owner, device, time.time(), time.time()))
        DB.commit()
        job_id = cur.lastrowid
    if source_job:
        if source_job["status"] == "done":
            # Already downloaded: copy the result over (same files on disk)
            update(job_id, ref=source_job["id"], **{f: source_job[f] for f in SHARED_FIELDS})
        else:
            # Still downloading: wait for it; finish_links() fills this in when it's done
            update(job_id, ref=source_job["id"], status="linked")
        return job_id, "linked"
    download_wakeup.set()
    return job_id, "new"


def finish_links(source_id):
    """A job finished, failed or was cancelled: update the entries other accounts linked to it."""
    src = q("SELECT * FROM jobs WHERE id=?", (source_id,), one=True)
    if not src:
        return
    if src["status"] == "failed" and src["retry_at"]:
        return  # the original retries by itself; entries linked to it keep waiting for that
    for r in q("SELECT id FROM jobs WHERE ref=? AND status='linked'", (source_id,)):
        if src["status"] in ("done", "failed"):
            update(r["id"], **{f: src[f] for f in SHARED_FIELDS})
        else:  # the original was cancelled: download it for this account after all
            update(r["id"], ref=None, status="queued", progress=0)
            download_wakeup.set()


def job_dict(row):
    d = dict(row)
    d["files"] = json.loads(d["files"] or "[]")
    d["analysis"] = json.loads(d["analysis"] or "{}")
    d.pop("transcript", None)  # only used for search; can be long
    return d


# ---------------------------------------------------------------- helpers

def safe_name(s, limit=150):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s or "").strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].rstrip(" .") or "untitled")


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}B"
        n /= 1024


def cancel_requested(job_id):
    """Whether the page asked to cancel this job (read from the database at most every 2 seconds)."""
    now = time.time()
    if now - _cancel_checked.get(job_id, 0) < 2:
        return False
    _cancel_checked[job_id] = now
    row = q("SELECT cancel FROM jobs WHERE id=?", (job_id,), one=True)
    return bool(row and row["cancel"])


def check_cancel(job_id):
    if cancel_requested(job_id):
        raise Cancelled()


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}


def unique_path(p: Path):
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"too many copies of {p}")


# ---------------------------------------------------------------- download: yt-dlp

def ytdlp_opts(job_id, workdir, extra=None):
    def hook(d):
        check_cancel(job_id)
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = done * 100 / total if total else 0
            spd = d.get("speed") or 0
            update(job_id, progress=round(pct, 1), speed=f"{human(spd)}/s" if spd else "")

    opts = {
        "outtmpl": str(workdir / "%(title).150B [%(id)s].%(ext)s"),
        "format_sort": FORMAT_SORT.split(","),
        "merge_output_format": "mp4/mkv",
        "writesubtitles": True, "writeautomaticsub": True,
        # Only original-language tracks; "en.*" would also pull every auto-translation and trip YouTube's rate limit
        "subtitleslangs": ["en", "en-orig", "en-US", "en-GB", "zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh", "ja", ".*-orig"],
        "subtitlesformat": "srt/best",
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook],
        "sleep_interval_subtitles": 2,  # many subtitle requests in a row trip YouTube's 429
        "retries": 10, "fragment_retries": 20, "file_access_retries": 5, "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "ignoreerrors": False,
        "postprocessors": [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    if extra:
        opts.update(extra)
    return opts


# YouTube sometimes answers one proxy exit IP with "Sign in to confirm you're not a bot" (or 429) while
# the other exit still works. Xray has a SOCKS inbound per exit (127.0.0.1:10820 → proxy-a, 10821 → proxy-b),
# so on that error yt-dlp tries again through each one directly.
BOT_CHECK = re.compile(r"confirm you.?re not a bot|HTTP Error 429|Too Many Requests", re.I)
EXITS = os.environ.get("YTDLP_EXITS", "socks5h://127.0.0.1:10821 socks5h://127.0.0.1:10820").replace(",", " ").split()


def ytdlp_probe(url, extra=None):
    """Return yt-dlp info if it can download this URL itself, else None. info["_net"] holds the proxy
    setting that worked, for the download. Raises when the site blocks us as a bot through every exit."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    if extra:
        opts.update(extra)
    info, err = None, None
    for net in [{}] + [{"proxy": p} for p in EXITS]:
        try:
            with yt_dlp.YoutubeDL({**opts, **net}) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            err = e
            if not BOT_CHECK.search(str(e)):
                return None  # not something yt-dlp handles: try the page sniffer
    if info is None and err is not None:
        raise RuntimeError(f"网站把下载当成了机器人（每个代理出口都试过了）：{str(err)[:300]}")
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
    elif not (info.get("formats") or info.get("url")):
        return None
    info["_net"] = net
    return info


def ytdlp_download(job_id, url, workdir, extra=None):
    """Download with yt-dlp; when the site takes us for a bot, try again through the other proxy exits."""
    nets = [extra or {}] + [{**(extra or {}), "proxy": p} for p in EXITS if p != (extra or {}).get("proxy")]
    for i, net in enumerate(nets):
        try:
            return _ytdlp_download(job_id, url, workdir, net, drop_subs_on_error=i == len(nets) - 1)
        except Exception as e:
            if i == len(nets) - 1 or not BOT_CHECK.search(str(e)) or "youtube" not in url and "youtu.be" not in url:
                raise


def _ytdlp_download(job_id, url, workdir, extra=None, drop_subs_on_error=True):
    import yt_dlp
    update(job_id, stage="downloading video")
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts(job_id, workdir, extra)) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "subtitles" not in str(e):
            raise
        if BOT_CHECK.search(str(e)) and not drop_subs_on_error:
            raise  # subtitles rate-limited (429): try the next proxy exit before going without them (= speech-to-text)
        # Subtitles are nice to have; don't fail the download over them
        with yt_dlp.YoutubeDL(ytdlp_opts(job_id, workdir, {**(extra or {}), "writesubtitles": False,
                                                             "writeautomaticsub": False})) as ydl:
            info = ydl.extract_info(url, download=True)
    if info.get("_type") == "playlist":
        info = next(e for e in info["entries"] if e)
    meta = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "upload_date", "duration", "description",
                                     "extractor_key", "webpage_url", "tags", "categories", "view_count",
                                     "artist", "track", "album", "series", "season_number", "episode_number")}
    if meta.get("description") and len(meta["description"]) > 5000:
        meta["description"] = meta["description"][:5000] + " …[description cut at 5000 chars]"
    return meta


# ---------------------------------------------------------------- download: embedded player sniffing

PLAYER_CONFIG = re.compile(r'"video"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"')  # DPlayer-style configs
MEDIA_IN_PAGE = re.compile(r'(?:https?:)?[\w:/.\-%]*?/[\w/.\-%]+\.(?:m3u8|mpd|mp4)(?:\?[^"\'\s<>\\]*)?')


def media_in_page(html_text, base):
    """Media URLs written into the page (player configs, inline JSON), for players that only fetch the
    stream after an ad or a click. Returns (player config URLs, other URLs), in page order."""
    import html as htmllib
    text = htmllib.unescape(html_text).replace("\\/", "/")
    def norm(u):
        return urllib.parse.urljoin(base, u.replace("\\u0026", "&").replace("&amp;", "&"))
    players = [norm(u) for u in PLAYER_CONFIG.findall(text)]
    others = [norm(u) for u in MEDIA_IN_PAGE.findall(text)]
    return players, others


AD_HOSTS = re.compile(r"doubleclick|googlesyndication|googleads|adservice|adsystem|imasdk|moatads|criteo|taboola")


def dedupe_media(urls):
    seen, out = set(), []
    for u in urls:
        key = urllib.parse.urlsplit(u)._replace(query="", fragment="").geturl()
        if key not in seen and not AD_HOSTS.search(u):
            seen.add(key)
            out.append(u)
    return out


def write_cookie_file(path, cookies):
    """cookies: [{"domain", "path", "secure", "expires", "name", "value"}] -> Netscape cookies.txt for yt-dlp"""
    with open(path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            exp = c.get("expires") or 0
            f.write("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"] or "/",
                               "TRUE" if c["secure"] else "FALSE", str(int(exp) if exp and exp > 0 else 0),
                               c["name"], c["value"] or ""]) + "\n")


def sniff_page(job_id, url):
    """Find the video on a page yt-dlp doesn't know. Fast path: the player config in the page's HTML
    (no browser). Otherwise open it in headless Chromium, start playback and catch the media requests."""
    update(job_id, stage="looking for the video on the page")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        players, _ = media_in_page(r.text, r.url)
        media = dedupe_media(players)
        if media:
            import html as htmllib
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            title = htmllib.unescape(m.group(1)).strip() if m else ""
            cookie_file = STATE / f"cookies-{job_id}.txt"
            write_cookie_file(cookie_file, [{"domain": c.domain, "path": c.path, "secure": c.secure,
                                             "expires": c.expires, "name": c.name, "value": c.value} for c in r.cookies])
            return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file)}
    except requests.RequestException:
        pass
    from playwright.sync_api import sync_playwright
    found = {}  # url -> (rank, size)

    def on_response(r):
        u, ct = r.url, (r.headers.get("content-type") or "").lower()
        if AD_HOSTS.search(u):
            return
        if re.search(r"\.(m3u8|mpd)(\?|$)", u) or "mpegurl" in ct or "dash+xml" in ct:
            rank = 0 if re.search(r"master|playlist|index", u) else 1
            found.setdefault(u, (rank, 0))
        elif ct.startswith("video/") or re.search(r"\.(mp4|webm|mkv|mov|flv)(\?|$)", u):
            size = int(r.headers.get("content-length") or 0)
            if ct.startswith("video/mp2t") or u.split("?")[0].endswith(".ts"):
                return  # HLS segments; the playlist is what we want
            found[u] = (2, max(size, found.get(u, (2, 0))[1]))

    with heavy_slot("browser", job_id), sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True,
                                    args=["--no-sandbox", "--mute-audio", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.set_default_timeout(20000)  # nothing in here may hang the job
        page.on("dialog", lambda d: d.dismiss())  # alert()/confirm() would block every evaluate
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        play_js = "document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })"
        for _ in range(2):
            for frame in page.frames:
                try:
                    frame.evaluate(play_js)
                except Exception:
                    pass
            # Many players only load the stream after a click on the player
            try:
                box = None
                for v in page.query_selector_all("video, iframe"):
                    b = v.bounding_box()
                    if b and b["width"] > 200 and (not box or b["width"] * b["height"] > box["width"] * box["height"]):
                        box = b
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    page.mouse.click(640, 400)
            except Exception:
                pass
            page.wait_for_timeout(6000)
            if found:
                break
        for frame in page.frames:
            try:
                for src in frame.evaluate("Array.from(document.querySelectorAll('video, video source'))"
                                          ".map(v => v.currentSrc || v.src).filter(s => s && !s.startsWith('blob:'))"):
                    found.setdefault(urllib.parse.urljoin(frame.url, src), (2, 0))
            except Exception:
                pass
        players, in_page = [], []
        # Search the rendered page inside the browser: some pages grow to tens of MB once their scripts run,
        # and copying all of that out to Python takes minutes on the Pi
        find_js = r"""() => {
            const t = document.documentElement.outerHTML;
            const re = /"video"\s*:\s*\{\s*"url"\s*:\s*"[^"]+"|(?:https?:)?[\w:\/.\-%]*?\/[\w\/.\-%]+\.(?:m3u8|mpd|mp4)(?:\?[^"'\s<>\\]*)?/g;
            return (t.match(re) || []).slice(0, 200).join("\n");
        }"""
        for frame in page.frames:
            try:
                p_urls, o_urls = media_in_page(frame.evaluate(find_js), frame.url)
                players += p_urls
                in_page += o_urls
            except Exception:
                pass
        title = page.title()
        cookies = ctx.cookies()
        browser.close()

    # A page can hold several videos (one player each). Player configs are the most reliable source;
    # otherwise take the best stream the browser actually requested, then anything else in the page.
    media = dedupe_media(players)
    if not media:
        ranked = [u for u, _ in sorted(found.items(), key=lambda kv: (kv[1][0], -kv[1][1]))]
        manifests = [u for u in in_page if re.search(r"\.(m3u8|mpd)(\?|$)", u)]
        media = dedupe_media(ranked[:1] or manifests or in_page)
    if not media:
        return None
    cookie_file = STATE / f"cookies-{job_id}.txt"
    write_cookie_file(cookie_file, cookies)
    return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file)}


# ---------------------------------------------------------------- download: aria2 (direct files + torrents)

ARIA_PROGRESS = re.compile(r"\[#\w+ ([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?(?:DL:([\d.]+\w+))?")


def aria2(job_id, args, as_bt=False):
    cmd = ["aria2c", "--continue=true", "--max-tries=10", "--retry-wait=10", "--console-log-level=warn", "--summary-interval=3", "--show-console-readout=true",
           "--enable-color=false", "--file-allocation=falloc", "--auto-file-renaming=false",
           "--allow-overwrite=true"] + args
    if as_bt:
        # Torrents run as `bt`, which the firewall keeps off the proxy
        cmd = ["sudo", "-n", "-u", "bt"] + cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, cancelled = [], False
    for line in proc.stdout:
        tail = (tail + [line.strip()])[-15:]
        m = ARIA_PROGRESS.search(line)
        if m:
            update(job_id, progress=float(m.group(3)), speed=(m.group(4) + "/s") if m.group(4) else "")
        if cancel_requested(job_id):
            cancelled = True
            proc.terminate()
    proc.wait()
    if cancelled:
        raise Cancelled()
    if proc.returncode != 0:
        raise RuntimeError("aria2 failed: " + " | ".join(t for t in tail if t)[-500:])


def head_is_file(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
        if r.status_code >= 400 or r.status_code == 405:
            r = requests.get(url, stream=True, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
            r.close()
    except requests.RequestException:
        return False
    ct = r.headers.get("content-type", "").lower()
    cd = r.headers.get("content-disposition", "").lower()
    if "attachment" in cd:
        return True
    if ct.startswith(("video/", "audio/")) or ct in ("application/octet-stream", "application/x-bittorrent",
                                                     "application/zip", "application/x-iso9660-image"):
        return True
    return bool(re.search(r"\.(mp4|mkv|avi|mov|zip|rar|7z|iso|dmg|exe|pdf|mp3|flac|torrent)$",
                          urllib.parse.urlparse(r.url).path, re.I))


# ---------------------------------------------------------------- post-processing

def make_plex_friendly(job_id, path: Path, keep_mkv=False):
    """Pi can't transcode on the fly, so fix files Plex clients can't play directly.
    MKVs from video sites become MP4 when their streams allow it, so phone browsers can play them too
    (torrents keep MKV: they often carry several audio/subtitle tracks MP4 can't hold)."""
    info = ffprobe(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        return path
    vcodec, acodec = v.get("codec_name"), (a or {}).get("codec_name")
    # VP9/AV1 direct-play on current Plex apps; re-encoding them on the Pi takes longer than the video itself
    good_video = vcodec in ("h264", "hevc", "vp9", "av1")
    good_audio = acodec in (None, "aac", "ac3", "eac3", "mp3", "flac") or (path.suffix == ".mkv" and acodec in ("opus", "dts", "truehd"))
    good_container = path.suffix.lower() in (".mp4", ".mkv", ".m4v", ".mov", ".webm")
    if (good_video and good_audio and good_container and not keep_mkv and path.suffix.lower() == ".mkv"
            and vcodec in ("h264", "hevc") and acodec in (None, "aac", "mp3", "ac3", "eac3")):
        update(job_id, stage="making it Plex-friendly")
        out = path.with_suffix(".mp4")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                            "-tag:v", "hvc1" if vcodec == "hevc" else "avc1", "-movflags", "+faststart", str(out)],
                           capture_output=True)
        if r.returncode == 0:
            path.unlink()
            return out
        out.unlink(missing_ok=True)
        return path
    if good_video and good_audio and good_container:
        return path
    update(job_id, stage="making it Plex-friendly")
    out = path.with_name(path.stem + ".plex.mkv" if path.suffix == ".mkv" else path.stem + ".plex.mp4")
    cmd = ["nice", "-n", "15", "ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
           "-map", "0:s?", "-c:s", "copy" if out.suffix == ".mkv" else "mov_text"]
    if good_video:
        cmd += ["-c:v", "copy"]
    else:
        height = int(v.get("height") or 1080)
        if height > 1080:
            cmd += ["-vf", "scale=-2:1080"]
            height = 1080
        bitrate = {480: "2M", 720: "4M"}.get(min((480, 720, 1080), key=lambda h: abs(h - height)), "8M")
        # Pi 4 hardware H.264 encoder
        cmd += ["-c:v", "h264_v4l2m2m", "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if good_audio else ["-c:a", "aac", "-b:a", "192k"]
    if out.suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    with heavy_slot("encode", job_id):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and not good_video:
            # Hardware encoder can't take some inputs; fall back to software (slow but works)
            cmd[cmd.index("h264_v4l2m2m")] = "libx264"
            cmd[cmd.index("-b:v"):cmd.index("-b:v") + 2] = ["-preset", "veryfast", "-crf", "22"]
            r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        return path  # keep the original rather than failing the whole job
    path.unlink()
    final = path.with_suffix(out.suffix)
    out.rename(final)
    return final


def grab_frame(video: Path, dest: Path):
    dur = float(ffprobe(video).get("format", {}).get("duration") or 0)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(max(dur * 0.1, 1) if dur else 1), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=640:-2", str(dest)], capture_output=True)
    return dest if dest.exists() else None


def srt_to_text(path: Path):
    text = path.read_text(errors="ignore")
    lines = [l.strip() for l in text.splitlines()
             if l.strip() and not l.strip().isdigit() and "-->" not in l]
    out = []
    for l in lines:  # auto-captions repeat lines a lot
        l = re.sub(r"<[^>]+>", "", l)
        if not out or out[-1] != l:
            out.append(l)
    return "\n".join(out)


_whisper = None
_whisper_lock = threading.Lock()


def transcribe(job_id, media: Path, srt_out: Path | None):
    """Speech-to-text of at most TRANSCRIBE_MAX_MIN minutes of audio. Longer videos are sampled: equal pieces from
    the start, middle and end, which is enough to summarise and keeps it to a few minutes on the Pi.
    Writes a sidecar .srt only when it covered the whole video."""
    global _whisper
    from faster_whisper import WhisperModel
    update(job_id, stage="transcribing speech")
    dur = float(ffprobe(media).get("format", {}).get("duration") or 0)
    limit = TRANSCRIBE_MAX_MIN * 60
    # Decode with ffmpeg ourselves (faster-whisper's PyAV decoding breaks with newer PyAV releases)
    import numpy as np
    def decode(start, length):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(media), "-t", str(length), "-vn",
                               "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    if dur and dur > limit:
        piece = limit / 3
        pcm = b"".join(decode(st, piece) for st in (0, dur / 2 - piece / 2, dur - piece))
    else:
        pcm = decode(0, limit)
    if not pcm:
        return None, None
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    with _whisper_lock, heavy_slot("whisper", job_id):
        if _whisper is None:
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4,
                                    download_root=str(STATE / "models"))
        segments, info = _whisper.transcribe(audio, vad_filter=True, beam_size=1)
        segs = []
        for s in segments:
            check_cancel(job_id)
            segs.append(s)
            if dur:
                update(job_id, progress=round(min(s.end / min(dur, limit), 1) * 100, 1))
    text = "\n".join(s.text.strip() for s in segs)
    if srt_out and segs and dur and dur <= limit:
        def ts(t):
            h, rem = divmod(t, 3600)
            m, s = divmod(rem, 60)
            return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"
        srt = "\n".join(f"{i}\n{ts(s.start)} --> {ts(s.end)}\n{s.text.strip()}\n" for i, s in enumerate(segs, 1))
        srt_out.with_name(f"{srt_out.stem}.{info.language}.srt").write_text(srt)
    note = "" if not dur or dur <= limit else \
        f"[transcript of {TRANSCRIBE_MAX_MIN} sampled minutes (start, middle, end) of a {int(dur // 60)}-minute video]"
    return text, (info.language, note)


# ---------------------------------------------------------------- analysis with an LLM (DeepSeek, OpenAI-compatible API)

LIBRARIES = ["Movies", "TV", "Music", "Videos", "Downloads"]
SUMMARY_LANG = os.environ.get("SUMMARY_LANG", "Simplified Chinese")

# Step 1: a cheap call on the name + metadata only. It files the item and decides whether the
# content is worth transcribing and summarising (talks, tutorials...) or is already described
# well enough by what it is (a film, an episode, a music video...).
CLASSIFY_FIELDS = {
    "title": "the media's own full title, in its original language, used as the file name: keep it as published and only "
             "remove site names, upload IDs and emoji noise; never shorten it to a code or rewrite/summarize it",
    "creator": "who made or performs it: channel/uploader, director, artist or main performer; null if unknown",
    "library": " | ".join(LIBRARIES),
    "folder": " | ".join(VIDEO_FOLDERS),
    "year": "integer or null",
    "show": "string or null",
    "season": "integer or null",
    "episode": "integer or null",
    "artist": "string or null",
    "album": "string or null",
    "language": "language of the media",
    "tags": "3-8 short strings describing the content (topic, genre, people, place); always give some",
    "brief": "1-2 sentences on what this is, from the metadata and what you know about it",
    "needs_transcript": "true only if what is said matters and the metadata doesn't already tell it "
                        "(talks, tutorials, news, interviews, vlogs, documentaries); "
                        "false for movies, TV episodes, music, music videos, short clips, gaming/sports footage",
}
CLASSIFY_SYSTEM = """You file downloaded media into a Plex library.
Libraries:
- Movies: feature films (needs title + year).
- TV: episodes of a TV series (needs show, season, episode).
- Music: audio-only music (artist, album).
- Videos: everything else that is a video (online videos, clips, talks, music videos...). Pick the best folder.
- Downloads: not media at all (software, documents, archives).
`folder` is only used for Videos; set it to "Other" otherwise.
Write `brief` and `tags` in {lang}.
When a tag means the same as one already in the library (listed in the request), use that exact spelling.
Describe adult content plainly and factually like any other content; don't leave fields empty because of it.
Reply with one JSON object with exactly these keys:
{fields}"""

# Step 2, only when step 1 says so: summarise the transcript.
SUMMARY_FIELDS = {
    "summary": "2-4 sentences: what it is about and what is said",
    "key_points": "3-6 short strings",
}
SUMMARY_SYSTEM = """You summarise a video for its owner from its transcript.
Write in {lang}, even when the video is in another language.
Reply with one JSON object with exactly these keys:
{fields}"""


def llm_json(system, user, fields, max_tokens, usage):
    """max_tokens includes the model's reasoning tokens; only tokens actually used are billed."""
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=180,
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                      json={"model": LLM_MODEL, "max_tokens": max_tokens,
                            **({"reasoning_effort": LLM_EFFORT} if LLM_EFFORT else {}),
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": system.format(
                                    lang=SUMMARY_LANG, fields=json.dumps(fields, ensure_ascii=False, indent=1))},
                                {"role": "user", "content": user}]})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    body = r.json()
    u = body.get("usage") or {}
    usage["calls"] = usage.get("calls", 0) + 1
    usage["tokens"] = usage.get("tokens", 0) + (u.get("total_tokens") or 0)
    content = body["choices"][0]["message"].get("content") or ""
    if not content.strip():
        # Happens when reasoning uses up max_tokens or the JSON mode returns nothing
        raise RuntimeError(f"empty reply (finish_reason={body['choices'][0].get('finish_reason')})")
    out = json.loads(content)
    return {k: out.get(k) for k in fields}  # the API only promises valid JSON, not this shape


def as_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def heuristic_analysis(name, meta, guess):
    """Used when there is no API key or the API call fails."""
    a = {"title": meta.get("title") or name, "summary": (meta.get("description") or "").strip()[:400],
         "key_points": [], "tags": meta.get("tags") or [], "language": "", "library": "Videos", "folder": "Other",
         "year": None, "show": None, "season": None, "episode": None, "artist": None, "album": None,
         "needs_transcript": False}
    creator = meta.get("uploader") or meta.get("channel") or meta.get("artist")
    if creator:
        a["creator"] = str(creator)
        a["tags"] = [str(creator)] + [t for t in a["tags"] if t != creator]
    if guess:
        if guess.get("type") == "episode" and guess.get("episode"):
            a.update(library="TV", show=str(guess.get("title")), season=guess.get("season") or 1,
                     episode=guess["episode"] if isinstance(guess["episode"], int) else guess["episode"][0])
        elif guess.get("type") == "movie" and meta.get("source") in ("torrent", "file"):
            a.update(library="Movies", title=str(guess.get("title")), year=guess.get("year"))
    if meta.get("source") == "ytdlp":
        a["folder"] = "Music Videos" if (meta.get("track") or "Music" in (meta.get("categories") or [])) else "Other"
    return a


def classify(job_id, name, meta, guess):
    if not LLM_API_KEY:
        return heuristic_analysis(name, meta, guess)
    update(job_id, stage="classifying")
    meta = {k: v for k, v in meta.items() if v}
    if len(meta.get("description") or "") > 1500:  # the start of a description is enough to classify
        meta["description"] = meta["description"][:1500] + " …[cut]"
    parts = [f"Name: {name}", "Metadata:\n" + json.dumps(meta, ensure_ascii=False, indent=1)]
    known = library_tags()[:200]
    if known:
        parts.append("Tags already in the library: " + "、".join(known))
    if guess:
        parts.append("Filename parser guess:\n" + json.dumps({k: str(v) for k, v in guess.items()}, ensure_ascii=False))
    usage = {}
    for attempt in range(2):
        try:
            a = llm_json(CLASSIFY_SYSTEM, "\n\n".join(parts), CLASSIFY_FIELDS, 4000, usage)
            break
        except Exception as e:
            if attempt:
                a = heuristic_analysis(name, meta, guess)
                a["note"] = f"AI classification failed: {e}"
                return a
    a["title"] = str(a["title"] or meta.get("title") or name)
    a["tags"] = [str(x).strip() for x in a["tags"] if str(x).strip()] if isinstance(a["tags"], list) else []
    if not a["tags"]:  # the model left them out: fall back to the site's own tags
        a["tags"] = [str(t) for t in (meta.get("tags") or [])][:6]
    a["creator"] = str(a.get("creator") or meta.get("uploader") or meta.get("channel") or meta.get("artist") or "").strip() or None
    if a["creator"] and a["creator"] not in a["tags"]:
        a["tags"].insert(0, a["creator"])  # the creator is always a tag, so you can browse/search by it
    a["library"] = a["library"] if a["library"] in LIBRARIES else "Videos"
    a["folder"] = a["folder"] if a["folder"] in VIDEO_FOLDERS else "Other"
    for k in ("year", "season", "episode"):
        a[k] = as_int(a[k])
    a["needs_transcript"] = a["needs_transcript"] is True or str(a["needs_transcript"]).lower() == "true"
    a["summary"], a["key_points"] = str(a.pop("brief") or ""), []
    a["usage"] = usage
    return a


def library_tags():
    """Every tag in use, most used first."""
    counts = {}
    for r in q("SELECT analysis FROM jobs WHERE status='done'"):
        for t in json.loads(r["analysis"] or "{}").get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])


MERGE_SYSTEM = """You tidy the tags of a media library. Find tags that mean the same thing: synonyms, near-synonyms,
translations of each other, different spellings, case or plural variants (e.g. "TED演讲" / "TED Talk" / "TED Talks",
"音乐视频" / "MV" / "官方MV"). Do not merge tags that are merely related (e.g. "心理学" and "拖延症" stay separate).
For each group pick one canonical tag, preferring the {lang} form and the one already used most.
Reply with one JSON object: {{"merge": {{"<tag to replace>": "<canonical tag>", ...}}}} listing only tags that change."""

tag_lock = threading.Lock()


def merge_tags():
    """Fold synonym tags together across the whole library (one cheap LLM call). Returns the mapping applied."""
    if not LLM_API_KEY:
        return {}
    with tag_lock:
        tags = library_tags()
        if len(tags) < 2:
            return {}
        out = llm_json(MERGE_SYSTEM, "Tags (most used first):\n" + "\n".join(tags), {"merge": "object"}, 32000, {})  # room for the model's reasoning over the whole tag list
        mapping = {k: v for k, v in (out.get("merge") or {}).items()
                   if isinstance(k, str) and isinstance(v, str) and k != v and k in tags and v.strip()}
        # follow chains (a -> b -> c) so everything lands on the final canonical tag
        for k in list(mapping):
            seen = {k}
            while mapping[k] in mapping and mapping[k] not in seen:
                seen.add(mapping[k])
                mapping[k] = mapping[mapping[k]]
        if not mapping:
            kv_set("tags_merged", tags)
            return {}
        changed = []
        for r in q("SELECT id, analysis, files FROM jobs WHERE status='done'"):
            a = json.loads(r["analysis"] or "{}")
            old = a.get("tags") or []
            new = list(dict.fromkeys(mapping.get(t, t) for t in old))
            if new != old:
                a["tags"] = new
                update(r["id"], analysis=a)
                vids = [f for f in json.loads(r["files"] or "[]") if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT]
                changed += [(vids[0], a)] if vids else []
        aliases = kv_get("tag_aliases", {})
        aliases.update(mapping)
        kv_set("tag_aliases", aliases)
        kv_set("tags_merged", library_tags())
    if changed:
        finishing_touch(plex_set_metadata, changed)
    return mapping


def merge_tags_if_new():
    """After a download: tidy tags again only if it brought tags the last tidy-up hasn't seen."""
    try:
        if set(library_tags()) - set(kv_get("tags_merged", [])):
            merge_tags()
    except Exception:
        traceback.print_exc()


def summarize(job_id, a, transcript, transcript_note):
    update(job_id, stage="summarizing")
    if len(transcript) > 60_000:  # stay well inside DeepSeek's context window
        transcript_note = (transcript_note + " ").lstrip() + "[transcript cut at 60k characters]"
        transcript = transcript[:60_000]
    user = (f"Title: {a['title']}\nWhat it is: {a['summary']}\n\n"
            f"Transcript {transcript_note}:\n{transcript}")
    try:
        out = llm_json(SUMMARY_SYSTEM, user, SUMMARY_FIELDS, 8000, a.setdefault("usage", {}))
        a["summary"] = str(out["summary"] or a["summary"])
        a["key_points"] = [str(x) for x in out["key_points"]] if isinstance(out["key_points"], list) else []
    except Exception as e:
        a["note"] = f"AI summary failed: {e}"
    return a


# ---------------------------------------------------------------- filing into the library

def destination(a, ext, original_stem):
    title = safe_name(a.get("title") or original_stem)
    lib = a.get("library") or "Videos"
    if lib == "Movies" and a.get("year"):
        d = MEDIA / "Movies" / f"{title} ({a['year']})"
        return d, f"{title} ({a['year']})"
    if lib == "TV" and a.get("show") and a.get("episode") is not None:
        show = safe_name(a["show"])
        season = int(a.get("season") or 1)
        return MEDIA / "TV" / show / f"Season {season:02}", f"{show} - S{season:02}E{int(a['episode']):02}"
    if lib == "Music" and ext in AUDIO_EXT:
        return MEDIA / "Music" / safe_name(a.get("artist") or "Unknown Artist") / safe_name(a.get("album") or "Singles"), title
    if lib == "Downloads":
        return MEDIA / "Downloads", original_stem
    folder = a.get("folder") if a.get("folder") in VIDEO_FOLDERS else "Other"
    return MEDIA / "Videos" / folder, title


def move_with_sidecars(main: Path, dest_dir: Path, base: str):
    """Move a media file plus its subtitles/thumbnail (same stem) into dest_dir as base.*"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(dest_dir / f"{base}{main.suffix}")
    base = target.stem
    moved = [target]
    for side in main.parent.iterdir():
        if side == main or not side.name.startswith(main.stem + "."):
            continue
        rest = side.name[len(main.stem):]  # e.g. ".en.srt", ".jpg"
        if side.suffix.lower() in SUB_EXT | {".jpg", ".png", ".webp"}:
            shutil.move(str(side), str(dest_dir / f"{base}{rest}"))
            moved.append(dest_dir / f"{base}{rest}")
    shutil.move(str(main), str(target))
    return moved


def plex_refresh():
    """Ask Plex to rescan (its file watching doesn't see changes made through the mergerfs pool)."""
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if not token:
        return
    try:
        sections = requests.get("http://127.0.0.1:32400/library/sections", timeout=10,
                                headers={"X-Plex-Token": token, "Accept": "application/json"}).json()
        for d in sections["MediaContainer"].get("Directory", []):
            requests.get(f"http://127.0.0.1:32400/library/sections/{d['key']}/refresh", timeout=10,
                         headers={"X-Plex-Token": token})
    except Exception:
        traceback.print_exc()


def plex_text(a):
    text = a.get("summary") or ""
    if a.get("key_points"):
        text += "\n\n" + "\n".join("• " + p for p in a["key_points"])
    return text.strip()


def plex_set_metadata(items):
    """Write our summary/tags into Plex for videos in the Videos library (Plex has no metadata source
    for those; films and series get Plex's own). items: [(file path, analysis)]. Waits for the scan."""
    token = os.environ.get("PLEX_TOKEN", "").strip()
    items = [(str(f), a) for f, a in items if str(f).startswith(str(MEDIA / "Videos")) and plex_text(a)]
    if not token or not items:
        return
    base, hdrs = "http://127.0.0.1:32400", {"X-Plex-Token": token, "Accept": "application/json"}
    for _ in range(20):  # the scan usually finishes within seconds
        time.sleep(6)
        try:
            sections = requests.get(f"{base}/library/sections", headers=hdrs, timeout=10).json()["MediaContainer"]["Directory"]
            sec = next(d for d in sections if any(l["path"] == str(MEDIA / "Videos") for l in d.get("Location", [])))
            videos = requests.get(f"{base}/library/sections/{sec['key']}/all", headers=hdrs, timeout=30).json()["MediaContainer"].get("Metadata", [])
        except Exception:
            continue
        by_file = {part["file"]: v["ratingKey"] for v in videos for m in v.get("Media", []) for part in m.get("Part", [])}
        pending = []
        for f, a in items:
            key = by_file.get(f)
            if not key:
                pending.append((f, a))
                continue
            params = {"type": 1, "id": key, "summary.value": plex_text(a), "summary.locked": 1}
            if a.get("year"):
                params.update({"year.value": a["year"], "year.locked": 1})
            for i, t in enumerate(a.get("tags", [])[:10]):
                params[f"genre[{i}].tag.tag"] = t
            if a.get("tags"):
                params["genre.locked"] = 1
            requests.put(f"{base}/library/sections/{sec['key']}/all", params=params, headers=hdrs, timeout=10)
        items = pending
        if not items:
            return


# ---------------------------------------------------------------- the pipeline

def process(job_id):
    job = job_dict(q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True))
    url = job["url"]
    workdir = INCOMPLETE / str(job_id)
    # Keep what's already there: after a restart or a failure, yt-dlp and aria2 resume their partial downloads.
    # (Cancelled and removed jobs clean up their folder.)
    workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o2775)
    update(job_id, status="downloading", stage="starting", progress=0, error="")
    meta = {}

    # 1. download
    if url.startswith("magnet:") or url.endswith(".torrent") or url.startswith("torrent-file:"):
        kind = "torrent"
        update(job_id, kind=kind, stage="downloading torrent")
        src = url[len("torrent-file:"):] if url.startswith("torrent-file:") else url
        aria2(job_id, ["--dir", str(workdir), "--seed-time=0", "--bt-stop-timeout=1800", "--follow-torrent=mem",
                       "--bt-remove-unselected-file=true", "--enable-dht=true", "--bt-enable-lpd=true", src], as_bt=True)
        meta["source"] = "torrent"
    else:
        info = ytdlp_probe(url)
        if info:
            kind = "video"
            update(job_id, kind=kind, title=info.get("title") or "")
            meta = ytdlp_download(job_id, url, workdir, info.get("_net"))
            meta["source"] = "ytdlp"
        elif head_is_file(url):
            kind = "file"
            update(job_id, kind=kind, stage="downloading file")
            aria2(job_id, ["--dir", str(workdir), "-x", "8", "-s", "8", "--user-agent", UA,
                           "--content-disposition-default-utf8=true", url])
            meta["source"] = "file"
        else:
            kind = "video"
            update(job_id, kind=kind)
            found = sniff_page(job_id, url)
            if not found:
                raise RuntimeError("No video found on that page")
            check_cancel(job_id)
            update(job_id, title=found["title"])
            urls = found["media_urls"]
            try:
                for i, media_url in enumerate(urls, 1):
                    suffix = f" {i:02}" if len(urls) > 1 else ""
                    extra = {"cookiefile": found["cookiefile"], "http_headers": {"Referer": url, "User-Agent": UA},
                             "outtmpl": str(workdir / f"{safe_name(found['title'])}{suffix}.%(ext)s")}
                    if len(urls) > 1:
                        update(job_id, stage=f"downloading video {i}/{len(urls)}")
                    meta = ytdlp_download(job_id, media_url, workdir, extra)
            finally:
                Path(found["cookiefile"]).unlink(missing_ok=True)
            meta.update(title=found["title"], webpage_url=url, source="sniffed")

    # 2. post-process each media file
    check_cancel(job_id)
    update(job_id, status="processing", stage="inspecting", progress=0, speed="")
    files = sorted((p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but produced no files")
    media = [p for p in files if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT
             and (kind != "torrent" or p.suffix.lower() in AUDIO_EXT  # album tracks are small; only videos get the sample filter
                  or ("sample" not in p.name.lower() and p.stat().st_size > 30 << 20))]

    from guessit import guessit
    results, final_files, plex_items = [], [], []
    thumb_url = ""
    for idx, m in enumerate(media):
        check_cancel(job_id)
        if m.suffix.lower() in VIDEO_EXT:
            m = make_plex_friendly(job_id, m, keep_mkv=kind == "torrent")
        guess = dict(guessit(m.name)) if kind in ("torrent", "file") else {}
        if m.suffix.lower() in VIDEO_EXT and not any(m.parent.glob(glob.escape(m.stem) + ".jpg")):
            grab_frame(m, m.with_suffix(".jpg"))
        if results and not (guess.get("type") == "episode" and results[0].get("library") == "TV"):
            # Extra videos from the same page/torrent: same filing, their own names, no more AI calls
            a = {**results[0], "title": re.sub(r" \[[\w-]+\]$", "", m.stem), "usage": {}}
        elif results:
            # Next episode of a season pack: reuse the show, take the numbers from the file name
            ep = guess.get("episode")
            a = {**results[0], "season": guess.get("season") or results[0].get("season"),
                 "episode": ep if isinstance(ep, int) else (ep or [None])[0], "usage": {}}
        else:
            a = classify(job_id, m.name, {**meta, "file": m.name, "size": human(m.stat().st_size),
                                          "duration_s": float(ffprobe(m).get("format", {}).get("duration") or 0)},
                         guess)
            subs = sorted(m.parent.glob(glob.escape(m.stem) + "*.srt"))
            transcript, note = (srt_to_text(subs[0]), f"(from subtitles {subs[0].name[len(m.stem):]})") if subs else (None, "")
            # Only transcribe and summarise when the classifier says the content is worth it
            if a.get("needs_transcript") and LLM_API_KEY:
                if not transcript:
                    try:
                        transcript, (lang, cut) = transcribe(job_id, m, m)
                        note = f"(speech-to-text, language {lang}) {cut}"
                    except Cancelled:
                        raise
                    except Exception as e:
                        a["note"] = f"transcription failed: {e}"
                if transcript:
                    a = summarize(job_id, a, transcript, note)
            if transcript:
                update(job_id, transcript=transcript[:200_000])
        a.setdefault("tags", [])
        dest_dir, base = destination(a, m.suffix.lower(), m.stem)
        if dest_dir.parent.name == "Videos":
            a["library"] = "Videos"  # e.g. a "movie" without a year can't go in Movies
            a["folder"] = dest_dir.name
        update(job_id, stage="filing into library")
        moved = move_with_sidecars(m, dest_dir, base)
        final_files += [str(p) for p in moved]
        plex_items.append((moved[0], a))
        jpg = next((p for p in moved if p.suffix == ".jpg"), None)
        if jpg and not thumb_url:
            thumb_url = str(jpg)
        results.append(a)

    # Leftovers (non-media torrents/files, extras) go to Downloads as-is
    leftovers = [p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")]
    if leftovers and (not media or kind in ("torrent", "file")):
        root_items = list(workdir.iterdir())
        for item in root_items:
            if item.name.endswith(".aria2"):
                continue
            if not media or any(p.suffix.lower() not in SUB_EXT | {".jpg", ".nfo", ".txt"} for p in
                                ([item] if item.is_file() else item.rglob("*"))):
                target = unique_path(MEDIA / "Downloads" / item.name)
                shutil.move(str(item), str(target))
                final_files.append(str(target))
    shutil.rmtree(workdir, ignore_errors=True)

    analysis = results[0] if results else {"title": meta.get("title") or url, "summary": "", "library": "Downloads"}
    if len(results) > 1:
        analysis = {**results[0], "episodes": len(results)}
    update(job_id, status="done", stage="", progress=100, files=final_files, analysis=analysis,
           title=analysis.get("title") or job["title"], thumb=thumb_url)
    for f in final_files:  # so the list doesn't have to ffprobe it later
        if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT:
            probe_of(f)
    plex_refresh()
    finishing_touch(plex_set_metadata, plex_items)
    finishing_touch(merge_tags_if_new)
    for f in final_files:
        if Path(f).suffix.lower() in (VIDEO_EXT | AUDIO_EXT) - BROWSER_DIRECT:
            finishing_touch(remux_for_browser, Path(f))


def run_job(job_id):
    try:
        process(job_id)
    except Cancelled:
        update(job_id, status="cancelled", stage="", speed="")
        shutil.rmtree(INCOMPLETE / str(job_id), ignore_errors=True)
    except Exception as e:
        traceback.print_exc()
        # Keep the partial download so a retry continues where it stopped. Network trouble is retried
        # automatically a few times (1, 5, then 15 minutes later); errors that won't fix themselves aren't.
        attempts = (q("SELECT attempts FROM jobs WHERE id=?", (job_id,), one=True)["attempts"] or 0) + 1
        permanent = re.search(r"No video found|Unsupported URL|no longer supported|produced no files|404|"
                              r"Private video|removed|not available", str(e), re.I)
        # Being taken for a bot passes after a while: wait longer and try more often
        delays = (1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600) if BOT_CHECK.search(str(e)) or "机器人" in str(e) else (60, 300, 900)
        if attempts <= len(delays) and not permanent:
            delay = delays[attempts - 1]
            wait = f"{delay // 3600} 小时" if delay >= 3600 else f"{delay // 60} 分钟"
            update(job_id, status="failed", stage="", speed="", attempts=attempts, retry_at=time.time() + delay,
                   error=f"{str(e)[:900]}\n（{wait}后自动重试，第 {attempts}/{len(delays)} 次，已下载的部分会保留）")
        else:
            update(job_id, status="failed", stage="", speed="", attempts=attempts, retry_at=None, error=str(e)[:1000])
    finally:
        finish_links(job_id)
        notify(job_id)


download_wakeup = threading.Event()
finishing = []  # job processes past their final status, still doing finishing touches
claim_lock = threading.Lock()


def worker_loop():
    while True:
        with claim_lock:
            # failed jobs whose automatic retry is due go back in the queue
            q("UPDATE jobs SET status='queued', retry_at=NULL WHERE status='failed' AND retry_at IS NOT NULL AND retry_at<=?",
              (time.time(),))
            # links you send go first; followed channels' videos take at most SUB_WORKERS slots, newest first
            # (a job parked waiting for the speech-to-text/encoder slot doesn't count, so downloads keep going
            # meanwhile; one worker always stays free for links you send)
            row = q("SELECT id FROM jobs WHERE status='queued' AND (COALESCE(source,'') NOT LIKE 'sub:%' OR "
                    "((SELECT COUNT(*) FROM jobs WHERE status IN ('downloading','processing') AND source LIKE 'sub:%' "
                    "  AND COALESCE(speed,'') != '排队等资源') < ? AND "
                    " (SELECT COUNT(*) FROM jobs WHERE status IN ('downloading','processing') AND source LIKE 'sub:%') < ?)) "
                    "ORDER BY COALESCE(source,'') LIKE 'sub:%', CASE WHEN source LIKE 'sub:%' THEN -id ELSE id END LIMIT 1",
                    (SUB_WORKERS, DOWNLOAD_WORKERS - 1), one=True)
            if row:
                update(row["id"], status="downloading", stage="starting", cancel=0)
        if not row:
            time.sleep(2)  # the page adds jobs from another process; polling is cheap
            continue
        jid = row["id"]
        # Each job in its own lower-priority process: it gets its own CPU core and can't slow the page
        proc = subprocess.Popen(["nice", "-n", "10", sys.executable, __file__, "run-job", str(jid)])
        # Once the job is done/failed its process may still be busy for minutes with finishing touches (Plex
        # metadata, tag tidy-up, browser copy); don't hold a download slot for that, reap it later
        while proc.poll() is None:
            time.sleep(3)
            if (q("SELECT status FROM jobs WHERE id=?", (jid,), one=True) or {"status": None})["status"] \
                    not in ("downloading", "processing"):
                finishing.append(proc)
                break
        with claim_lock:
            finishing[:] = [p for p in finishing if p.poll() is None]
        status = (q("SELECT status FROM jobs WHERE id=?", (jid,), one=True) or {"status": None})["status"]
        if proc.poll() is not None and proc.returncode != 0 and status in ("downloading", "processing"):
            update(jid, status="failed", stage="", speed="", error=f"任务进程意外退出（代码 {proc.returncode}）")
            finish_links(jid)


# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    files = params.pop("files", None)
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=params if files else None,
                      json=None if files else params, files=files, timeout=70)
    return r.json()


def job_text(j):
    a = j["analysis"]
    icon = {"queued": "⏳", "downloading": "⬇️", "processing": "⚙️", "done": "✅", "failed": "❌", "cancelled": "🚫",
            "linked": "🔗"}.get(j["status"], "•")
    lines = [f"{icon} #{j['id']} {a.get('title') or j['title'] or j['url'][:80]}"]
    if j["status"] in ("downloading", "processing"):
        lines.append(f"{j['stage']} {j['progress']:.0f}% {j['speed']}".strip())
    if j["status"] == "done":
        if a.get("summary"):
            lines.append(a["summary"])
        if a.get("key_points"):
            lines += ["• " + p for p in a["key_points"][:6]]
        if a.get("tags"):
            lines.append(" ".join("#" + re.sub(r"\W+", "_", t).strip("_") for t in a["tags"][:8]))
        if j["files"]:
            lines.append("📁 " + j["files"][0].replace(str(MEDIA), "media"))
        if a.get("note"):
            lines.append("ℹ️ " + a["note"])
    if j["status"] == "failed":
        lines.append(j["error"][:500])
    return "\n".join(lines)


def notify(job_id):
    if not TG_TOKEN:
        return
    try:
        j = job_dict(q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True))
        chat = j["chat_id"] or kv_get("tg_owner")
        if not chat:
            return
        text = job_text(j)
        if j["msg_id"]:
            tg("deleteMessage", chat_id=chat, message_id=j["msg_id"])
        if j["status"] == "done" and j["thumb"] and Path(j["thumb"]).exists() and len(text) <= 1024:
            with open(j["thumb"], "rb") as f:
                tg("sendPhoto", chat_id=chat, caption=text, files={"photo": f})
        else:
            tg("sendMessage", chat_id=chat, text=text[:4096], disable_web_page_preview=True)
    except Exception:
        traceback.print_exc()


def tg_progress_loop():
    """Keep the 'working on it' messages in Telegram up to date."""
    last = {}
    while True:
        time.sleep(15)
        for row in q("SELECT * FROM jobs WHERE status IN ('queued','downloading','processing') AND msg_id IS NOT NULL"):
            j = job_dict(row)
            text = job_text(j)
            if last.get(j["id"]) != text:
                last[j["id"]] = text
                try:
                    tg("editMessageText", chat_id=j["chat_id"], message_id=j["msg_id"], text=text,
                       disable_web_page_preview=True)
                except Exception:
                    pass


def tg_loop():
    offset = kv_get("tg_offset", 0)
    allowed = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED", "").replace(",", " ").split() if x}
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        except Exception:
            time.sleep(10)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            kv_set("tg_offset", offset)
            msg = upd.get("message") or {}
            user = (msg.get("from") or {}).get("id")
            chat = (msg.get("chat") or {}).get("id")
            owner = kv_get("tg_owner")
            if owner is None and not allowed:
                kv_set("tg_owner", chat)  # first person to message the bot owns it
                owner = chat
            if user not in allowed and chat != owner:
                tg("sendMessage", chat_id=chat, text="Sorry, this is a private bot.")
                continue
            try:
                tg_handle(msg, chat)
            except Exception as e:
                traceback.print_exc()
                tg("sendMessage", chat_id=chat, text=f"Error: {e}")


def tg_handle(msg, chat):
    text = msg.get("text") or msg.get("caption") or ""
    doc = msg.get("document")
    if text.startswith("/start") or text.startswith("/help"):
        tg("sendMessage", chat_id=chat, text="Send me links (video pages, files, magnets) or .torrent files. "
           "I'll download them, analyze them and put them in Plex.\n/jobs – recent jobs\n/cancel <id>\n/retry <id>")
        return
    if text.startswith("/jobs"):
        rows = q("SELECT * FROM jobs ORDER BY id DESC LIMIT 10")
        out = "\n".join(job_text(job_dict(r)).split("\n")[0] for r in rows) or "No jobs yet."
        tg("sendMessage", chat_id=chat, text=out)
        return
    m = re.match(r"/(cancel|retry)\s+#?(\d+)", text)
    if m:
        action, jid = m.group(1), int(m.group(2))
        tg("sendMessage", chat_id=chat, text=(cancel_job if action == "cancel" else retry_job)(jid))
        return
    urls = find_urls(text)
    if doc and (doc.get("file_name", "").endswith(".torrent") or doc.get("mime_type") == "application/x-bittorrent"):
        path = save_tg_file(doc["file_id"], doc["file_name"])
        urls.append("torrent-file:" + str(path))
    elif doc or msg.get("video"):
        f = doc or msg["video"]
        if f.get("file_size", 0) > 20 << 20:
            tg("sendMessage", chat_id=chat, text="Telegram only lets bots fetch files up to 20 MB – send a link instead.")
        else:
            path = save_tg_file(f["file_id"], f.get("file_name") or f"telegram-{f['file_unique_id']}.mp4")
            dest = unique_path(MEDIA / "Downloads" / path.name)
            shutil.move(str(path), str(dest))
            tg("sendMessage", chat_id=chat, text=f"Saved to media/Downloads/{dest.name}")
    for u in urls:
        sent = tg("sendMessage", chat_id=chat, text=f"⏳ queued: {u[:200]}", disable_web_page_preview=True)
        add_job(u, source="telegram", chat_id=chat, msg_id=sent.get("result", {}).get("message_id"))
    if not urls and not doc and not msg.get("video") and text:
        tg("sendMessage", chat_id=chat, text="I didn't find a link in that.")


def save_tg_file(file_id, name):
    info = tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=120)
    r.raise_for_status()
    path = STATE / "uploads" / f"{int(time.time())}-{safe_name(name)}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(r.content)
    return path


# ---------------------------------------------------------------- job control

def cancel_job(jid):
    row = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not row:
        return f"No job #{jid}"
    if row["status"] in ("queued", "linked"):
        update(jid, status="cancelled")
        finish_links(jid)
    elif row["status"] in ("downloading", "processing"):
        update(jid, cancel=1)  # the job process sees this within a couple of seconds
    else:
        return f"#{jid} is already {row['status']}"
    return f"Cancelling #{jid}"


def retry_job(jid):
    row = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] not in ("failed", "cancelled"):
        return f"#{jid} can't be retried"
    update(jid, status="queued", error="", progress=0, stage="", ref=None, attempts=0, retry_at=None, cancel=0)
    download_wakeup.set()
    return f"Retrying #{jid}"


# ---------------------------------------------------------------- following channels (追更)
# A link to an uploader's page (a B站 space, a YouTube channel) follows it instead of downloading one video:
# its latest videos are queued right away (SUB_BACKFILL of them, or all with "缓存全部"), and the worker
# checks it again every SUB_INTERVAL seconds (a week) and queues whatever is new, like following a show.
SUB_INTERVAL = int(os.environ.get("SUB_INTERVAL", str(7 * 86400)))  # once a week
SUB_BACKFILL = int(os.environ.get("SUB_BACKFILL", "50"))
SUB_WORKERS = int(os.environ.get("SUB_WORKERS", "2"))  # download slots followed channels may use; the rest stay free for links you send
SUB_SEEN_MAX = 5000


def channel_of(url):
    """(platform, id, URL of the uploader's video list) when `url` is an uploader page, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "space.bilibili.com":
        m = re.match(r"/(\d+)", parts.path)
        if m:
            return "bilibili", m.group(1), f"https://space.bilibili.com/{m.group(1)}/upload/video"
    if host == "youtube.com":
        m = re.match(r"/(@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)", parts.path)
        if m:
            path = urllib.parse.unquote(m.group(1))
            return "youtube", path, f"https://www.youtube.com/{urllib.parse.quote(path, safe='/@')}/videos"
    return None


def add_sub(url, owner, device=None):
    """Follow an uploader. Returns (subscription id, "new" | "duplicate")."""
    platform, cid, videos = channel_of(url)
    key = f"{platform}:{cid.lower()}"
    row = q("SELECT id FROM subs WHERE owner IS ? AND key=?", (owner, key), one=True)
    if row:
        return row["id"], "duplicate"
    with db_lock:
        cur = DB.execute("INSERT INTO subs (owner, platform, key, url, name, backfill, device, created) VALUES (?,?,?,?,?,?,?,?)",
                         (owner, platform, key, videos, cid.removeprefix("@"), SUB_BACKFILL, device, time.time()))
        DB.commit()
    return cur.lastrowid, "new"  # the worker fetches the list within a minute (checked IS NULL)


MIXIN_KEY = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
             12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
             57, 62, 11, 36, 20, 34, 44, 52]


def bili_session():
    """A B站 web session that passes its risk control without logging in: browser cookies (buvid3/4)
    and the WBI key that signs space API requests."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://space.bilibili.com/"})
    s.get("https://www.bilibili.com/", timeout=15)
    spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15).json()["data"]
    s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
    s.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
    img = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()["data"]["wbi_img"]
    raw = "".join(u.rsplit("/", 1)[1].split(".")[0] for u in (img["img_url"], img["sub_url"]))
    s.wbi_key = "".join(raw[i] for i in MIXIN_KEY)[:32]
    return s


def bili_signed(s, params):
    params = {**params, "wts": int(time.time())}
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(params.items())}
    return {**params, "w_rid": hashlib.md5((urllib.parse.urlencode(params) + s.wbi_key).encode()).hexdigest()}


def list_bilibili(mid, limit):
    s = bili_session()
    card = s.get("https://api.bilibili.com/x/web-interface/card", params={"mid": mid}, timeout=15).json()["data"]
    out, total, pn = [], 0, 1
    while limit is None or len(out) < limit:
        params = {"mid": mid, "ps": 30, "pn": pn, "order": "pubdate", "platform": "web", "web_location": 1550101,
                  # canvas/WebGL fingerprint fields the web page sends; without them the API answers -352
                  "dm_img_list": "[]", "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
                  "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBIRCBHcmFwaGljcyBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApR29vZ2xlIEluYy4gKEludGVsKQ",
                  "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
        for attempt in range(3):
            r = s.get("https://api.bilibili.com/x/space/wbi/arc/search", params=bili_signed(s, params), timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"code": r.status_code}
            if body.get("code") == 0:
                break
            time.sleep(3 + attempt * 5)
        else:
            raise RuntimeError(f"B站列表获取失败（{body.get('code')} {body.get('message') or ''}）")
        data = body["data"]
        total = data["page"]["count"]
        vlist = data["list"]["vlist"] or []
        out += [{"url": f"https://www.bilibili.com/video/{v['bvid']}", "title": v["title"]} for v in vlist]
        if not vlist or pn * 30 >= total:
            break
        pn += 1
        time.sleep(1)
    return {"name": card["card"]["name"], "avatar": card["card"]["face"], "total": total,
            "entries": out[:limit] if limit else out}


def list_youtube(url, limit):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    if limit:
        opts["playlistend"] = limit
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [{"url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or ""}
               for e in info.get("entries") or [] if e and e.get("id")]
    avatar = next((t["url"] for t in info.get("thumbnails") or [] if t.get("id") == "avatar_uncropped"), "")
    return {"name": info.get("channel") or info.get("uploader") or "", "avatar": avatar,
            "total": info.get("playlist_count") or (None if limit else len(entries)), "entries": entries}


def list_channel(sub, limit):
    """The uploader's videos, newest first (at most `limit`; None = all)."""
    if sub["platform"] == "bilibili":
        return list_bilibili(sub["key"].split(":", 1)[1], limit)
    return list_youtube(sub["url"], limit)


AVATARS = STATE / "avatars"


def check_sub(sub_id, everything=False):
    """Queue the uploader's videos not seen yet. First check: the newest `backfill` (0 = all);
    later checks only look at the newest 30; everything=True goes through the whole list."""
    sub = q("SELECT * FROM subs WHERE id=?", (sub_id,), one=True)
    if not sub:
        return 0
    first = sub["seen"] in (None, "[]")  # no successful check yet
    limit = None if everything or (first and not sub["backfill"]) else sub["backfill"] if first else 30
    try:
        res = list_channel(sub, limit)
    except Exception as e:
        traceback.print_exc()
        q("UPDATE subs SET checked=?, error=? WHERE id=?", (time.time(), str(e)[:300], sub_id))
        return 0
    seen = json.loads(sub["seen"] or "[]")
    seen_set = set(seen)
    new = [e for e in res["entries"] if link_key(e["url"]) not in seen_set]
    # oldest first, so the newest video gets the highest id and sits at the top of the page
    for e in reversed(new):
        jid, how = add_job_ex(e["url"], source=f"sub:{sub_id}", owner=sub["owner"], device=sub["device"])
        if how == "new" and e["title"]:
            update(jid, title=e["title"])  # the list already has the title: show it while the video waits
    seen = (seen + [link_key(e["url"]) for e in reversed(new)])[-SUB_SEEN_MAX:]
    avatar = sub["avatar"] or ""
    if res["avatar"] and not (AVATARS / f"{sub_id}.jpg").exists():
        try:
            r = requests.get(res["avatar"], timeout=20, headers={"User-Agent": UA})
            if r.ok:
                AVATARS.mkdir(parents=True, exist_ok=True)
                (AVATARS / f"{sub_id}.jpg").write_bytes(r.content)
                avatar = str(AVATARS / f"{sub_id}.jpg")
        except requests.RequestException:
            pass
    total = res["total"] if res["total"] is not None else sub["total"]
    # with a list that's longer than what we fetched, `total` is still how many the uploader has
    q("UPDATE subs SET name=?, avatar=?, total=?, seen=?, checked=?, error='', backfill=? WHERE id=?",
      (res["name"] or sub["name"], avatar, total or len(res["entries"]), json.dumps(seen), time.time(),
       0 if everything else sub["backfill"], sub_id))
    if new:
        print(f"sub {sub_id} {res['name']}: queued {len(new)}")
    return len(new)


def sub_loop():
    """Worker: check followed channels that are due (new ones, refresh requests, and every SUB_INTERVAL)."""
    while True:
        try:
            for r in q("SELECT id, everything FROM subs WHERE checked IS NULL OR checked < ? ORDER BY checked IS NOT NULL, checked",
                       (time.time() - SUB_INTERVAL,)):
                check_sub(r["id"], everything=bool(r["everything"]))
                q("UPDATE subs SET everything=0 WHERE id=?", (r["id"],))
        except Exception:
            traceback.print_exc()
        time.sleep(30)


# ---------------------------------------------------------------- web

HERE = Path(__file__).parent

# Privacy and accounts
# - Every browser gets an anonymous device id cookie; without logging in it only sees its own jobs.
# - Logging in (or registering) ties the browser to an account and moves its jobs there, so an
#   account sees the jobs of all its devices. The "admin" account (GRABBER_PASSWORD) sees everyone's.
# - The iOS shortcut sends no cookies, only the phone's name. The first time a phone uses it, it is
#   matched to the browser that last opened this page from the same IP (the Safari on that phone,
#   where the shortcut was installed), and stays tied to it.
from werkzeug.security import check_password_hash, generate_password_hash

DEVICE_COOKIE = "grabber_device"
NAME_RE = re.compile(r"[\w.\-]{1,32}")
_seen_cache = {}


def secret_key():
    path = STATE / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


def ensure_admin():
    if ADMIN_PASSWORD:
        q("INSERT INTO users (name, pw, admin, created) VALUES ('admin', ?, 1, ?) "
          "ON CONFLICT(name) DO UPDATE SET pw=excluded.pw, admin=1", (generate_password_hash(ADMIN_PASSWORD), time.time()))


def device_label(ua):
    """Short readable name for a browser, e.g. "iPhone · Safari"."""
    ua = ua or ""
    os_name = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                                   ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "设备")
    browser = next((n for k, n in (("Edg/", "Edge"), ("MicroMessenger", "微信"), ("CriOS", "Chrome"), ("FxiOS", "Firefox"),
                                   ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "浏览器")
    return f"{os_name} · {browser}"


def owner_of_device(device_id):
    row = q("SELECT user FROM devices WHERE id=?", (device_id,), one=True)
    return f"user:{row['user']}" if row and row["user"] else device_id


def phone_owner(name):
    """Owner for a job sent by the iOS shortcut from the phone called `name`."""
    row = q("SELECT device FROM phones WHERE name=?", (name,), one=True)
    if not row:
        dev = q("SELECT id FROM devices WHERE ip=? AND seen>? ORDER BY seen DESC LIMIT 1",
                (client_ip(), time.time() - 30 * 86400), one=True)
        if not dev:
            return "shortcut"  # never opened the page on this phone: admin-only
        q("INSERT OR IGNORE INTO phones (name, device, created) VALUES (?,?,?)", (name, dev["id"], time.time()))
        row = q("SELECT device FROM phones WHERE name=?", (name,), one=True)
    return owner_of_device(row["device"])


# From outside, only these work without logging in (everything else needs an account)
PUBLIC_PATHS = ("/", "/api/login", "/api/jobs", "/api/account")
login_failures = {}  # ip -> [failed attempts, first failure time]


def is_external():
    return str(request.environ.get("SERVER_PORT")) == str(EXTERNAL_PORT)


def client_ip():
    # Through the tunnel the peer is 127.0.0.1; Caddy on the VPS passes the real address in
    # X-Forwarded-For, which waitress (trusting 127.0.0.1) turns into the remote address
    return request.remote_addr


@app.before_request
def identify():
    g.external = is_external()
    g.device = request.cookies.get(DEVICE_COOKIE) or ""
    g.new_device = not re.fullmatch(r"[0-9a-f]{32}", g.device)
    if g.new_device:
        g.device = secrets.token_hex(16)
    g.user = session.get("user")
    # The login is a signed cookie, so it is also checked against the devices table: a browser removed from
    # the account (e.g. a lost phone) is logged out on its next request, not only when it logs out itself
    if g.user and (not q("SELECT 1 FROM users WHERE name=?", (g.user,), one=True)
                   or (q("SELECT user FROM devices WHERE id=?", (g.device,), one=True) or {"user": None})["user"] != g.user):
        session.clear()
        g.user = None
    g.admin = bool(g.user and q("SELECT admin FROM users WHERE name=?", (g.user,), one=True)["admin"])
    g.owner = f"user:{g.user}" if g.user else g.device
    if g.external and not g.user:
        # No anonymous device mode on the internet: show the page and the login form, nothing else
        if request.path == "/api/jobs":
            return jsonify(jobs=[], login_required=True, external=True, user=None, admin=False,
                           disk={"free": 0, "total": 0}, features={}, privacy={})
        if request.path == "/api/account":
            return jsonify(user=None, devices=[])
        if request.path not in PUBLIC_PATHS and not request.path.startswith(("/static/", "/play/", "/subs/", "/thumb/")):
            return jsonify(error="login required", login_required=True), 401
    if request.path == "/api/add" and not request.cookies:
        phone = str((request.get_json(silent=True) or {}).get("device", "")).strip()[:60]
        if not phone:  # body that isn't valid JSON (see add_post)
            m = re.search(r'"device"\s*:\s*"([^"]{1,60})"', request.get_data(as_text=True))
            phone = m.group(1) if m else ""
        g.owner = phone_owner(phone) if phone else "shortcut"
        g.device_label = f"📱 {phone}" if phone else "快捷指令"
        g.new_device = False
        return
    g.device_label = device_label(request.headers.get("User-Agent"))
    # Remember where each browser was last seen (throttled; the page polls every 2 s)
    key = (g.device, client_ip())
    if time.time() - _seen_cache.get(key, 0) > 600:
        _seen_cache[key] = time.time()
        q("INSERT INTO devices (id, ip, seen, label) VALUES (?,?,?,?) "
          "ON CONFLICT(id) DO UPDATE SET ip=excluded.ip, seen=excluded.seen, label=excluded.label",
          (g.device, client_ip(), time.time(), g.device_label))


@app.after_request
def remember(resp):
    if getattr(g, "new_device", False):
        resp.set_cookie(DEVICE_COOKIE, g.device, max_age=10 * 365 * 86400, httponly=True, samesite="Lax")
    return resp


def scope_sql():
    return ("1", ()) if g.admin else ("owner = ?", (g.owner,))


def visible(jid):
    row = q("SELECT owner FROM jobs WHERE id=?", (jid,), one=True)
    return bool(row) and (g.admin or row["owner"] == g.owner)


def sign_in(name):
    """Log this browser into `name` and move the jobs it added anonymously into the account."""
    session.permanent = True
    session["user"] = name
    q("INSERT INTO devices (id, user, ip, seen) VALUES (?,?,?,?) "
      "ON CONFLICT(id) DO UPDATE SET user=excluded.user", (g.device, name, client_ip(), time.time()))
    q("UPDATE jobs SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    # followed uploaders too; one the account already follows stays once
    q("DELETE FROM subs WHERE owner=? AND key IN (SELECT key FROM subs WHERE owner=?)", (g.device, f"user:{name}"))
    q("UPDATE subs SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    # bring this device's privacy settings into the account
    dev, acc = privacy_get(g.device), privacy_get(f"user:{name}")
    privacy_set(f"user:{name}", list(dict.fromkeys(acc["tags"] + dev["tags"])), list(dict.fromkeys(acc["ids"] + dev["ids"])))


# ---------------------------------------------------------------- privacy mode

def privacy_get(owner):
    row = q("SELECT tags, ids FROM privacy WHERE owner=?", (owner,), one=True)
    return {"tags": json.loads(row["tags"]), "ids": json.loads(row["ids"])} if row else {"tags": [], "ids": []}


def privacy_set(owner, tags, ids):
    q("INSERT INTO privacy (owner, tags, ids) VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET tags=excluded.tags, ids=excluded.ids",
      (owner, json.dumps(tags, ensure_ascii=False), json.dumps(ids)))


# Privacy unlocks live only in the open page: unlocking returns a token the page keeps in memory and sends
# back as X-Privacy-Token. Reloading the page (or the token expiring) locks everything again.
privacy_tokens = {}  # token -> {"owner", "expires", "reveal"}


def privacy_token():
    tok = privacy_tokens.get(request.headers.get("X-Privacy-Token", ""))
    if tok and tok["owner"] == g.owner and tok["expires"] > time.time():
        return tok
    return None


def revealed():
    tok = privacy_token()
    return bool(tok and tok["reveal"])


def is_hidden(d, prefs):
    # hiding is by tag only (hiding single items by hand was dropped)
    aliases = kv_get("tag_aliases", {})
    hide = {aliases.get(t, t).lower() for t in prefs["tags"]}
    return any(aliases.get(t, t).lower() in hide for t in (d.get("analysis") or {}).get("tags") or [])


@app.post("/api/privacy/unlock")
def privacy_unlock():
    """Open the hidden privacy menu (reached by tapping the avatar three times) with the account password."""
    if not g.user:
        return jsonify(error="先登录账号"), 403
    key = f"privacy:{g.user}"
    fails, since = login_failures.get(key, (0, time.time()))
    if fails >= 5 and time.time() - since < 15 * 60:
        return jsonify(error="尝试次数太多，请 15 分钟后再试"), 429
    row = q("SELECT pw FROM users WHERE name=?", (g.user,), one=True)
    if not row or not check_password_hash(row["pw"], str((request.get_json(silent=True) or {}).get("password", ""))):
        time.sleep(1)
        recent = time.time() - since < 15 * 60
        login_failures[key] = (fails + 1 if recent else 1, since if recent else time.time())
        return jsonify(error="密码不对"), 403
    login_failures.pop(key, None)
    for t in [t for t, v in privacy_tokens.items() if v["expires"] < time.time()]:
        privacy_tokens.pop(t, None)
    token = secrets.token_urlsafe(24)
    privacy_tokens[token] = {"owner": g.owner, "expires": time.time() + 30 * 60, "reveal": False}
    return jsonify(token=token, expires_in=30 * 60)


@app.get("/api/privacy")
def privacy_info():
    if not privacy_token():
        return jsonify(error="locked"), 403
    prefs = privacy_get(g.owner)
    return jsonify(**prefs, revealed=revealed(), library_tags=library_tags()[:300])


@app.post("/api/privacy")
def privacy_update():
    """{"tags": [...]} sets the hidden tags; {"reveal": true/false} shows or hides them in this page."""
    tok = privacy_token()
    if not tok:
        return jsonify(error="locked"), 403
    body = request.get_json(silent=True) or {}
    prefs = privacy_get(g.owner)
    if isinstance(body.get("tags"), list):
        prefs["tags"] = list(dict.fromkeys(str(t).strip()[:40] for t in body["tags"] if str(t).strip()))[:200]
    privacy_set(g.owner, prefs["tags"], [])
    if "reveal" in body:
        tok["reveal"] = bool(body["reveal"])
    return jsonify(ok=True)


def credentials():
    body = request.get_json(silent=True) or {}
    return str(body.get("name", "")).strip(), str(body.get("password", ""))


@app.post("/api/login")
def login():
    ip = client_ip()
    fails, since = login_failures.get(ip, (0, time.time()))
    if fails >= 10 and time.time() - since < 15 * 60:
        return jsonify(error="尝试次数太多，请 15 分钟后再试"), 429
    name, pw = credentials()
    row = q("SELECT pw FROM users WHERE name=?", (name,), one=True)
    if not row or not check_password_hash(row["pw"], pw):
        time.sleep(1)  # slow down guessing
        login_failures[ip] = (fails + 1 if time.time() - since < 15 * 60 else 1,
                              since if time.time() - since < 15 * 60 else time.time())
        return jsonify(error="用户名或密码不对"), 403
    login_failures.pop(ip, None)
    sign_in(name)
    return jsonify(ok=True)


@app.get("/api/name-available")
def name_available():
    name = request.args.get("name", "").strip()
    return jsonify(available=bool(NAME_RE.fullmatch(name)) and not q("SELECT 1 FROM users WHERE name=?", (name,), one=True))


@app.post("/api/register")
def register():
    if g.external:  # accounts are made at home; strangers on the internet can't sign up
        return jsonify(error="只能在家里的网络注册新账号"), 403
    name, pw = credentials()
    if not NAME_RE.fullmatch(name):
        return jsonify(error="用户名：1-32 个字母、数字、汉字或 . - _"), 400
    if len(pw) < 4:
        return jsonify(error="密码至少 4 位"), 400
    try:
        with db_lock:
            DB.execute("INSERT INTO users (name, pw, created) VALUES (?,?,?)",
                       (name, generate_password_hash(pw), time.time()))
            DB.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="这个用户名已被注册"), 409
    sign_in(name)
    return jsonify(ok=True)


@app.get("/api/account")
def account():
    """Devices tied to the logged-in account: browsers that logged in, and iPhones using the shortcut."""
    if not g.user:
        return jsonify(user=None, devices=[])
    devices = []
    for d in q("SELECT * FROM devices WHERE user=? ORDER BY seen DESC", (g.user,)):
        devices.append({"id": d["id"], "label": d["label"] or "设备", "seen": d["seen"], "current": d["id"] == g.device,
                        "phones": [p["name"] for p in q("SELECT name FROM phones WHERE device=?", (d["id"],))]})
    # Jobs from before devices were recorded have no device; JSON keys must be strings
    counts = {r["device"] or "未知设备": r["n"] for r in q("SELECT device, COUNT(*) n FROM jobs WHERE owner=? GROUP BY device",
                                                       (f"user:{g.user}",))}
    return jsonify(user=g.user, devices=devices, counts=counts)


@app.post("/api/devices/<device_id>/remove")
def remove_device(device_id):
    """Untie a browser (and the iPhone shortcut that goes with it) from the account; its jobs stay."""
    if not g.user:
        return jsonify(error="not logged in"), 403
    q("UPDATE devices SET user=NULL WHERE id=? AND user=?", (device_id, g.user))
    if device_id == g.device:
        session.clear()
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    # This browser goes back to being anonymous; the account keeps its jobs
    q("UPDATE devices SET user=NULL WHERE id=?", (g.device,))
    session.clear()
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(HERE / "static", name, max_age=30 * 86400)


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(HERE / "static", "favicon.ico", max_age=30 * 86400, mimetype="image/x-icon")


@app.get("/shortcut")
def shortcut():
    """iOS share-sheet shortcut: share a link from any app and it gets sent to /api/add."""
    return send_from_directory(HERE, "send-to-pi.shortcut", as_attachment=True, download_name="发送到拾光.shortcut")


@app.get("/add")
def add_get():
    """Target for the bookmarklet: /add?url=..."""
    url = request.args.get("url", "")
    if URL_RE.match(url) and channel_of(url):
        add_sub(url, g.owner, g.device_label)
        return f"<meta http-equiv=refresh content='1;url=/'>开始追更 – {url}"
    if URL_RE.match(url):
        jid = add_job(url, source="web", owner=g.owner, device=g.device_label)
        return f"<meta http-equiv=refresh content='1;url=/'>Queued #{jid} – {url}"
    return "No URL", 400


@app.post("/api/add")
def add_post():
    ids = []
    if "torrent" in request.files:
        f = request.files["torrent"]
        path = STATE / "uploads" / f"{int(time.time())}-{safe_name(f.filename)}"
        path.parent.mkdir(exist_ok=True)
        f.save(path)
        ids.append(add_job("torrent-file:" + str(path), owner=g.owner, device=g.device_label))
    body = request.get_json(silent=True) or {}
    raw = "" if body or request.form else request.get_data(as_text=True)
    text = request.form.get("text") or body.get("text", "") or raw
    device = str(body.get("device", "")).strip()[:60]
    if raw and not device:
        # Android's HTTP Shortcuts pastes shared text into a JSON template without escaping it, so a title
        # with quotes breaks the JSON; still take the links and the device name out of the raw body
        m = re.search(r'"device"\s*:\s*"([^"]{1,60})"', raw)
        device = m.group(1) if m else ""
    source = f"shortcut:{device}" if device else "web"
    hows, subs = [], []
    for u in find_urls(text):
        if channel_of(u):  # an uploader's page: follow it instead of downloading the page
            sid, how = add_sub(u, g.owner, g.device_label)
            subs.append({"id": sid, "how": how})
            continue
        jid, how = add_job_ex(u, source=source, owner=g.owner, device=g.device_label)
        ids.append(jid)
        hows.append(how)
    if not ids and subs:
        if not request.cookies:
            return Response(f"开始追更 {len(subs)} 个 UP 主，新视频会自动下载", mimetype="text/plain")
        return jsonify(ids=[], duplicates=[], linked=[], subs=subs)
    if not ids:
        if not request.cookies:
            return Response("没找到链接", mimetype="text/plain", status=400)
        return jsonify(error="No link found"), 400
    dupes = [i for i, h in zip(ids, hows) if h == "duplicate"]
    linked = [i for i, h in zip(ids, hows) if h == "linked"]
    if not request.cookies:
        # iOS shortcut / Android HTTP Shortcuts show the reply as a notification: keep it readable
        new = len(ids) - len(dupes) - len(linked)
        msg = "，".join(x for x in (f"开始追更 {len(subs)} 个 UP 主" if subs else "", f"开始下载 {new} 个" if new else "",
                                    f"{len(linked)} 个别人已经下过，直接加进来了" if linked else "",
                                    f"{len(dupes)} 个已经在拾光里了" if dupes else "") if x)
        return Response(msg, mimetype="text/plain")
    return jsonify(ids=ids, duplicates=dupes, linked=linked, subs=subs)


def owner_label(o):
    o = o or ""
    return o[5:] if o.startswith("user:") else "快捷指令（未识别）" if o == "shortcut" else "匿名设备" if o else "旧任务"


def snippet(text, term, width=60):
    i = text.lower().find(term.lower())
    if i < 0:
        return ""
    start = max(0, i - width)
    return ("…" if start else "") + text[start:i + len(term) + width].replace("\n", " ") + "…"


@app.get("/api/jobs")
def jobs():
    term = request.args.get("q", "").strip()
    term = kv_get("tag_aliases", {}).get(term, term)  # a merged-away tag searches for its canonical form
    scope, scope_args = scope_sql()
    scope += " AND status != 'cancelled'"  # cancelled jobs are hidden
    sub = request.args.get("sub", "")
    limit = 100
    if sub.isdigit():  # one followed uploader's videos
        scope, scope_args, limit = scope + " AND source = ?", (*scope_args, f"sub:{sub}"), 1000
    if term:
        # Searches titles, links, summaries, key points, tags, file paths and transcripts
        like = f"%{term}%"
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND (title LIKE ? OR url LIKE ? OR analysis LIKE ? OR files LIKE ? "
                 "OR transcript LIKE ?) ORDER BY id DESC LIMIT ?", (*scope_args, *(like,) * 5, max(limit, 200)))
        out = []
        for r in rows:
            d = job_dict(r)
            if term.lower() not in json.dumps(d, ensure_ascii=False).lower():
                d["match"] = snippet(r["transcript"] or "", term)
            out.append(d)
    else:
        out = [job_dict(r) for r in q(f"SELECT * FROM jobs WHERE {scope} ORDER BY id DESC LIMIT ?", (*scope_args, limit))]
    # privacy mode: hidden items aren't even sent unless they've been revealed with the password
    prefs, show_hidden = privacy_get(g.owner), revealed()
    hidden_count = 0
    kept = []
    for d in out:
        if is_hidden(d, prefs):
            hidden_count += 1
            if not show_hidden:
                continue
            d["hidden"] = True
        kept.append(d)
    out = kept
    for d in out:
        if d["status"] == "linked" and d.get("ref"):  # show the original download's progress
            src = q("SELECT status, stage, progress, speed, title, thumb FROM jobs WHERE id=?", (d["ref"],), one=True)
            if src:
                d.update(status=src["status"] if src["status"] in ("queued", "downloading", "processing") else "queued",
                         stage=src["stage"], progress=src["progress"], speed=src["speed"], title=d["title"] or src["title"])
        owner = d.pop("owner", None)
        d["media"] = media_info(d) if d["status"] == "done" else []
        d["missing"] = d["status"] == "done" and not d["media"] and any(
            Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT for f in d["files"])
        if g.admin:
            d["owner_label"] = owner_label(owner)
    usage = shutil.disk_usage(MEDIA)
    return jsonify(jobs=out, disk={"free": usage.free, "total": usage.total},
                   features={"ai": bool(LLM_API_KEY), "telegram": bool(TG_TOKEN)}, admin=g.admin, user=g.user,
                   privacy={"revealed": show_hidden}, external=g.external,  # no hidden counts on purpose
                   subs=subs_list(), sub_interval=SUB_INTERVAL)


def subs_list():
    """Followed uploaders visible here, with how many of their videos are downloaded / waiting."""
    where, args = ("1", ()) if g.admin else ("owner = ?", (g.owner,))
    counts = {}
    for r in q("SELECT source, status, COUNT(*) n FROM jobs WHERE source LIKE 'sub:%' GROUP BY source, status"):
        c = counts.setdefault(r["source"], {})
        c[r["status"]] = c.get(r["status"], 0) + r["n"]
    out = []
    for r in q(f"SELECT * FROM subs WHERE {where} ORDER BY id DESC", args):
        c = counts.get(f"sub:{r['id']}", {})
        out.append({"id": r["id"], "platform": r["platform"], "name": r["name"], "url": r["url"],
                    "avatar": bool(r["avatar"]), "total": r["total"], "backfill": r["backfill"],
                    "checked": r["checked"], "error": r["error"], "pending": r["checked"] is None or bool(r["everything"]),
                    "done": c.get("done", 0), "active": sum(c.get(k, 0) for k in ("queued", "downloading", "processing", "linked")),
                    "failed": c.get("failed", 0), "next": (r["checked"] or time.time()) + SUB_INTERVAL,
                    **({"owner_label": owner_label(r["owner"])} if g.admin else {})})
    return out


def sub_visible(sid):
    row = q("SELECT owner FROM subs WHERE id=?", (sid,), one=True)
    return bool(row) and (g.admin or row["owner"] == g.owner)


@app.post("/api/subs/<int:sid>/<action>")
def sub_action(sid, action):
    """refresh: check for new videos now · all: download every video of the uploader · delete: stop following
    (videos already downloaded stay; ones still waiting in the queue are dropped)"""
    if not sub_visible(sid):
        return jsonify(error="not found"), 404
    if action == "refresh":
        q("UPDATE subs SET checked=NULL WHERE id=?", (sid,))
    elif action == "all":
        q("UPDATE subs SET everything=1, checked=NULL WHERE id=?", (sid,))
    elif action == "delete":
        q("DELETE FROM subs WHERE id=?", (sid,))
        for r in q("SELECT id FROM jobs WHERE source=? AND status='queued'", (f"sub:{sid}",)):
            cancel_job(r["id"])
        (AVATARS / f"{sid}.jpg").unlink(missing_ok=True)
    else:
        return jsonify(error="unknown action"), 400
    return jsonify(ok=True)


@app.get("/subavatar/<int:sid>")
def sub_avatar(sid):
    row = q("SELECT avatar FROM subs WHERE id=?", (sid,), one=True)
    if not row or not row["avatar"] or not Path(row["avatar"]).exists():
        return "", 404
    return send_file(row["avatar"], max_age=86400)


def delete_job_files(row):
    """Delete a job's files from disk: the media, its subtitles/cover, and the browser copy.
    Only paths inside the library are touched; empty folders it leaves behind are removed."""
    removed = 0
    for f in json.loads(row["files"] or "[]"):
        p = Path(f)
        try:
            p.resolve().relative_to(MEDIA.resolve())
        except ValueError:
            continue
        if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT and p.exists():
            st = p.stat()
            (PLAY_CACHE / f"{hashlib.sha1(f'{p}:{st.st_mtime}'.encode()).hexdigest()}.mp4").unlink(missing_ok=True)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        elif p.exists():
            p.unlink()
            removed += 1
        # Tidy empty folders, but keep the library's own folders (Movies, TV, Videos/Talks...)
        parent = p.parent
        while (parent != MEDIA and parent.parent != MEDIA and parent.parent != MEDIA / "Videos"
               and parent.exists() and not any(parent.iterdir())):
            parent.rmdir()
            parent = parent.parent
    return removed


def sweep_orphans():
    """Covers and subtitles left behind when a video was deleted some other way (Plex, Samba, Finder).
    Only looks in Videos/, which this app manages; Movies/TV/Music may hold Plex's own artwork."""
    removed = []
    for folder in [p for p in (MEDIA / "Videos").rglob("*") if p.is_dir()] + [MEDIA / "Videos"]:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        stems = [e.stem for e in entries if e.is_file() and e.suffix.lower() in VIDEO_EXT | AUDIO_EXT]
        for e in entries:
            if e.is_file() and e.suffix.lower() in SUB_EXT | {".jpg", ".png", ".webp"} \
                    and not any(e.name.startswith(st + ".") for st in stems):
                e.unlink(missing_ok=True)
                removed.append(str(e))
    return removed


def sweep_loop():
    while True:
        try:
            for f in sweep_orphans():
                print("removed orphan", f)
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


def remove_job(jid, with_files):
    """Remove a job from the list (stopping it first); optionally delete its files too."""
    cancel_job(jid)
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row:
        return "gone", 0
    if row["status"] in ("downloading", "processing"):
        return "stopping", 0
    shared = [r for r in q("SELECT id, files FROM jobs WHERE id != ? AND status NOT IN ('failed','cancelled')", (jid,))
              if set(json.loads(r["files"] or "[]")) & set(json.loads(row["files"] or "[]"))]
    # Files another account still uses stay on disk; only this entry goes
    files = delete_job_files(row) if with_files and not shared else 0
    shutil.rmtree(INCOMPLETE / str(jid), ignore_errors=True)  # any partial download
    q("DELETE FROM jobs WHERE id=?", (jid,))
    q("UPDATE jobs SET ref=NULL WHERE ref=?", (jid,))
    if with_files and shared:
        return "kept_shared", 0
    return "removed", files


@app.post("/api/jobs/<int:jid>/<action>")
def job_action(jid, action):
    if not visible(jid):
        return jsonify(error="not found"), 404
    if action == "cancel":
        return jsonify(msg=cancel_job(jid))
    if action == "retry":
        return jsonify(msg=retry_job(jid))
    if action == "delete":
        with_files = bool((request.get_json(silent=True) or {}).get("files"))
        result, files = remove_job(jid, with_files)
        if files:
            plex_refresh()
        return jsonify(msg="removed from list" if result != "stopping" else "still stopping; remove it again in a moment",
                       result=result, files=files)
    return jsonify(error="unknown action"), 400


@app.post("/api/tags/merge")
def tags_merge():
    if not g.admin:
        return jsonify(error="admin only"), 403
    return jsonify(merged=merge_tags())


@app.post("/api/jobs/remove")
def remove_many():
    """Batch removal: {"ids": [...], "files": true/false}"""
    body = request.get_json(silent=True) or {}
    with_files = bool(body.get("files"))
    out = {"removed": 0, "stopping": 0, "files": 0}
    for jid in body.get("ids", [])[:500]:
        if isinstance(jid, int) and visible(jid):
            result, files = remove_job(jid, with_files)
            out[result] = out.get(result, 0) + 1
            out["files"] += files
    if out["files"]:
        plex_refresh()
    return jsonify(out)


# ---------------------------------------------------------------- watching in the browser

BROWSER_DIRECT = {".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}


def playable(job):
    """Media files of a finished job the page can play, with their subtitle files."""
    out = []
    for f in job["files"]:
        p = Path(f)
        if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT and p.exists():
            subs = [str(x) for x in sorted(p.parent.glob(glob.escape(p.stem) + ".*"))
                    if x.suffix.lower() in (".srt", ".vtt")]
            out.append({"path": f, "subs": subs})
    return out


_probes = {}


def probe_of(path):
    """(duration in seconds, video codec) of a media file. ffprobe takes a second or more per file on the Pi's
    disks, and the list shows a hundred of them, so results are kept in the database (by path + modification
    time) and survive restarts; the first page load after a restart used to take minutes."""
    key = (path, Path(path).stat().st_mtime)
    if key in _probes:
        return _probes[key]
    row = q("SELECT duration, vcodec FROM probes WHERE path=? AND mtime=?", key, one=True)
    if row:
        _probes[key] = (row["duration"], row["vcodec"])
        return _probes[key]
    info = ffprobe(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic") != 1), {})
    _probes[key] = (float(info.get("format", {}).get("duration") or 0), v.get("codec_name"))
    q("INSERT OR REPLACE INTO probes (path, mtime, duration, vcodec) VALUES (?,?,?,?)", (*key, *_probes[key]))
    return _probes[key]


def duration_of(path):
    return probe_of(path)[0]


def vcodec_of(path):
    return probe_of(path)[1]


def warm_probes():
    """Web start-up: probe any finished file not in the database yet, in the background."""
    for r in q("SELECT files FROM jobs WHERE status='done'"):
        for f in json.loads(r["files"] or "[]"):
            try:
                if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT and Path(f).exists():
                    probe_of(f)
            except Exception:
                pass


def media_token(jid, n, days=7):
    """Signed, expiring token for /play, /subs and /thumb URLs. iOS Safari plays video through a separate
    media process that doesn't reliably send the page's cookies, so these URLs carry their own permission."""
    exp = int(time.time()) + days * 86400
    sig = hmac.new(app.secret_key.encode(), f"{jid}:{n}:{exp}".encode(), "sha256").hexdigest()[:24]
    return f"{exp}.{sig}"


def token_ok(jid, n):
    try:
        exp, sig = request.args.get("t", "").split(".")
        good = hmac.new(app.secret_key.encode(), f"{jid}:{n}:{exp}".encode(), "sha256").hexdigest()[:24]
        return int(exp) > time.time() and hmac.compare_digest(sig, good)
    except ValueError:
        return False


def media_info(job):
    jid = job["id"]
    return [{"name": Path(m["path"]).stem, "audio": Path(m["path"]).suffix.lower() in AUDIO_EXT,
             "duration": duration_of(m["path"]), "vcodec": vcodec_of(m["path"]),
             "src": f"/play/{jid}/{i}?t={media_token(jid, i)}",
             "subsrc": [f"/subs/{jid}/{i}/{k}?t={media_token(jid, i)}" for k in range(len(m["subs"]))],
             "subs": [Path(x).name[len(Path(m["path"]).stem) + 1:].rsplit(".", 1)[0] or "字幕" for x in m["subs"]]}
            for i, m in enumerate(playable(job))]


def job_media(jid, n):
    if not (token_ok(jid, n) or visible(jid)):
        return None
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    items = playable(job_dict(row))
    return items[n] if 0 <= n < len(items) else None


@app.get("/play/<int:jid>/<int:n>")
def play(jid, n):
    m = job_media(jid, n)
    if not m:
        return "", 404
    path = Path(m["path"])
    if path.suffix.lower() in BROWSER_DIRECT:
        return send_file(path, conditional=True)  # supports Range requests, so seeking works
    # Browsers can't open MKV/AVI/TS (or APE/WMA): repackage once as MP4 into a cache and serve that with
    # Range support (iPhones refuse video without it). Video is copied; only odd audio gets re-encoded.
    cached = remux_for_browser(path)
    if cached:
        return send_file(cached, conditional=True)
    return "", 415


PLAY_CACHE = MEDIA / ".cache" / "play"


@contextlib.contextmanager
def remux_lock(out: Path):
    """The web process (someone pressed play) and a job process (just finished) may both convert the same file:
    a file lock keeps them from writing the same output at once (16 lock files, picked by the hash)."""
    (STATE / "locks").mkdir(parents=True, exist_ok=True)
    with open(STATE / "locks" / f"remux-{out.name[0]}.lock", "w") as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        yield


def remux_for_browser(path: Path):
    st = path.stat()
    out = PLAY_CACHE / f"{hashlib.sha1(f'{path}:{st.st_mtime}'.encode()).hexdigest()}.mp4"
    if out.exists():
        return out
    with remux_lock(out):
        if out.exists():
            return out
        PLAY_CACHE.mkdir(parents=True, exist_ok=True)
        audio_only = path.suffix.lower() in AUDIO_EXT
        tmp = out.with_suffix(".part.mp4")
        cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-v", "error", "-i", str(path)]
        cmd += ["-vn", "-map", "0:a:0"] if audio_only else ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
        acodec = next((x.get("codec_name") for x in ffprobe(path).get("streams", []) if x.get("codec_type") == "audio"), None)
        cmd += ["-c:a", "copy"] if acodec in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(out)
    # Keep the cache from growing forever: drop the oldest beyond 50 GB
    files = sorted(PLAY_CACHE.glob("*.mp4"), key=lambda p: p.stat().st_atime)
    while files and sum(f.stat().st_size for f in files) > 50 << 30:
        files.pop(0).unlink(missing_ok=True)
    return out


@app.get("/subs/<int:jid>/<int:n>/<int:k>")
def subs(jid, n, k):
    m = job_media(jid, n)
    if not m or not 0 <= k < len(m["subs"]):
        return "", 404
    text = Path(m["subs"][k]).read_text(errors="ignore")
    if not text.startswith("WEBVTT"):  # SRT -> WebVTT: header + dot as the millisecond separator
        text = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", text.replace("\r", ""))
    return Response(text, mimetype="text/vtt")


@app.get("/thumb/<int:jid>")
def thumb(jid):
    row = q("SELECT thumb, files FROM jobs WHERE id=?", (jid,), one=True)
    if not (token_ok(jid, 0) or visible(jid)) or not row:
        return "", 404
    thumb = row["thumb"]
    if not thumb or not Path(thumb).exists():
        # the file was renamed or moved: use the cover that sits with the media now, and remember it
        thumb = next((f for f in json.loads(row["files"] or "[]") if f.endswith(".jpg") and Path(f).exists()), None)
        if not thumb:
            return "", 404
        q("UPDATE jobs SET thumb=? WHERE id=?", (thumb, jid))
    return send_file(thumb, max_age=3600)


def setup_app():
    app.secret_key = secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      MAX_CONTENT_LENGTH=10 << 20)  # uploads are only .torrent files
    app.permanent_session_lifetime = 365 * 86400


def main_web():
    init_db()
    setup_app()
    ensure_admin()
    threading.Thread(target=warm_probes, daemon=True).start()
    from waitress import serve
    serve(app, listen=f"0.0.0.0:{PORT} 127.0.0.1:{EXTERNAL_PORT}", threads=16, channel_timeout=300,
          ident="shiguang", trusted_proxy="127.0.0.1", trusted_proxy_count=1,
          trusted_proxy_headers="x-forwarded-for", clear_untrusted_proxy_headers=True)


def main_worker():
    init_db(reset=True)
    setup_app()
    INCOMPLETE.mkdir(parents=True, exist_ok=True)
    for _ in range(DOWNLOAD_WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=sweep_loop, daemon=True).start()
    threading.Thread(target=sub_loop, daemon=True).start()
    if TG_TOKEN:
        threading.Thread(target=tg_loop, daemon=True).start()
        threading.Thread(target=tg_progress_loop, daemon=True).start()
    while True:
        time.sleep(3600)


def main_run_job(job_id):
    init_db()
    setup_app()
    run_job(job_id)
    # finishing touches run in threads: let them complete (at most 10 minutes in all). Only our own threads:
    # libraries leave pool threads around that never end, and waiting on each of those kept the process for an hour
    deadline = time.time() + 600
    for t in finishing_threads:
        t.join(timeout=max(0, deadline - time.time()))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"
    if mode == "worker":
        main_worker()
    elif mode == "run-job":
        main_run_job(int(sys.argv[2]))
    else:
        main_web()
