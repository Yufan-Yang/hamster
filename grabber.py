#!/opt/grabber/venv/bin/python
"""Grabber: download anything sent from the web page or Telegram, analyze it, file it for Plex.

Sources: video sites (yt-dlp), pages with an embedded player (headless Chromium sniffing),
direct file URLs (aria2) and magnets/.torrent files (aria2 as user `bt`, which bypasses the proxy).
After download: make it Plex-friendly, get a transcript (subtitles or Whisper),
ask an LLM (DeepSeek) for a summary + category, and move it into the media library.
"""
import functools
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
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

import requests
from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session

MEDIA = Path(os.environ.get("MEDIA_ROOT", "/mnt/media"))
INCOMPLETE = MEDIA / ".incomplete"
# 随记 files live on the disks, not the SD card; the folder is private to the grabber user (the Samba share is public)
NOTES_DIR = MEDIA / ".notes"
NOTE_MAX_UPLOAD = 4 << 30  # one note's files at most (phone videos are big)
STATE = Path(os.environ.get("STATE_DIR", "/var/lib/grabber"))
# On the Pi the database lives on a hard disk (DB_PATH=/mnt/disk1/.grabber/grabber.db): it's written all day, which
# wears out an SD card; the disks spin anyway
DB_PATH = Path(os.environ.get("DB_PATH") or STATE / "grabber.db")
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
# One pass per piece of audio: no retries at higher temperatures and no carrying text over (with music or
# noise those made Whisper decode the same audio again and again: 6 minutes took 40 on the Pi)
WHISPER_FAST = {"temperature": 0.0, "condition_on_previous_text": False}
NOTE_WHISPER_MODEL = os.environ.get("NOTE_WHISPER_MODEL", "small")  # 随记 voice clips are short: a better model is affordable
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


# ---------------------------------------------------------------- wake-ups between processes
#
# The web page, the worker and the job processes share the database; when one changes something another one is
# waiting for (a job queued, a task published, a download finished), it rings a bell instead of the others asking
# the database every few seconds. A bell is a datagram to a UNIX socket each long-running process listens on;
# waiting is with a timeout, so a lost ring only means a short delay.

WAKE_DIR = STATE / "wake"
_bell = threading.Condition()
_rings = {}


def ring(*topics):
    """Something about `topics` ("jobs", "tasks") changed: wake whoever waits on it, in any process."""
    import socket
    msg = ",".join(topics).encode()
    with _bell:  # this process too
        for t in topics:
            _rings[t] = _rings.get(t, 0) + 1
        _bell.notify_all()
    for f in WAKE_DIR.glob("*.sock") if WAKE_DIR.exists() else []:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(msg, str(f))
        except OSError:
            pass  # that process isn't running


def listen_bell(name):
    """Hear rings from other processes (call once per long-running process)."""
    import socket
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    path = WAKE_DIR / f"{name}-{os.getpid()}.sock"
    for old in WAKE_DIR.glob(f"{name}-*.sock"):
        old.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))

    def loop():
        while True:
            topics = sock.recv(256).decode(errors="ignore").split(",")
            with _bell:
                for t in topics:
                    _rings[t] = _rings.get(t, 0) + 1
                _bell.notify_all()
    threading.Thread(target=loop, daemon=True).start()


def bell_mark(topic):
    """Note where the rings are before looking, so a ring that comes while looking isn't missed."""
    with _bell:
        return _rings.get(topic, 0)


def bell_wait(topic, mark, timeout):
    with _bell:
        _bell.wait_for(lambda: _rings.get(topic, 0) != mark, timeout)


# ---------------------------------------------------------------- database

def log_usage(kind, purpose="", job_id=None, amount=0, tokens_in=0, tokens_out=0, seconds=0, cost=None, cache_hit=0):
    try:
        q("INSERT INTO usage (ts, kind, purpose, job_id, amount, tokens_in, tokens_out, seconds, cost, cache_hit) "
          "VALUES (?,?,?,?,?,?,?,?,?,?)",
          (time.time(), kind, purpose, job_id, amount, tokens_in, tokens_out, round(seconds, 1), cost, cache_hit))
    except Exception:
        traceback.print_exc()  # bookkeeping must never break a job


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # readers and the writer in other processes don't block each other
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")  # with WAL: still consistent after a power cut, far fewer syncs
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
    -- 随记: everyday notes (text + photos / videos / voice); `media` = JSON list of attached files, `pending` = the
    -- worker still has to make video posters and transcribe speech (so voice notes can be searched)
    CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, text TEXT DEFAULT '',
        media TEXT DEFAULT '[]', pending INTEGER DEFAULT 0, device TEXT, created REAL, updated REAL);
    CREATE INDEX IF NOT EXISTS notes_owner ON notes (owner, id);
    CREATE INDEX IF NOT EXISTS notes_when ON notes (owner, created);
    -- what the box did, for 资源使用: LLM calls (tokens), speech-to-text, encodes, reading pictures, downloads
    CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, purpose TEXT, job_id INTEGER,
        amount REAL DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, seconds REAL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
    -- network traffic per day: Xray outbounds (every device going through the Pi) and the Pi's own Wi-Fi
    CREATE TABLE IF NOT EXISTS traffic (day TEXT, name TEXT, bytes INTEGER DEFAULT 0, PRIMARY KEY (day, name));
    -- search index, filled in idle time: timed lines (subtitle cues, text read off covers and photos) ...
    CREATE TABLE IF NOT EXISTS seg (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref INTEGER, part INTEGER DEFAULT 0,
        t REAL, src TEXT, text TEXT);
    CREATE INDEX IF NOT EXISTS seg_ref ON seg (kind, ref);
    -- ... and what pictures look like (CLIP vectors of covers, note photos, video frames every few seconds);
    -- `base` = the picture's average likeness to everyday words, so "looks like X" means clearly above that
    CREATE TABLE IF NOT EXISTS vec (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref INTEGER, part INTEGER DEFAULT 0,
        t REAL, src TEXT, base REAL, v BLOB);
    CREATE INDEX IF NOT EXISTS vec_ref ON vec (kind, ref);
    -- the task board (see "the task board"): one row per kind of work and target, e.g. transcribe job:12:0
    CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
        priority INTEGER DEFAULT 0, state TEXT DEFAULT 'queued', payload TEXT DEFAULT '{}', progress TEXT, result TEXT,
        worker TEXT, lease_until REAL, attempts INTEGER DEFAULT 0, not_before REAL, error TEXT DEFAULT '',
        parent INTEGER, published_by TEXT, created REAL, updated REAL, UNIQUE (kind, target));
    CREATE INDEX IF NOT EXISTS tasks_queue ON tasks (state, priority, id);
    -- who claims tasks and what they can do
    CREATE TABLE IF NOT EXISTS workers (name TEXT PRIMARY KEY, caps TEXT, seen REAL, task INTEGER, paused TEXT);
    -- same content uploaded twice, or a clip of a longer video: per job a fingerprint (MinHash of what's said,
    -- mean of its frames), and the pairs found
    CREATE TABLE IF NOT EXISTS fingerprints (job_id INTEGER PRIMARY KEY, minhash BLOB, shingles INTEGER,
        frames BLOB, nframes INTEGER, duration REAL, updated REAL);
    CREATE TABLE IF NOT EXISTS similar (a INTEGER, b INTEGER, kind TEXT, a_in_b REAL, b_in_a REAL, updated REAL,
        PRIMARY KEY (a, b));
    -- 追更周报: per account, what the followed uploaders put out in a week, summarised
    CREATE TABLE IF NOT EXISTS digests (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, start TEXT, end TEXT,
        body TEXT, created REAL);
    -- where each account (or anonymous browser) is in each video: 继续观看 on every device, 已看完
    CREATE TABLE IF NOT EXISTS watch (owner TEXT, job_id INTEGER, part INTEGER DEFAULT 0, pos REAL, dur REAL,
        done INTEGER DEFAULT 0, updated REAL, PRIMARY KEY (owner, job_id));
    -- temperatures every 5 minutes (CPU, each disk), for "highest in the last day" in 资源使用
    CREATE TABLE IF NOT EXISTS health (ts REAL, cpu REAL, disks TEXT);
    -- DeepSeek balance over time: drops are what was really charged
    CREATE TABLE IF NOT EXISTS balance (ts REAL, currency TEXT, total REAL);
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
    if "paused" not in [r[1] for r in DB.execute("PRAGMA table_info(workers)")]:
        DB.execute("ALTER TABLE workers ADD COLUMN paused TEXT")  # why a worker isn't taking tasks (a game...)
    ucols = [r[1] for r in DB.execute("PRAGMA table_info(usage)")]
    if "cost" not in ucols:
        DB.execute("ALTER TABLE usage ADD COLUMN cost REAL")  # CNY, LLM calls only
        DB.execute("ALTER TABLE usage ADD COLUMN cache_hit INTEGER DEFAULT 0")  # input tokens served from DeepSeek's cache
        for r in DB.execute("SELECT id, ts, tokens_in, tokens_out FROM usage WHERE kind='llm' AND purpose != 'earlier'").fetchall():
            # before costs were kept: priced as if no input came from the cache (an upper bound)
            DB.execute("UPDATE usage SET cost=?, cache_hit=-1 WHERE id=?", (llm_cost(LLM_MODEL, 0, r[2], r[3], r[1]), r[0]))
    if "segments" not in cols:
        DB.execute("ALTER TABLE jobs ADD COLUMN segments TEXT")  # speech-to-text lines with their times
    if "backfill" not in cols:
        # an older video queued when following an uploader: speech-to-text waits for idle time
        DB.execute("ALTER TABLE jobs ADD COLUMN backfill INTEGER DEFAULT 0")
        DB.execute("UPDATE jobs SET backfill=1 WHERE status IN ('queued','downloading','processing','failed') AND source LIKE 'sub:%'")
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
    ring("jobs")
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
            ring("jobs")


def job_dict(row):
    d = dict(row)
    d["files"] = json.loads(d["files"] or "[]")
    d["analysis"] = json.loads(d["analysis"] or "{}")
    d.pop("transcript", None)  # only used for search; can be long
    d.pop("segments", None)
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
    last = [0.0]

    def hook(d):
        check_cancel(job_id)
        # yt-dlp reports many times a second; the page polls every few seconds. Every write lands on the SD card
        if d["status"] == "downloading" and time.time() - last[0] >= 3:
            last[0] = time.time()
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
        # ai-zh / ai-en: B站's own AI subtitles (only offered with a logged-in cookies.txt); saves speech-to-text
        "subtitleslangs": ["en", "en-orig", "en-US", "en-GB", "zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh", "ja", ".*-orig",
                           "ai-zh", "ai-en"],
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
    tail, cancelled, last = [], False, 0.0
    for line in proc.stdout:
        tail = (tail + [line.strip()])[-15:]
        m = ARIA_PROGRESS.search(line)
        if m and time.time() - last >= 3:  # aria2 prints every second: write every 3 s at most
            last = time.time()
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


def transcribe(job_id, media: Path, srt_out: Path | None, prompt=None, model=None, purpose="job"):
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
    # (offset in the decoded audio, time in the media) of each piece, to give segments their real times
    spans = []
    if dur and dur > limit:
        piece = limit / 3
        chunks = [(st, decode(st, piece)) for st in (0, dur / 2 - piece / 2, dur - piece)]
    else:
        chunks = [(0, decode(0, limit))]
    at = 0
    for st, chunk in chunks:
        spans.append((at, st))
        at += len(chunk) / 32000  # 16 kHz, 16-bit mono
    pcm = b"".join(c for _, c in chunks)
    if not pcm:
        return None, None
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    started = time.time()
    with _whisper_lock, heavy_slot("whisper", job_id):
        if _whisper is None:
            _whisper = WhisperModel(model or WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4,
                                    download_root=str(STATE / "models"))
        segments, info = _whisper.transcribe(audio, vad_filter=True, beam_size=1, initial_prompt=prompt, **WHISPER_FAST)
        segs = []
        for s in segments:
            check_cancel(job_id)
            segs.append(s)
            if dur:
                update(job_id, progress=round(min(s.end / min(dur, limit), 1) * 100, 1))
    text = "\n".join(s.text.strip() for s in segs)
    log_usage("whisper", purpose, job_id, amount=len(audio) / 16000, seconds=time.time() - started)
    def real(t):
        off, st = max((sp for sp in spans if sp[0] <= t), default=(0, 0))
        return round(st + t - off, 1)
    if job_id is not None:  # lets a search jump to the moment something is said
        update(job_id, segments=json.dumps([[real(s.start), s.text.strip()] for s in segs], ensure_ascii=False))
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


# DeepSeek's list prices, CNY per 1M tokens at peak: (input from cache, input not from cache, output incl. reasoning).
# Off-peak is half; peak is 01:00-04:00 and 06:00-10:00 UTC on weekdays (Chinese public holidays are off-peak all
# day; not known here, so those days are priced as peak). https://api-docs.deepseek.com/quick_start/pricing, 2026-10.
# LLM_PRICE="hit,miss,output" overrides. The DeepSeek balance (record_balance) shows what was really charged.
LLM_PRICES = {"deepseek-flash": (0.04, 2.0, 8.0), "deepseek-v4-pro": (0.30, 9.0, 27.0)}


def llm_cost(model, hit, miss, out, when):
    try:
        price = tuple(float(x) for x in os.environ["LLM_PRICE"].split(","))
    except (KeyError, ValueError):
        price = LLM_PRICES.get(model) or LLM_PRICES.get(LLM_MODEL)
    if not price:
        return None
    t = time.gmtime(when)
    if not (t.tm_wday < 5 and (1 <= t.tm_hour < 4 or 6 <= t.tm_hour < 10)):
        price = tuple(p / 2 for p in price)
    return round((hit * price[0] + miss * price[1] + out * price[2]) / 1e6, 6)


def record_balance():
    """DeepSeek account balance (free to ask), so 资源使用 can show what was really charged, not only list prices."""
    if not LLM_API_KEY or "deepseek" not in LLM_BASE_URL:
        return
    r = requests.get(f"{LLM_BASE_URL}/user/balance", headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=20)
    info = next((b for b in r.json().get("balance_infos", []) if float(b.get("total_balance") or 0) > 0),
                (r.json().get("balance_infos") or [None])[0])
    if info:
        last = q("SELECT total FROM balance ORDER BY ts DESC LIMIT 1", one=True)
        total = float(info["total_balance"])
        if not last or abs(last["total"] - total) > 1e-9 or time.time() - kv_get("balance_logged", 0) > 6 * 3600:
            q("INSERT INTO balance (ts, currency, total) VALUES (?,?,?)", (time.time(), info["currency"], total))
            kv_set("balance_logged", time.time())


def llm_json(system, user, fields, max_tokens, usage, purpose="", job_id=None, think=True):
    """max_tokens includes the model's reasoning tokens; only tokens actually used are billed.
    think=False: no reasoning at all, for mechanical work (translating lines, matching tags) where it only costs:
    thinking took ~5,000 of the ~6,000 output tokens of a 60-line subtitle batch."""
    thinking = ({"reasoning_effort": LLM_EFFORT} if LLM_EFFORT else {}) if think else \
        ({"thinking": {"type": "disabled"}} if "deepseek" in LLM_BASE_URL else {})
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=180,
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                      json={"model": LLM_MODEL, "max_tokens": max_tokens, **thinking,
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
    hit = u.get("prompt_cache_hit_tokens") or 0
    miss = u.get("prompt_cache_miss_tokens", (u.get("prompt_tokens") or 0) - hit)
    cost = llm_cost(body.get("model") or LLM_MODEL, hit, miss, u.get("completion_tokens") or 0, time.time())
    if cost is not None:
        usage["cost"] = round(usage.get("cost", 0) + cost, 6)
    log_usage("llm", purpose, job_id, amount=1, tokens_in=u.get("prompt_tokens") or 0,
              tokens_out=u.get("completion_tokens") or 0, cost=cost, cache_hit=hit)
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
            a = llm_json(CLASSIFY_SYSTEM, "\n\n".join(parts), CLASSIFY_FIELDS, 4000, usage, "classify", job_id)
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
    """Fold synonym tags together across the whole library (the 合并标签 button; ~20k tokens, so not automatic)."""
    if not LLM_API_KEY:
        return {}
    with tag_lock:
        tags = library_tags()
        if len(tags) < 2:
            return {}
        out = llm_json(MERGE_SYSTEM, "Tags (most used first):\n" + "\n".join(tags), {"merge": "object"}, 32000, {}, "tags")  # room for the model's reasoning over the whole tag list
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
        apply_tag_mapping(mapping)
        kv_set("tags_merged", library_tags())
    return mapping


def apply_tag_mapping(mapping):
    """Rename tags across the library (and in Plex); a search for an old name finds the new one."""
    if not mapping:
        return
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
    if changed:
        finishing_touch(plex_set_metadata, changed)


MATCH_SYSTEM = """You keep a media library's tags tidy. For each NEW tag, say which EXISTING tag means exactly the same
thing (a synonym, translation, abbreviation, other spelling, case or plural variant, e.g. "LLM" = "大语言模型",
"TED Talks" = "TED演讲", "川普" = "特朗普"), or null if none does. Merely related tags are not the same ("伊朗" is not
"中东", "深度学习" is not "机器学习"); names of different people are never the same.
Reply with one JSON object: {{"same": {{"<new tag>": "<existing tag or null>", ...}}}}"""


def tag_key(t):
    """What's left of a tag once case, width, traditional characters, spaces, punctuation and a plural -s are gone."""
    import unicodedata
    try:
        from opencc import OpenCC
        t = OpenCC("t2s").convert(t)
    except Exception:
        pass
    k = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", t).casefold())
    return k[:-1] if re.fullmatch(r"[a-z]{4,}s", k) else k


def merge_tags_if_new():
    """After a download: fold its new tags into ones already in the library that mean the same.
    Only the new tags are looked at: same spelling once normalised is merged right here; the rest goes to the
    LLM as one narrow question ("which existing tag is this one?"), a few hundred tokens instead of
    re-reading the whole list pair by pair (which cost ~20k reasoning tokens per download)."""
    try:
        (STATE / "locks").mkdir(parents=True, exist_ok=True)
        with open(STATE / "locks" / "tags.lock", "w") as lock:  # one at a time across job processes
            fcntl.flock(lock, fcntl.LOCK_EX)
            tags = library_tags()
            known = [t for t in tags if t in set(kv_get("tags_merged", []))]
            new = [t for t in tags if t not in set(known)]
            if not new:
                return
            if not known:  # first run: nothing to compare with yet
                kv_set("tags_merged", tags)
                return
            by_key = {}
            for t in known:  # most used first, so the common spelling wins
                by_key.setdefault(tag_key(t), t)
            mapping = {t: by_key[tag_key(t)] for t in new if tag_key(t) in by_key and by_key[tag_key(t)] != t}
            ask = [t for t in new if t not in mapping]
            if ask and LLM_API_KEY:
                out = llm_json(MATCH_SYSTEM, "EXISTING tags (most used first):\n" + "\n".join(known) +
                               "\n\nNEW tags:\n" + "\n".join(ask), {"same": "object"}, 4000, {}, "tags", think=False)
                for t, same in (out.get("same") or {}).items():
                    if t in ask and isinstance(same, str) and same in known and same != t:
                        mapping[t] = same
            apply_tag_mapping(mapping)
            kv_set("tags_merged", list(dict.fromkeys(known + [t for t in new if t not in mapping])))
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
        out = llm_json(SUMMARY_SYSTEM, user, SUMMARY_FIELDS, 8000, a.setdefault("usage", {}), "summarize", job_id)
        a["summary"] = str(out["summary"] or a["summary"])
        points = out["key_points"]
        if isinstance(points, str):  # now and then a single string instead of a list
            points = [x.strip(" -•·") for x in re.split(r"[\n；;]+", points)]
        a["key_points"] = [str(x) for x in points if str(x).strip()] if isinstance(points, list) else []
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


# ---------------------------------------------------------------- B站 joint videos

def bili_staff(url):
    """Names of everyone credited on a B站 joint video (创作团队: UP主, 参演 ...), [] otherwise."""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    if not m:
        return []
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": m.group(1)}, timeout=15,
                         headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        return [s["name"] for s in (r.json().get("data") or {}).get("staff") or [] if s.get("name")]
    except Exception:
        return []


def add_people_tags(a, names):
    """Everyone in it is a tag right after the creator, like the creator is."""
    tags = a.setdefault("tags", [])
    at = 1 if tags and tags[0] == a.get("creator") else 0
    for name in names:
        if name not in tags:
            tags.insert(at, name)
            at += 1


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
            started, before = time.time(), m
            m = make_plex_friendly(job_id, m, keep_mkv=kind == "torrent")
            if m != before:
                log_usage("encode", "plex", job_id, amount=float(ffprobe(m).get("format", {}).get("duration") or 0),
                          seconds=time.time() - started)
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
            staff = bili_staff(meta.get("webpage_url") or url)  # B站 joint videos list everyone who's in them
            a = classify(job_id, m.name, {**meta, "file": m.name, "size": human(m.stat().st_size),
                                          "duration_s": float(ffprobe(m).get("format", {}).get("duration") or 0),
                                          **({"people_in_it": staff} if staff else {})},
                         guess)
            add_people_tags(a, staff)
            if re.fullmatch(r"\d{8}", str(meta.get("upload_date") or "")):  # when it came out (for 追更周报)
                d = meta["upload_date"]
                a["published"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            subs = sorted(m.parent.glob(glob.escape(m.stem) + "*.srt"))
            transcript, note = (srt_to_text(subs[0]), f"(from subtitles {subs[0].name[len(m.stem):]})") if subs else (None, "")
            # Subtitles that came with it: summarise now (when the classifier says it's worth it). Otherwise the
            # task board makes them (transcribe -> save_subs -> summarize), on the Mac when it's there
            if transcript and a.get("needs_transcript") and LLM_API_KEY:
                a = summarize(job_id, a, transcript, note)
            elif not transcript and a.get("needs_transcript"):
                a["note"] = "summarised once subtitles have been made"
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
    log_usage("download", kind, job_id, amount=sum(Path(f).stat().st_size for f in final_files if Path(f).exists()))
    try:  # its slow work goes on the task board: links you sent first, older videos of followed uploaders last
        publish_job_work(job_id, 10 if job.get("backfill") else 50, force=True)
    except Exception:
        traceback.print_exc()
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
        ring("jobs", "tasks")  # a download slot is free; the CPU may be (the Pi's idle work waits for that)
        notify(job_id)


finishing = []  # job processes past their final status, still doing finishing touches
claim_lock = threading.Lock()


def worker_loop():
    while True:
        mark = bell_mark("jobs")
        with claim_lock:
            # failed jobs whose automatic retry is due go back in the queue
            if q("SELECT 1 FROM jobs WHERE status='failed' AND retry_at IS NOT NULL AND retry_at<=? LIMIT 1", (time.time(),), one=True):
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
            bell_wait("jobs", mark, 60)  # a new job rings; a due automatic retry is found within the minute
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
    return f"Retrying #{jid}"


# ---------------------------------------------------------------- following channels (追更)
# A link to an uploader's page (a B站 space, a YouTube channel) follows it instead of downloading one video:
# its latest videos are queued right away (SUB_BACKFILL of them, or all with "缓存全部"), and the worker
# checks it again every SUB_INTERVAL seconds (a week) and queues whatever is new, like following a show.
SUB_INTERVAL = int(os.environ.get("SUB_INTERVAL", str(7 * 86400)))  # once a week
SUB_BACKFILL = int(os.environ.get("SUB_BACKFILL", "50"))
SUB_WORKERS = int(os.environ.get("SUB_WORKERS", "2"))  # download slots followed channels may use; the rest stay free for links you send
SUB_SEEN_MAX = 5000
SUB_RETRY = 3600  # a failed check (e.g. B站 risk control) is tried again an hour later, not next week


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
        for attempt in range(6):  # B站 answers about half of these with HTTP 412 (risk control); retrying gets through
            r = s.get("https://api.bilibili.com/x/space/wbi/arc/search", params=bili_signed(s, params), timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"code": r.status_code}
            if body.get("code") == 0:
                break
            time.sleep(3 + attempt * 4)
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
    later checks only look at the newest 30; everything=True goes through the whole list.
    Returns how many were queued, or None if the list couldn't be fetched."""
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
        return None
    seen = json.loads(sub["seen"] or "[]")
    seen_set = set(seen)
    new = [e for e in res["entries"] if link_key(e["url"]) not in seen_set]
    # oldest first, so the newest video gets the highest id and sits at the top of the page
    for e in reversed(new):
        jid, how = add_job_ex(e["url"], source=f"sub:{sub_id}", owner=sub["owner"], device=sub["device"])
        if how == "new" and e["title"]:
            update(jid, title=e["title"])  # the list already has the title: show it while the video waits
        if how == "new" and (first or everything):
            update(jid, backfill=1)  # older videos: their subtitles are made in idle time
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
            for r in q("SELECT id, everything FROM subs WHERE checked IS NULL OR checked < ? OR (error != '' AND checked < ?) "
                       "ORDER BY checked IS NOT NULL, checked", (time.time() - SUB_INTERVAL, time.time() - SUB_RETRY)):
                if check_sub(r["id"], everything=bool(r["everything"])) is not None:
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
    if request.path.startswith("/api/compute/"):  # the Mac worker: token, no cookie, not a browser
        g.device, g.new_device, g.user, g.admin, g.owner = "", False, None, False, None
        return
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
    if request.path.startswith("/api/notes") and request.method == "POST":
        request.max_content_length = NOTE_MAX_UPLOAD
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
    q("UPDATE notes SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    q("UPDATE OR IGNORE watch SET owner=? WHERE owner=?", (f"user:{name}", g.device))
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
    # The same browser gets a new cookie for every address it opens the page by (192.168.3.200 and
    # pi-gateway.local are two sites to it), so it shows up as several devices: one row per kind of browser
    # on one IP, removed together
    devices, rows = [], {}
    for d in q("SELECT * FROM devices WHERE user=? ORDER BY seen DESC", (g.user,)):
        phones = [p["name"] for p in q("SELECT name FROM phones WHERE device=?", (d["id"],))]
        row = rows.get((d["label"], d["ip"]))
        if row:
            row["ids"].append(d["id"])
            row["current"] = row["current"] or d["id"] == g.device
            row["phones"] += [p for p in phones if p not in row["phones"]]
            continue
        row = rows[(d["label"], d["ip"])] = {"id": d["id"], "ids": [d["id"]], "label": d["label"] or "设备", "seen": d["seen"],
                                             "current": d["id"] == g.device, "phones": phones}
        devices.append(row)
    # Jobs from before devices were recorded have no device; JSON keys must be strings
    counts = {r["device"] or "未知设备": r["n"] for r in q("SELECT device, COUNT(*) n FROM jobs WHERE owner=? GROUP BY device",
                                                       (f"user:{g.user}",))}
    return jsonify(user=g.user, devices=devices, counts=counts)


@app.post("/api/devices/<device_id>/remove")
def remove_device(device_id):
    """Untie a browser (and the iPhone shortcut that goes with it) from the account; its jobs stay."""
    if not g.user:
        return jsonify(error="not logged in"), 403
    ids = device_id.split(",")  # one row in the list can be several cookies of the same browser
    for i in ids:
        q("UPDATE devices SET user=NULL WHERE id=? AND user=?", (i, g.user))
    if g.device in ids:
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
    if not ids and not request.cookies and text.strip():
        # the iOS / Android shortcut shared plain text (no link): keep it as a 随记
        now = time.time()
        q("INSERT INTO notes (owner, text, device, created, updated) VALUES (?,?,?,?,?)",
          (g.owner, text.strip()[:20000], g.device_label, now, now))
        return Response("没有链接，已经记到「随记」里", mimetype="text/plain")
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


@functools.lru_cache(maxsize=256)
def _cues(path, mtime):
    """(start seconds, text) of each cue of an .srt / .vtt file."""
    out = []
    text = Path(path).read_text(errors="ignore").replace("\r", "")
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d+)\s*-->", block)
        if m:
            t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + float("0." + m.group(4))
            line = re.sub(r"<[^>]+>", "", block[m.end():].split("\n", 1)[-1]).replace("\n", " ").strip()
            if line and (not out or out[-1][1] != line):
                out.append((round(t, 1), line))
    return out


def subtitle_hits(d, row, term, limit=30):
    """Where `term` is said: [{part, t, text}] from the subtitle files, else from speech-to-text segments."""
    term, hits = term.lower(), []
    for n, m in enumerate(playable(d)):
        for sub in m["subs"][:1]:  # the first track: others are usually the same lines in another language
            try:
                cues = _cues(sub, Path(sub).stat().st_mtime)
            except OSError:
                continue
            hits += [{"part": n, "t": t, "text": x} for t, x in cues if term in x.lower()]
    if not hits and row["segments"]:
        hits = [{"part": 0, "t": t, "text": x} for t, x in json.loads(row["segments"]) if term in x.lower()]
    return hits[:limit]


def titleish(d):
    a = d.get("analysis") or {}
    return " ".join(str(x or "") for x in (d.get("title"), a.get("title"), a.get("show"), a.get("creator")))


def match_fields(d, r):
    a = d.get("analysis") or {}
    return [("简介", " ".join(str(a.get(k) or "") for k in ("summary", "brief"))),
            ("要点", " · ".join(map(str, a.get("key_points") or []))),
            ("标签", " · ".join(map(str, a.get("tags") or []))),
            ("字幕", r["transcript"] or ""),
            ("链接", d.get("url") or ""),
            ("文件", " ".join(map(str, d.get("files") or [])))]


def snippet(text, term, before=16, after=60):
    """A bit of `text` around `term`, starting just before it: cards show only two lines, so the hit has to be
    near the start or it's cut off (and the result looks unrelated)."""
    i = text.lower().find(term.lower())
    if i < 0:
        return ""
    start = max(0, i - before)
    return ("…" if start else "") + text[start:i + len(term) + after].replace("\n", " ") + "…"


@app.get("/api/jobs")
def jobs():
    term = request.args.get("q", "").strip()
    term = kv_get("tag_aliases", {}).get(term, term)  # a merged-away tag searches for its canonical form
    scope, scope_args = scope_sql()
    scope += " AND status != 'cancelled'"  # cancelled jobs are hidden
    sub = request.args.get("sub", "")
    # every unfinished job (queued / downloading / failed) is always sent; finished ones a page at a time
    limit = int(request.args["limit"]) if request.args.get("limit", "").isdigit() else 100
    if sub.isdigit():  # one followed uploader's videos
        scope, scope_args, limit = scope + " AND source = ?", (*scope_args, f"sub:{sub}"), max(limit, 1000)
    more = False
    if term:
        # Searches titles, links, summaries, key points, tags, file paths and transcripts
        like = f"%{term}%"
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND (title LIKE ? OR url LIKE ? OR analysis LIKE ? OR files LIKE ? "
                 "OR transcript LIKE ?) ORDER BY id DESC LIMIT ?", (*scope_args, *(like,) * 5, max(limit, 200)))
        # The idle-time index belongs to the job that downloaded the files; entries linked to it share it
        by_source = {}
        for r in q(f"SELECT id, ref FROM jobs WHERE {scope} AND status IN ('done', 'linked')", scope_args):
            by_source.setdefault(r["ref"] or r["id"], []).append(r["id"])
        said = {}  # lines said in the video (subtitles) or written on its cover
        for r in q("SELECT ref, part, t, src, text FROM seg WHERE kind='job' AND text LIKE ? ORDER BY ref, part, t", (like,)):
            for jid in by_source.get(r["ref"], []):
                said.setdefault(jid, []).append({"part": r["part"], "t": r["t"], "src": r["src"],
                                                 "text": snippet(r["text"], term, 10, 40).strip("…") if len(r["text"]) > 50 else r["text"]})
        looks = {}  # covers and frames that look like it
        for source, found in visual_hits(term, "job", set(by_source)).items():
            for jid in by_source[source]:
                looks[jid] = found
        known = {r["id"] for r in rows}
        extra = [i for i in dict.fromkeys([*said, *looks]) if i not in known]
        if extra:
            rows += q(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(extra))})", extra)
        out, only_looks = [], []
        for r in rows:
            d = job_dict(r)
            hits = said.get(r["id"], [])[:30]
            if not hits and d["status"] == "done" and term.lower() in (r["transcript"] or "").lower():
                hits = [{**h, "src": "字幕"} for h in subtitle_hits(d, r, term)]  # not indexed yet
            seen = [{"part": p, "t": t, "src": "画面" if t is not None else "封面", "text": f"看起来像「{term}」"}
                    for _, p, t, _ in looks.get(r["id"], [])[:5]]
            if term.lower() not in titleish(d).lower():
                # say where it was found when it isn't in the title, so the result doesn't look random
                if hits:
                    d["match_where"], d["match"] = hits[0]["src"], hits[0]["text"]
                else:
                    d["match_where"], d["match"] = next(((k, snippet(t, term)) for k, t in match_fields(d, r)
                                                         if term.lower() in t.lower()), ("", ""))
                if not d["match"] and seen:
                    d["match_where"], d["match"] = seen[0]["src"], seen[0]["text"]
                    if seen[0]["t"] is not None:
                        d["frame"] = {"part": seen[0]["part"], "t": seen[0]["t"]}  # show that moment as the cover
            d["hits"] = hits + seen
            # matched only by how it looks: after the text matches, most alike first
            (only_looks if d.get("match_where") in ("画面", "封面") and not hits else out).append(d)
        # at most a dozen: further down the list the likeness gets thin
        out += sorted(only_looks, key=lambda d: -looks[d["id"]][0][0])[:12]
    else:
        unfinished = "status IN ('queued', 'downloading', 'processing', 'linked', 'failed')"
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND {unfinished}", scope_args)
        finished = q(f"SELECT * FROM jobs WHERE {scope} AND NOT {unfinished} ORDER BY id DESC LIMIT ?", (*scope_args, limit + 1))
        more = len(finished) > limit
        if request.args.get("ids"):  # particular videos (opened from a digest, a note...), wherever they are
            wanted = [int(x) for x in request.args["ids"].split(",") if x.isdigit()][:50]
            finished += q(f"SELECT * FROM jobs WHERE {scope} AND id IN ({','.join('?' * len(wanted))})", (*scope_args, *wanted)) if wanted else []
            finished = list({r["id"]: r for r in finished}.values())
            limit = len(finished)
        shown = {r["id"] for r in rows + finished[:limit]}
        # videos you're in the middle of are always there, also when they're further back than the first page
        resume = [r["job_id"] for r in q("SELECT job_id FROM watch WHERE owner=? AND done=0 AND pos > 15 "
                                         "ORDER BY updated DESC LIMIT 12", (g.owner,)) if r["job_id"] not in shown]
        extra = q(f"SELECT * FROM jobs WHERE {scope} AND id IN ({','.join('?' * len(resume))})", (*scope_args, *resume)) if resume else []
        out = [job_dict(r) for r in sorted(rows + finished[:limit] + extra, key=lambda r: r["id"], reverse=True)]
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
    watched = {r["job_id"]: r for r in q("SELECT * FROM watch WHERE owner=?", (g.owner,))}
    for d in out:
        w = watched.get(d["id"])
        if w:
            d["watch"] = {"part": w["part"], "pos": w["pos"], "dur": w["dur"], "done": bool(w["done"]), "at": w["updated"]}
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
                   subs=subs_list(), sub_interval=SUB_INTERVAL, more=more,
                   notes=q("SELECT COUNT(*) n FROM notes WHERE owner=?", (g.owner,), one=True)["n"],
                   # a search also shows matching 随记 among the videos
                   note_hits=[note_dict(r, m, seen) for r, m, seen in notes_search(term)[:50]] if term else [])


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
                    "failed": c.get("failed", 0), "next": (r["checked"] or time.time()) + (SUB_RETRY if r["error"] else SUB_INTERVAL),
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


# ---------------------------------------------------------------- search index: pictures, subtitles, idle work
#
# Everything slow happens ahead of time, in idle time, so a search is only a lookup:
#   seg  – timed lines: subtitle cues (downloaded, or made here with speech-to-text), text read off covers
#   vec  – CLIP vectors (Chinese-CLIP): covers, note photos, a video frame every FRAME_EVERY seconds
# A search runs LIKE over seg and one matrix product over vec (the query's text vector against every picture).
# No LLM is involved: it can't see pictures, and the local models are free and keep diaries private.

CLIP_DIR = STATE / "models" / "clip"
CLIP_REPO = "https://huggingface.co/Xenova/chinese-clip-vit-base-patch16/resolve/main/"
# How clearly above its own baseline a picture must match, by what it is. Measured on 2,545 frames: things that
# are in them (卡车 大桥 地图 士兵 沙漠 ...) top out at 0.055-0.13 with all of the top 6 right; things that aren't
# (猫 钢琴 篮球 雪山 ...) stay below 0.05. Covers are designed graphics, photos were calibrated on the 随记 photos.
CLIP_MARGINS = {"画面": 0.055, "封面": 0.065, "照片": 0.07}
CLIP_MEAN, CLIP_STD = (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)
# everyday words: a picture's average likeness to these is its baseline (some pictures resemble everything a bit)
CLIP_ANCHORS = ["人", "男人", "女人", "孩子", "一群人", "人脸", "文字", "屏幕", "电脑", "手机", "房间", "桌子", "街道", "城市",
                "建筑", "天空", "大海", "山", "树", "花", "草地", "食物", "饮料", "汽车", "动物", "狗", "猫", "鸟", "衣服",
                "书", "地图", "图表", "舞台", "室内", "室外", "夜晚", "运动", "乐器", "会议", "照片", "画", "卡通", "风景",
                "海报", "演讲", "厨房", "办公室", "商店", "交通工具", "游戏"]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
IDLE_WHISPER_MODEL = os.environ.get("IDLE_WHISPER_MODEL", "small")
FRAME_EVERY = int(os.environ.get("FRAME_EVERY", "20"))
IDLE_WORK = os.environ.get("IDLE_WORK", "1") != "0"
# The Mac mini as a compute worker (mac_worker.py): it asks for work over the LAN with this token
COMPUTE_TOKEN = os.environ.get("COMPUTE_TOKEN", "").strip()
_models = {}
_model_lock = threading.Lock()


def clip_files():
    """Download Chinese-CLIP (ONNX, int8) once and split it into its text half (for queries, kept in the web
    process) and its image half (for indexing), so neither has to load the other."""
    if (CLIP_DIR / "text.onnx").exists() and (CLIP_DIR / "vision.onnx").exists():
        return
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    for name, src in (("tokenizer.json", "tokenizer.json"), ("model.onnx", "onnx/model_quantized.onnx")):
        if not (CLIP_DIR / name).exists():
            part = CLIP_DIR / (name + ".part")
            with requests.get(CLIP_REPO + src, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            part.rename(CLIP_DIR / name)
    import onnx.utils
    whole = str(CLIP_DIR / "model.onnx")
    onnx.utils.extract_model(whole, str(CLIP_DIR / "text.onnx"), ["input_ids", "attention_mask"], ["text_embeds"])
    onnx.utils.extract_model(whole, str(CLIP_DIR / "vision.onnx"), ["pixel_values"], ["image_embeds"])
    (CLIP_DIR / "model.onnx").unlink()


def _onnx(name):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    return ort.InferenceSession(str(CLIP_DIR / name), so)


@functools.lru_cache(maxsize=512)
def clip_text(text):
    import numpy as np
    with _model_lock:
        if "text" not in _models:
            clip_files()
            from tokenizers import Tokenizer
            _models["tok"] = Tokenizer.from_file(str(CLIP_DIR / "tokenizer.json"))
            _models["text"] = _onnx("text.onnx")
        ids = np.array([_models["tok"].encode(text).ids[:52]], np.int64)
        v = _models["text"].run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0]
    return v / np.linalg.norm(v)


def clip_anchors():
    if "anchors" not in _models:
        import numpy as np
        path = CLIP_DIR / "anchors.npy"
        if not path.exists():
            np.save(path, np.stack([clip_text(w) for w in CLIP_ANCHORS]))
        _models["anchors"] = np.load(path)
    return _models["anchors"]


def clip_image(path, t=None):
    """CLIP vector of a picture, or of the frame `t` seconds into a video; None if it can't be read."""
    import numpy as np
    pixels = None
    if t is None and Path(path).suffix.lower() in IMAGE_EXT:
        try:  # Pillow follows the photo's EXIF rotation
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                pixels = np.asarray(ImageOps.exif_transpose(im).convert("RGB").resize((224, 224), Image.BICUBIC))
        except Exception:
            pixels = None
    if pixels is None:
        raw = subprocess.run(["ffmpeg", "-v", "error", *(["-ss", str(t)] if t is not None else []), "-i", str(path),
                              "-frames:v", "1", "-vf", "scale=224:224:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24",
                              "-"], capture_output=True, timeout=120).stdout
        if len(raw) != 224 * 224 * 3:
            return None
        pixels = np.frombuffer(raw, np.uint8).reshape(224, 224, 3)
    a = (pixels.astype(np.float32) / 255 - np.array(CLIP_MEAN, np.float32)) / np.array(CLIP_STD, np.float32)
    with _model_lock:
        if "vision" not in _models:
            clip_files()
            _models["vision"] = _onnx("vision.onnx")
        v = _models["vision"].run(None, {"pixel_values": a.transpose(2, 0, 1)[None]})[0][0]
    return v / np.linalg.norm(v)


def store_vec(kind, ref, part, t, src, v):
    import numpy as np
    q("INSERT INTO vec (kind, ref, part, t, src, base, v) VALUES (?,?,?,?,?,?,?)",
      (kind, ref, part, t, src, float((clip_anchors() @ v).mean()), v.astype(np.float32).tobytes()))


def ocr_text(path):
    """Lines of text read off a picture (RapidOCR: PaddleOCR's models on onnxruntime)."""
    if "ocr" not in _models:
        from rapidocr_onnxruntime import RapidOCR
        _models["ocr"] = RapidOCR()
    started = time.time()
    found, _ = _models["ocr"](str(path))
    lines = [str(text).strip() for _, text, score in (found or []) if float(score) >= 0.6 and len(str(text).strip()) >= 2]
    log_usage("ocr", "picture", amount=1, seconds=time.time() - started)
    return lines


_vecs = {"key": None, "rows": 0}


def vec_matrix():
    """All picture vectors, cached in this process; new rows are appended, a removal reloads everything."""
    import numpy as np
    top = q("SELECT COALESCE(MAX(id), 0) m, COUNT(*) n FROM vec", one=True)
    if _vecs["key"] == (top["m"], top["n"]):
        return _vecs
    after = _vecs["last"] if _vecs["key"] and top["n"] - _vecs["rows"] == top["m"] - _vecs["last"] else 0
    rows = q("SELECT id, kind, ref, part, t, src, base, v FROM vec WHERE id > ? ORDER BY id", (after,))
    meta = [(r["kind"], r["ref"], r["part"], r["t"], r["src"]) for r in rows]
    # the bar to clear: the picture's baseline plus the margin for its kind
    bar = np.array([r["base"] + CLIP_MARGINS[r["src"].split(":")[0]] for r in rows], np.float32)
    # half precision: a frame every few seconds of 150 hours of video is ~100k vectors, 100 MB instead of 200
    m = np.frombuffer(b"".join(r["v"] for r in rows), np.float32).reshape(len(rows), -1).astype(np.float16) \
        if rows else np.zeros((0, 512), np.float16)
    if after:
        meta, bar, m = _vecs["meta"] + meta, np.concatenate([_vecs["bar"], bar]), np.concatenate([_vecs["m"], m])
    _vecs.update(key=(top["m"], top["n"]), rows=top["n"], last=top["m"], meta=meta, bar=bar, m=m)
    return _vecs


def visual_hits(term, kind, allowed):
    """{ref: [(margin, part, t, src) best first]} for pictures that clearly look like `term`."""
    # Chinese-CLIP understands Chinese; English words and single characters mostly match noise
    if len(term) < 2 or not re.search(r"[\u4e00-\u9fff]", term):
        return {}
    try:
        V = vec_matrix()
        if not len(V["meta"]):
            return {}
        import numpy as np
        qv = clip_text(term).astype(np.float32)
        sims = np.concatenate([V["m"][i:i + 20000].astype(np.float32) @ qv for i in range(0, len(V["m"]), 20000)])
        margin = sims - V["bar"]  # >= 0: clearly looks like it
        idx = np.where(margin >= 0)[0]
    except Exception:
        traceback.print_exc()
        return {}
    out = {}
    for i in idx[np.argsort(-margin[idx])]:
        k, ref, part, t, src = V["meta"][i]
        if k == kind and ref in allowed:
            out.setdefault(ref, []).append((float(margin[i]), part, t, src))
    return out


def forget_index(jid):
    q("DELETE FROM seg WHERE kind='job' AND ref=?", (jid,))
    q("DELETE FROM vec WHERE kind='job' AND ref=?", (jid,))
    q("DELETE FROM tasks WHERE target=? OR target LIKE ?", (f"job:{jid}", f"job:{jid}:%"))


# ---- the task board
#
# Everything slow is a task on one board: the `tasks` table, here on the Pi because it's always on. Anyone may
# publish: the Pi when something happens (a download finished, a note was added), a finished task (transcribe ->
# save_subs -> summarize), the Mac (`task publish ...`). Anyone may claim what it can do, through the same steps:
#     claim (what I can do) -> heartbeat (progress; keeps the lease) -> done (result) | fail (retry or give up)
# Claimers: the Pi's own workers (a light one for library writes and AI calls, a heavy one for CPU work in idle
# time) and the Mac mini (mac/mac_worker.py, over HTTP). A lease that isn't renewed runs out and the task goes back
# on the board: a Mac that sleeps or a process that dies only means someone picks the task up later.
# Only the Pi changes the library (files, database): what the Mac works out comes back as a result, and a Pi task
# writes it.

# Every kind of task is declared once, with @task on the function that does it on the Pi:
#   label    how 资源使用 names it
#   pool     which of the Pi's own workers takes it: "light" (library writes, ~no CPU), "ai" (LLM calls; a long
#            translation), "ai-quick" (LLM calls that mustn't wait behind one), "cpu" (heavy, idle time only)
#   prefer   while a worker with this trait ("gpu": the Mac) is around, the Pi leaves the kind to it (unless the task
#            has waited PREFER_WAIT)
#   remote   a remote worker (the Mac, scripts) may publish it
#   then     what's published when it's done (whoever did it): a kind (its result is written by that Pi task), or a
#            function (task, result)
# A worker claims by listing the kinds it can do (plus "gpu" if it has one). "save_*" kinds write what a worker
# worked out into the library, so only the Pi does them.
TASK_KINDS = {}
WORKER_FRESH = 300
PREFER_WAIT = 3 * 86400  # e.g. a Mac that claims but never finishes
LEASE = 300  # seconds a claim lasts without a heartbeat
PREFER_WAIT_PAUSED = 4 * 3600  # a worker paused (a game) longer than this stops counting as around
# "now": CPU work someone is waiting for (a note just made): done right away, not only in idle time
PI_WORKERS = {"light": "pi", "ai": "pi-ai", "ai-quick": "pi-ai-2", "cpu": "pi-cpu", "now": "pi-now"}


def task(kind, label, pool, prefer=None, remote=False, then=None, prefer_wait=PREFER_WAIT):
    def register(run):
        TASK_KINDS[kind] = {"label": label, "pool": pool, "prefer": prefer, "remote": remote, "then": then, "run": run,
                            "prefer_wait": prefer_wait}
        return run
    return register


def pi_worker(pool):
    """(name, kinds it takes) of one of the Pi's own workers. The "ai" worker also takes the quick AI kinds."""
    pools = {pool, "ai-quick"} if pool == "ai" else {pool}
    return PI_WORKERS[pool], [k for k, v in TASK_KINDS.items() if v["pool"] in pools]


def _write(sql, args=()):
    """One statement; how many rows it changed (for compare-and-set updates other processes may race)."""
    with db_lock:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur.rowcount


def task_payload(kind, target):
    """What a task needs to know about its target, filled in by the Pi (a publisher only names the target)."""
    n = re.fullmatch(r"note:(\d+)", target)
    if n:
        row = q("SELECT text, media FROM notes WHERE id=?", (int(n.group(1)),), one=True)
        if not row:
            raise ValueError(f"no note {n.group(1)}")
        return {"note": int(n.group(1)), "title": (row["text"] or "随记")[:30],
                "files": [{"file": m["file"], "kind": m["kind"]} for m in json.loads(row["media"]) if m.get("todo")]}
    d = re.fullmatch(r"digest:(.+):(\d{4}-\d\d-\d\d):(\d{4}-\d\d-\d\d)", target)
    if d:
        return {"owner": d.group(1), "start": d.group(2), "end": d.group(3), "title": f"{d.group(2)} ~ {d.group(3)}"}
    m = re.fullmatch(r"job:(\d+)(?::(\d+))?", target)
    if not m:
        raise ValueError(f"unknown target {target}")
    row = q("SELECT * FROM jobs WHERE id=?", (int(m.group(1)),), one=True)
    if not row or row["status"] != "done":
        raise ValueError(f"no finished job {m.group(1)}")
    a = json.loads(row["analysis"] or "{}")
    out = {"job": row["id"], "title": a.get("title") or row["title"]}
    if m.group(2) is not None:
        media = playable(job_dict(row))
        n = int(m.group(2))
        if n >= len(media):
            raise ValueError(f"job {row['id']} has no file {n}")
        out.update(part=n, duration=duration_of(media[n]["path"]) or 0, language=whisper_lang(a.get("language")))
    return out


def publish(kind, target, priority=0, parent=None, by="pi", force=False, not_before=None):
    """Put a task on the board; one per kind and target (publishing it again is a no-op unless `force`, which runs
    a finished one again). Returns its id."""
    if kind not in TASK_KINDS:
        raise ValueError(f"unknown kind {kind}")
    payload = json.dumps(task_payload(kind, target), ensure_ascii=False)
    now = time.time()
    _write("INSERT OR IGNORE INTO tasks (kind, target, priority, payload, parent, published_by, not_before, created, "
           "updated) VALUES (?,?,?,?,?,?,?,?,?)", (kind, target, priority, payload, parent, by, not_before, now, now))
    row = q("SELECT id, state FROM tasks WHERE kind=? AND target=?", (kind, target), one=True)
    ring("tasks")
    if force and row["state"] in ("done", "failed"):
        _write("UPDATE tasks SET state='queued', priority=?, payload=?, parent=?, published_by=?, result=NULL, "
               "progress=NULL, error='', attempts=0, worker=NULL, lease_until=NULL, not_before=?, updated=? "
               "WHERE id=? AND state IN ('done','failed')", (priority, payload, parent, by, not_before, now, row["id"]))
    return row["id"]


def publish_job_work(jid, priority, force=False):
    """A finished download's slow work: subtitles where there are none (speech-to-text, except songs), indexing the
    subtitle files it came with, the cover."""
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] != "done" or row["ref"]:
        return
    a = json.loads(row["analysis"] or "{}")
    music = a.get("library") == "Music" or a.get("folder") == "Music Videos"  # speech-to-text can't do songs
    for n, m in enumerate(playable(job_dict(row))):
        if m["subs"]:
            publish("index_subs", f"job:{jid}:{n}", priority, force=force)
        elif not music:
            publish("transcribe", f"job:{jid}:{n}", priority, force=force)
        if Path(m["path"]).suffix.lower() in VIDEO_EXT:
            publish("frames", f"job:{jid}:{n}", priority - 1, force=force)
    if row["thumb"] and Path(row["thumb"]).exists():
        publish("cover", f"job:{jid}", priority, force=force)


_seen_written = {}


def seen_worker(name, caps, task=None, paused=None):
    # at most once a minute unless something changed: three workers asking every few seconds wore the SD card
    state = (json.dumps(sorted(caps)), task, paused)
    last = _seen_written.get(name)
    if last and last[0] == state and time.time() - last[1] < 60:
        return
    _seen_written[name] = (state, time.time())
    q("INSERT INTO workers (name, caps, seen, task, paused) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
      "caps=excluded.caps, seen=excluded.seen, task=excluded.task, paused=excluded.paused",
      (name, json.dumps(sorted(caps)), time.time(), task, paused))


def task_dict(row):
    return {"id": row["id"], "kind": row["kind"], "target": row["target"], "priority": row["priority"],
            "payload": json.loads(row["payload"]), "progress": json.loads(row["progress"] or "null")}


def claim_task(worker, caps):
    """The most urgent task this worker can do, now leased to it; None if there's nothing."""
    caps, now = set(caps), time.time()
    seen_worker(worker, caps)
    # leases that ran out: back on the board (keeping any progress); looked for first, so a quiet board isn't written
    if q("SELECT 1 FROM tasks WHERE state='running' AND lease_until < ? LIMIT 1", (now,), one=True):
        _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL WHERE state='running' AND lease_until < ?", (now,))
    kinds = [k for k in TASK_KINDS if k in caps]
    if not kinds:
        return None
    alive = {c for r in q("SELECT caps FROM workers WHERE seen > ? AND name != ?", (now - WORKER_FRESH, worker))
             for c in json.loads(r["caps"])}
    # leave a preferred worker's tasks to it while it's around (unless they've waited long enough)
    waived = [k for k in kinds if TASK_KINDS[k].get("prefer") and TASK_KINDS[k]["prefer"] not in caps
              and TASK_KINDS[k]["prefer"] in alive]
    marks = ",".join("?" * len(kinds))
    cond = f"state='queued' AND kind IN ({marks}) AND COALESCE(not_before, 0) <= ?"
    args = [*kinds, now]
    for k in waived:  # until it has waited that kind's prefer_wait
        cond += " AND NOT (kind = ? AND created > ?)"
        args += [k, now - TASK_KINDS[k]["prefer_wait"]]
    for _ in range(5):  # another worker may take the same row first: try the next one
        row = q(f"SELECT * FROM tasks WHERE {cond} ORDER BY priority DESC, id DESC LIMIT 1", args, one=True)
        if not row:
            return None
        if _write("UPDATE tasks SET state='running', worker=?, lease_until=?, attempts=attempts+1, updated=? "
                  "WHERE id=? AND state='queued'", (worker, now + LEASE, now, row["id"])):
            seen_worker(worker, caps, row["id"])
            return task_dict(row) | {"lease": LEASE}
    return None


def heartbeat_task(tid, worker, progress=None):
    """Still on it (and how far): the lease is renewed. False if the task isn't this worker's any more."""
    seen_worker(worker, json.loads((q("SELECT caps FROM workers WHERE name=?", (worker,), one=True) or {"caps": "[]"})["caps"]), tid)
    return bool(_write("UPDATE tasks SET lease_until=?, progress=COALESCE(?, progress), updated=? "
                       "WHERE id=? AND worker=? AND state='running'",
                       (time.time() + LEASE, None if progress is None else json.dumps(progress, ensure_ascii=False),
                        time.time(), tid, worker)))


def complete_task(tid, worker, result):
    if not _write("UPDATE tasks SET state='done', result=?, lease_until=NULL, updated=? WHERE id=? AND worker=? "
                  "AND state='running'", (json.dumps(result, ensure_ascii=False), time.time(), tid, worker)):
        return False
    task = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
    try:
        then = TASK_KINDS.get(task["kind"], {}).get("then")
        if isinstance(then, str):  # a Pi task writes the result into the library
            publish(then, task["target"], task["priority"], parent=task["id"], force=True)
        elif then:
            then(task, result)
    except Exception:
        traceback.print_exc()
    return True


def fail_task(tid, worker, error, retry=True):
    """retry: try again later (after 1, 4, 9... minutes; at most 5 times); else it stays failed."""
    row = q("SELECT attempts FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, worker), one=True)
    if not row:
        return False
    if retry and row["attempts"] < 5:
        _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL, error=?, not_before=?, updated=? WHERE id=?",
               (str(error)[:1000], time.time() + 60 * row["attempts"] ** 2, time.time(), tid))
    else:
        _write("UPDATE tasks SET state='failed', worker=NULL, lease_until=NULL, error=?, updated=? WHERE id=?",
               (str(error)[:1000], time.time(), tid))
    return True


def release_task(tid, worker):
    """Paused for other work (not a failure): back on the board with its progress, the try not counted."""
    ring("tasks")
    _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL, attempts=MAX(attempts-1, 0), updated=? "
           "WHERE id=? AND worker=? AND state='running'", (time.time(), tid, worker))


def task_media(task):
    p = task["payload"]
    row = q("SELECT * FROM jobs WHERE id=?", (p["job"],), one=True)
    media = playable(job_dict(row)) if row else []
    if p.get("part") is None or p["part"] >= len(media):
        raise ValueError("the file is gone")
    return Path(media[p["part"]]["path"]), media[p["part"]]["subs"]


# ---- what happens when a task is done (on the Pi, whoever did it)

def after_transcribe(task, result):
    p = json.loads(task["payload"])
    log_usage("whisper", "mac" if task["worker"] != PI_WORKERS["cpu"] else "idle", p["job"],
              amount=float(result.get("audio_seconds") or 0), seconds=float(result.get("seconds") or 0))
    publish("save_subs", task["target"], task["priority"], parent=task["id"], force=True)


def parent_result(task):
    row = q("SELECT result FROM tasks WHERE id=(SELECT parent FROM tasks WHERE id=?)", (task["id"],), one=True)
    return json.loads(row["result"] or "{}") if row else {}


def drop_parent_result(task, summary):
    """Once written into the library, the bulky result (vectors, every subtitle line) isn't needed on the board."""
    _write("UPDATE tasks SET result=? WHERE id=(SELECT parent FROM tasks WHERE id=?)",
           (json.dumps(summary, ensure_ascii=False), task["id"]))


def vec_from(b64):
    import base64
    import numpy as np
    v = np.frombuffer(base64.b64decode(b64), np.float16).astype(np.float32)
    return v / np.linalg.norm(v)


def vec_to(v):
    import base64
    import numpy as np
    return base64.b64encode(np.asarray(v, np.float16).tobytes()).decode()


# ---- the Pi's own work for each kind

class Paused(Exception):
    pass


@task("save_subs", "保存字幕", "light")
def pi_save_subs(task, beat):
    """The subtitles a transcribe task worked out: .srt next to the video (player, Plex), lines into the search
    index, the transcript; then the summary that waited for them."""
    res = parent_result(task)
    segs = [[float(a), float(b), str(t).strip()] for a, b, t in res.get("segments") or [] if str(t).strip()]
    lang = re.sub(r"[^a-z-]", "", str(res.get("language") or "und"))[:8] or "und"
    path, _ = task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]

    def ts(t):
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(sec):02},{int((sec % 1) * 1000):03}"
    if segs:
        srt = path.with_name(f"{path.stem}.{lang}.srt")
        srt.write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(segs, 1)))
        q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (jid, n))
        for a, _, t in segs:
            q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'字幕',?)", (jid, n, a, t))
        if n == 0:
            update(jid, transcript="\n".join(t for _, _, t in segs)[:200_000])
            a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
            if a.get("needs_transcript") and not a.get("key_points") and LLM_API_KEY:
                publish("summarize", f"job:{jid}", task["priority"], parent=task["id"], force=True)
        plex_refresh()  # Plex picks up the new subtitle file
        maybe_translate(task, path, lang)
        publish("similar", f"job:{jid}", task["priority"] - 3, parent=task["id"], force=True)
        publish("chapters", task["target"], task["priority"] - 1, parent=task["id"], force=True)
    drop_parent_result(task, {"language": lang, "lines": len(segs), "audio_seconds": res.get("audio_seconds")})
    return {"lines": len(segs), "language": lang}


@task("summarize", "AI 总结", "ai-quick", remote=True)
def pi_summarize(task, beat):
    jid = task["payload"]["job"]
    row = q("SELECT analysis, transcript, files FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    if not row["transcript"]:
        return {"skipped": "no transcript"}
    a.pop("note", None)
    a = summarize(jid, a, row["transcript"], "(subtitles of the whole video)")
    update(jid, analysis=a, stage="")
    publish("chapters", f"job:{jid}:0", task["priority"], parent=task["id"], force=True)  # key points changed
    vids = [f for f in json.loads(row["files"] or "[]") if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT]
    if vids:
        threading.Thread(target=plex_set_metadata, args=([(vids[0], a)],), daemon=True).start()
    return {"summary": bool(a.get("key_points"))}


def offpeak_from(when=None):
    """The next moment DeepSeek charges half (see llm_cost); `when` itself if it already does."""
    t = when or time.time()
    while (lambda g: g.tm_wday < 5 and (1 <= g.tm_hour < 4 or 6 <= g.tm_hour < 10))(time.gmtime(t)):
        t += 900
    return t


def sub_lang(path, stem):
    """The language code in a subtitle file's name: "Talk.en-orig.srt" -> "en-orig"."""
    return Path(path).name[len(stem) + 1:].rsplit(".", 1)[0]


def maybe_translate(task, path, subs_or_lang):
    """English subtitles and no Chinese ones yet: translate them (off-peak, at half the price)."""
    langs = [subs_or_lang] if isinstance(subs_or_lang, str) else [sub_lang(x, path.stem) for x in subs_or_lang]
    if any(l.split("-")[0] == "en" for l in langs) and not any(l.split("-")[0] == "zh" for l in langs) and LLM_API_KEY:
        publish("translate", task["target"], task["priority"] - 2, parent=task["id"], not_before=offpeak_from())


def srt_cues(path):
    """[(start, end, text)] of an .srt/.vtt, consecutive repeats merged and overlaps cut (auto captions roll: each
    cue starts before the last one ends)."""
    out = []
    text = Path(path).read_text(errors="ignore").replace("\r", "")
    stamp = r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d+)"
    for block in re.split(r"\n\s*\n", text):
        m = re.search(stamp + r"\s*-->\s*" + stamp, block)
        if not m:
            continue
        g = m.groups()
        a = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + float("0." + g[3])
        b = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + float("0." + g[7])
        line = re.sub(r"<[^>]+>", "", block[m.end():].split("\n", 1)[-1]).replace("\n", " ").strip()
        if not line:
            continue
        if out and out[-1][2] == line:
            out[-1][1] = b
            continue
        if out and out[-1][1] > a:
            out[-1][1] = a
        out.append([a, b, line])
    return out


def write_srt(path, cues):
    def ts(t):
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(sec):02},{int((sec % 1) * 1000):03}"
    Path(path).write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)))


TRANSLATE_SYSTEM = """You translate a video's subtitles from {src} into natural Simplified Chinese.
The input is consecutive subtitle lines, numbered; auto-generated captions break sentences anywhere and have no
punctuation. Give exactly one Chinese line per input line, in the same order: you may move a few words between
neighbouring lines so the Chinese reads naturally, but every line must stay about where its words are said.
Keep names, terms and code identifiers that are usually left untranslated; use the common Chinese renderings
of well-known names. Reply with one JSON object: {{"lines": ["...", ...]}} with exactly as many lines as given."""


@task("translate", "翻译字幕", "ai")
def pi_translate(task, beat):
    """Chinese subtitles for an English video: X.zh.srt next to it (Plex shows it as Chinese; the page also offers
    both together), and the Chinese lines go into search (a Chinese search finds the English moment)."""
    path, subs = task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]
    en = sorted((x for x in subs if sub_lang(x, path.stem).split("-")[0] == "en"),
                key=lambda x: sub_lang(x, path.stem) != "en")  # the uploader's own .en before the automatic -orig
    if not en or any(sub_lang(x, path.stem).split("-")[0] == "zh" for x in subs):
        return {"skipped": "no English subtitles, or Chinese ones already"}
    cues = srt_cues(en[0])
    # carry on from what an earlier run (stopped by a restart) translated already
    prog = task["progress"] or {}
    out = prog.get("out") if prog.get("from") == Path(en[0]).name else None
    out = out or []
    i, size, usage = len(out), 60, {}
    while i < len(cues):
        batch = cues[i:i + size]
        user = "\n".join(f"{k + 1}. {t}" for k, (_, _, t) in enumerate(batch))
        try:
            got = llm_json(TRANSLATE_SYSTEM.replace("{src}", "English"), user, {"lines": "array of strings"}, 12000,
                           usage, "translate", jid, think=False)["lines"]
        except Exception:
            got = None
        if not isinstance(got, list) or len(got) != len(batch):
            if size > 8:  # the model merged or split lines: try smaller pieces
                size //= 2
                continue
            got = [t for _, _, t in batch]  # give up on these few: keep the English
        out += [[a, b, str(t).strip()] for (a, b, _), t in zip(batch, got)]
        i += len(batch)
        size = min(60, size * 2)
        beat({"pct": round(i / len(cues) * 100), "from": Path(en[0]).name, "out": out})
    write_srt(path.with_name(f"{path.stem}.zh.srt"), out)
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='译文'", (jid, n))
    for a, _, t in out:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'译文',?)", (jid, n, round(a, 1), t))
    plex_refresh()
    return {"lines": len(out), "from": Path(en[0]).name, "calls": usage.get("calls"), "cost": usage.get("cost")}


DIGEST_SYSTEM = """You write a digest of the videos some followed uploaders published in a period (usually a week), for
the person following them, in Simplified Chinese. For each uploader: an overview of 2-4 sentences on what they
covered and their main views (not a list of titles), then one short line per video saying what it's about. Finally
one sentence for the whole period. Be concrete: names, places, claims, numbers. The ids are only for the JSON:
never mention a video's id in the text.
Reply with one JSON object: {"headline": "...", "uploaders": [{"sub": <id>, "overview": "...",
"videos": [{"job": <id>, "line": "..."}]}]}"""


def digest_videos(owner, start, end):
    """The followed uploaders' videos that came out from `start` to `end` (dates) and are downloaded: by their
    upload date, or when that isn't known, by when a weekly check (not the first look back) fetched them."""
    subs = {r["id"]: r for r in q("SELECT * FROM subs WHERE owner=?", (owner,))}
    t0 = time.mktime(time.strptime(start, "%Y-%m-%d"))
    t1 = time.mktime(time.strptime(end, "%Y-%m-%d")) + 86400
    out = {}
    for r in q("SELECT * FROM jobs WHERE status='done' AND owner=? AND source LIKE 'sub:%'", (owner,)):
        sid = int(r["source"][4:])
        a = json.loads(r["analysis"] or "{}")
        pub = a.get("published")
        if sid in subs and ((start <= pub <= end) if pub else (not r["backfill"] and t0 <= r["created"] < t1)):
            out.setdefault(sid, []).append((pub or time.strftime("%Y-%m-%d", time.localtime(r["created"])), r, a))
    return subs, {sid: sorted(v, key=lambda x: x[0]) for sid, v in out.items()}


@task("digest", "追更周报", "ai-quick")
def pi_digest(task, beat):
    p = task["payload"]
    subs, found = digest_videos(p["owner"], p["start"], p["end"])
    body = {"start": p["start"], "end": p["end"], "headline": "", "uploaders": []}
    if found:
        parts = []
        for sid, vids in found.items():
            parts.append(f"Uploader {sid}: {subs[sid]['name']}")
            for pub, r, a in vids:
                points = "；".join(a.get("key_points") or [])
                parts.append(f"- video {r['id']} ({pub}): {a.get('title') or r['title']}\n  {a.get('summary', '')}"
                             + (f"\n  要点：{points}" if points else ""))
        out = llm_json(DIGEST_SYSTEM.replace("{", "{{").replace("}", "}}"), "\n".join(parts)[:60000],
                       {"headline": "string", "uploaders": "array"}, 8000, {}, "digest")
        body["headline"] = str(out.get("headline") or "")
        for u in out.get("uploaders") or []:
            sid = as_int(u.get("sub"))
            if sid in found:
                titles = {r["id"]: (a.get("title") or r["title"], pub) for pub, r, a in found[sid]}
                body["uploaders"].append({"sub": sid, "name": subs[sid]["name"], "overview": str(u.get("overview") or ""),
                                          "videos": [{"job": as_int(v.get("job")), "line": str(v.get("line") or ""),
                                                      "title": titles[as_int(v.get("job"))][0],
                                                      "published": titles[as_int(v.get("job"))][1]}
                                                     for v in u.get("videos") or [] if as_int(v.get("job")) in titles]})
    q("DELETE FROM digests WHERE owner=? AND start=? AND end=?", (p["owner"], p["start"], p["end"]))
    q("INSERT INTO digests (owner, start, end, body, created) VALUES (?,?,?,?,?)",
      (p["owner"], p["start"], p["end"], json.dumps(body, ensure_ascii=False), time.time()))
    return {"uploaders": len(body["uploaders"]), "videos": sum(len(u["videos"]) for u in body["uploaders"])}


def backfill_published():
    """Once, in the background: upload dates for videos downloaded before they were kept (B站: its API; YouTube:
    yt-dlp), so 追更周报 can tell what came out when."""
    if kv_get("published_backfilled"):
        return
    for r in q("SELECT id, url, analysis FROM jobs WHERE status='done' AND source LIKE 'sub:%'"):
        a = json.loads(r["analysis"] or "{}")
        if a.get("published"):
            continue
        day = None
        try:
            bv = re.search(r"(BV[0-9A-Za-z]{10})", r["url"])
            if bv:
                data = requests.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bv.group(1)},
                                    timeout=15, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}).json().get("data") or {}
                if data.get("pubdate"):
                    day = time.strftime("%Y-%m-%d", time.localtime(data["pubdate"]))
                time.sleep(1)
            elif "youtu" in r["url"]:
                info = ytdlp_probe(r["url"]) or {}
                d = str(info.get("upload_date") or "")
                day = f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else None
        except Exception:
            traceback.print_exc()
        if day:
            a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (r["id"],), one=True)["analysis"] or "{}")
            a["published"] = day
            update(r["id"], analysis=a)
    kv_set("published_backfilled", True)


def publish_digests():
    """Monday mornings: last week's digest for every account that follows someone."""
    now = time.localtime()
    if now.tm_wday != 0 or now.tm_hour < 8:
        return
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    for r in q("SELECT DISTINCT owner FROM subs"):
        publish("digest", f"digest:{r['owner']}:{start}:{end}", 40)


# ---- the same content twice: a re-upload, or a clip of a longer video

MINHASH_N = 128
_mh = {}


def minhash(text):
    """(MinHash signature, number of distinct 6-character shingles) of what's said, punctuation and spaces dropped."""
    import numpy as np
    import zlib
    t = re.sub(r"[\W_]+", "", text.casefold())
    sh = {zlib.crc32(t[i:i + 6].encode()) for i in range(max(len(t) - 5, 0))}
    if not sh:
        return None, 0
    if "ab" not in _mh:
        rng = np.random.default_rng(42)  # the same permutations everywhere, every time
        _mh["ab"] = (rng.integers(1, (1 << 31) - 1, MINHASH_N, dtype=np.uint64), rng.integers(0, (1 << 31) - 1, MINHASH_N, dtype=np.uint64))
    a, b = _mh["ab"]
    x = np.fromiter(sh, np.uint64)
    sig = ((x[:, None] * a[None, :] + b[None, :]) % np.uint64((1 << 31) - 1)).min(axis=0)
    return sig.astype(np.uint32), len(sh)


def job_frames(jid):
    import numpy as np
    rows = q("SELECT v FROM vec WHERE kind='job' AND ref=? AND src='画面' ORDER BY part, t", (jid,))
    return np.stack([np.frombuffer(r["v"], np.float32) for r in rows]) if rows else None


@task("similar", "找重复和切片", "light")
def pi_similar(task, beat):
    """Fingerprint this video, then compare it with every other one: candidates by fingerprint (cheap), checked frame by
    frame and by what's said. "same": most of each is in the other; "clip": most of the shorter one is in the longer."""
    import numpy as np
    jid = task["payload"]["job"]
    row = q("SELECT transcript FROM jobs WHERE id=?", (jid,), one=True)
    sig, nsh = minhash(row["transcript"] or "")
    frames = job_frames(jid)
    mean = (frames.mean(0) / np.linalg.norm(frames.mean(0))).astype(np.float32) if frames is not None else None
    dur = sum(duration_of(m["path"]) or 0 for m in playable(job_dict(q("SELECT * FROM jobs WHERE id=?", (jid,), one=True))))
    q("INSERT OR REPLACE INTO fingerprints (job_id, minhash, shingles, frames, nframes, duration, updated) VALUES (?,?,?,?,?,?,?)",
      (jid, sig.tobytes() if sig is not None else None, nsh, mean.tobytes() if mean is not None else None,
       0 if frames is None else len(frames), dur, time.time()))
    q("DELETE FROM similar WHERE a=? OR b=?", (jid, jid))
    found = 0
    for o in q("SELECT * FROM fingerprints WHERE job_id != ? AND job_id IN (SELECT id FROM jobs WHERE status='done' AND ref IS NULL)", (jid,)):
        said_ab = said_ba = 0.0
        if sig is not None and o["minhash"]:
            jac = float((sig == np.frombuffer(o["minhash"], np.uint32)).mean())
            if jac > 0.02:  # containment from Jaccard and the two set sizes
                said_ab = min(1, jac * (nsh + o["shingles"]) / ((1 + jac) * nsh))
                said_ba = min(1, jac * (nsh + o["shingles"]) / ((1 + jac) * o["shingles"]))
        # What's said decides when both have enough of it (a talk show's episodes share a studio: their frames look
        # alike). Frames decide only for videos with little speech, and must match more (0.8 against 0.6).
        if nsh >= 300 and o["shingles"] >= 300:
            ab, ba, need = said_ab, said_ba, 0.6
        else:
            if not (mean is not None and o["frames"] and len(frames) >= 10 and o["nframes"] >= 10
                    and float(mean @ np.frombuffer(o["frames"], np.float32)) > 0.85):
                continue
            other = job_frames(o["job_id"])
            sims = frames @ other.T
            seen_ab, seen_ba = float((sims.max(1) > 0.92).mean()), float((sims.max(0) > 0.92).mean())
            ab, ba, need = seen_ab, seen_ba, 0.8
        kind = "same" if ab >= need and ba >= need else "clip" if max(ab, ba) >= need else None
        if kind:
            a, b = (jid, o["job_id"]) if jid < o["job_id"] else (o["job_id"], jid)
            a_in_b, b_in_a = (ab, ba) if a == jid else (ba, ab)
            q("INSERT OR REPLACE INTO similar (a, b, kind, a_in_b, b_in_a, updated) VALUES (?,?,?,?,?,?)",
              (a, b, kind, round(a_in_b, 3), round(b_in_a, 3), time.time()))
            found += 1
    return {"found": found, "shingles": nsh, "frames": 0 if frames is None else len(frames)}


CHAPTERS_SYSTEM = """You split a video into chapters from its timed subtitles, for a Chinese viewer.
Each input line starts with its time [h:mm:ss]. Give 4-12 chapters covering the whole video in order: where each
starts (seconds, taken from a line's time) and a short Chinese title (at most 16 characters) naming the topic, not
"第一部分". Then, for each numbered KEY POINT, the time (seconds) where it is said or argued most directly, or null.
Reply with one JSON object: {{"chapters": [{{"t": <seconds>, "title": "..."}}], "points": [<seconds or null>, ...]}}"""


@task("chapters", "章节", "ai-quick", remote=True)
def pi_chapters(task, beat):
    """Chapters of a video, and where each key point of its summary is said, from its subtitles (one AI call
    without thinking: ~¥0.03 for an hour and a half). The watch page lists both; a click jumps there."""
    jid, n = task["payload"]["job"], task["payload"]["part"]
    rows = q("SELECT t, text FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕' AND t IS NOT NULL ORDER BY t",
             (jid, n))
    if len(rows) < 20:
        return {"skipped": "too few subtitles"}
    blocks, cur, start = [], [], None
    for r in rows:  # ~30-second blocks keep the input small
        if start is None:
            start = r["t"]
        cur.append(r["text"])
        if r["t"] - start >= 30:
            blocks.append(f"[{int(start // 3600)}:{int(start % 3600 // 60):02}:{int(start % 60):02}] {' '.join(cur)}")
            cur, start = [], None
    if cur:
        blocks.append(f"[{int(start // 3600)}:{int(start % 3600 // 60):02}:{int(start % 60):02}] {' '.join(cur)}")
    a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
    points = (a.get("key_points") or []) if n == 0 else []
    user = "SUBTITLES:\n" + "\n".join(blocks)[:80000] + "\n\nKEY POINTS:\n" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points))
    out = llm_json(CHAPTERS_SYSTEM, user, {"chapters": "array", "points": "array"}, 4000, {}, "chapters", jid, think=False)
    end = rows[-1]["t"]
    chapters = sorted(({"t": float(c["t"]), "title": str(c.get("title") or "")[:24]} for c in out.get("chapters") or []
                       if isinstance(c, dict) and isinstance(c.get("t"), (int, float)) and 0 <= c["t"] <= end + 60),
                      key=lambda c: c["t"])
    times = [float(t) if isinstance(t, (int, float)) and 0 <= t <= end + 60 else None for t in (out.get("points") or [])]
    a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
    a.setdefault("chapters", {})[str(n)] = chapters
    if n == 0 and len(times) == len(points):
        a["point_times"] = times
    update(jid, analysis=a)
    return {"chapters": len(chapters), "points": sum(t is not None for t in times)}


@task("index_subs", "整理已有字幕", "light", remote=True)
def pi_index_subs(task, beat):
    path, subs = task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]
    if not subs:
        return {"lines": 0}
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (jid, n))
    cues = _cues(subs[0], Path(subs[0]).stat().st_mtime)  # the first track; others are mostly the same lines translated
    for t, text in cues:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'字幕',?)", (jid, n, t, text))
    maybe_translate(task, path, subs)
    publish("chapters", task["target"], task["priority"] - 1, parent=task["id"], force=True)
    return {"lines": len(cues), "file": Path(subs[0]).name}


def job_thumb(task):
    row = q("SELECT thumb FROM jobs WHERE id=?", (task["payload"]["job"],), one=True)
    if not row or not row["thumb"] or not Path(row["thumb"]).exists():
        raise ValueError("no cover")
    return Path(row["thumb"])


@task("cover", "识别封面", "cpu", prefer="gpu", remote=True, then="save_cover")
def pi_cover(task, beat):
    """What the cover looks like and the text on it, worked out on the Pi's CPU (~30 s; the Mac takes ~0.2 s)."""
    thumb, started = job_thumb(task), time.time()
    v = clip_image(thumb)
    return {"vector": vec_to(v) if v is not None else None, "lines": ocr_text(thumb), "seconds": round(time.time() - started, 1)}


@task("save_cover", "保存封面识别", "light")
def pi_save_cover(task, beat):
    jid, res = task["payload"]["job"], parent_result(task)
    q("DELETE FROM vec WHERE kind='job' AND ref=? AND src='封面'", (jid,))
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND src='封面文字'", (jid,))
    if res.get("vector"):
        store_vec("job", jid, 0, None, "封面", vec_from(res["vector"]))
    for line in res.get("lines") or []:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,0,NULL,'封面文字',?)", (jid, str(line)))
    log_usage("clip", "cover", jid, amount=1, seconds=float(res.get("seconds") or 0))
    drop_parent_result(task, {"lines": len(res.get("lines") or [])})
    return {"lines": len(res.get("lines") or [])}


def keyframes(path, out_dir):
    """The video's keyframes (every few seconds; the encoder puts them at cuts too) as 224x224 JPEGs named by
    time. Only keyframes are decoded, so the Pi does an hour in about a minute and a half."""
    proc = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "info", "-nostats", "-skip_frame", "nokey", "-i", str(path),
                           "-an", "-fps_mode", "vfr", "-vf", "scale=224:224:flags=bicubic,showinfo", "-q:v", "4",
                           str(out_dir / "%06d.jpg")], capture_output=True, text=True)
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)]
    files = sorted(out_dir.glob("*.jpg"))
    return [(t, f) for t, f in zip(times, files)]


@task("frames", "识别画面", "cpu", prefer="gpu", remote=True, then="save_frames")
def pi_frames(task, beat):
    """What's on screen, keyframe by keyframe, on the Pi's CPU (~3 s a frame; the Mac does them 100x faster).
    A frame much like the last one kept is skipped."""
    path, _ = task_media(task)
    started, kept, prev = time.time(), [], None
    with tempfile.TemporaryDirectory(dir=INCOMPLETE / "tmp") as tmp:
        frames = keyframes(path, Path(tmp))
        for i, (t, f) in enumerate(frames):
            if i % 20 == 0 and (not pi_idle() or not beat({"pct": round(i / max(len(frames), 1) * 100)})):
                raise Paused()  # starts over next time: the keyframes are quick, the vectors are the slow part
            v = clip_image(f)
            if v is not None and (prev is None or float(v @ prev) < 0.85):  # same shot as the last kept: skip
                kept.append([round(t, 2), vec_to(v)])
                prev = v
    return {"frames": kept, "keyframes": len(frames), "seconds": round(time.time() - started, 1)}


@task("save_frames", "保存画面识别", "light")
def pi_save_frames(task, beat):
    jid, n, res = task["payload"]["job"], task["payload"]["part"], parent_result(task)
    q("DELETE FROM vec WHERE kind='job' AND ref=? AND part=? AND src='画面'", (jid, n))
    for t, b64 in res.get("frames") or []:
        store_vec("job", jid, n, float(t), "画面", vec_from(b64))
    log_usage("clip", "frames", jid, amount=res.get("keyframes") or 0, seconds=float(res.get("seconds") or 0))
    publish("similar", f"job:{jid}", task["priority"] - 3, parent=task["id"], force=True)
    drop_parent_result(task, {"kept": len(res.get("frames") or []), "keyframes": res.get("keyframes")})
    return {"kept": len(res.get("frames") or [])}


@task("transcribe", "转文字", "cpu", prefer="gpu", remote=True, then=after_transcribe)
def pi_transcribe(task, beat):
    """Speech-to-text on the Pi's CPU (whisper small, ~1x realtime) when no Mac is around: 90 seconds at a time,
    the progress kept on the board, so a pause loses nothing."""
    from faster_whisper import WhisperModel
    import numpy as np
    path, _ = task_media(task)
    prog = task["progress"] if (task["progress"] or {}).get("by") == "pi" else {}
    until, segs, lang = prog.get("until", 0), prog.get("segs", []), prog.get("language") or task["payload"].get("language")
    dur, chunk, started = duration_of(str(path)) or 0, 90, time.time()
    model = None
    try:
        while until < dur:
            if not pi_idle():
                raise Paused()
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(until), "-i", str(path), "-t", str(chunk), "-vn",
                                  "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
            if not pcm:
                break
            audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            if model is None:
                model = WhisperModel(IDLE_WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4,
                                     download_root=str(STATE / "models"))
            with heavy_slot("whisper"):
                if lang is None:
                    _, info = model.transcribe(audio[:30 * 16000], beam_size=1)
                    lang = info.language
                # the prompt keeps Chinese in simplified characters (search is by simplified text)
                found, _ = model.transcribe(audio, language=lang, vad_filter=True, beam_size=1,
                                            initial_prompt="以下是普通话的句子，用简体中文。" if lang == "zh" else None,
                                            **WHISPER_FAST)
                segs += [[round(until + s.start, 2), round(until + s.end, 2), s.text.strip()] for s in found if s.text.strip()]
            until += chunk
            if not beat({"by": "pi", "until": until, "segs": segs, "language": lang, "pct": round(min(until / dur, 1) * 100)}):
                raise Paused()  # the task isn't ours any more
    finally:
        prog_seconds = time.time() - started
    return {"language": lang, "segments": segs, "audio_seconds": round(min(until, dur), 1), "seconds": round(prog_seconds, 1)}


def run_claimed(task, worker):
    """Do a claimed task here and report back."""
    beat = lambda progress=None: heartbeat_task(task["id"], worker, progress)  # noqa: E731
    try:
        result = TASK_KINDS[task["kind"]]["run"](task, beat)
    except Paused:
        release_task(task["id"], worker)
        return "paused"
    except ValueError as e:  # the target is gone or doesn't fit: no point trying again
        fail_task(task["id"], worker, e, retry=False)
        return "failed"
    except Exception as e:
        traceback.print_exc()
        fail_task(task["id"], worker, e)
        return "failed"
    complete_task(task["id"], worker, result)
    return "done"


def mem_available_mb():
    try:
        return next(int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable"))
    except Exception:
        return 0


def box_busy():
    """Something someone is waiting for needs the CPU: a download being processed, a note being transcribed.
    (Plain downloading doesn't count: it's network-bound.)"""
    return bool(q("SELECT 1 FROM jobs WHERE status='processing' LIMIT 1", one=True)
                or q("SELECT 1 FROM tasks WHERE kind='note_media' AND state='running' LIMIT 1", one=True))


def pi_idle():
    return not box_busy() and mem_available_mb() > 500


def light_loop(pool="light"):
    """Worker thread: writing results into the library ("light"), or AI calls ("ai", "ai-quick"); no CPU to speak
    of, so done right away, here."""
    worker = pi_worker(pool)
    while True:
        mark = bell_mark("tasks")
        try:
            claimed = claim_task(*worker)
            if claimed:
                run_claimed(claimed, worker[0])
                continue
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 60)  # a publish rings; a delayed retry or a lease running out within the minute


def heavy_loop():
    """Worker thread: CPU work (speech-to-text when no Mac is around, covers) when nothing else needs the CPU,
    one task at a time in its own lowest-priority process, which pauses (progress kept) when something comes in."""
    time.sleep(30)
    while True:
        mark = bell_mark("tasks")
        try:
            if IDLE_WORK and not box_busy() and mem_available_mb() > 1000:
                claimed = claim_task(*pi_worker("cpu"))
                if claimed:
                    subprocess.run(["nice", "-n", "19", sys.executable, __file__, "task", str(claimed["id"]), PI_WORKERS["cpu"]])
                    continue
            else:
                seen_worker(*pi_worker("cpu"))
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 120)  # a publish or a finished download rings


def now_loop():
    """Worker thread: CPU work someone is waiting for (a note just made), right away, in its own process."""
    worker = pi_worker("now")
    while True:
        mark = bell_mark("tasks")
        try:
            claimed = claim_task(*worker)
            if claimed:
                subprocess.run(["nice", "-n", "15", sys.executable, __file__, "task", str(claimed["id"]), worker[0]])
                continue
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 60)


def run_task_process(tid, worker):
    """`grabber.py task N WORKER`: the process for one task a CPU worker claimed."""
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, worker), one=True)
    if row:
        run_claimed(task_dict(row), worker)


def board_summary():
    """The board for 资源使用: per kind how many wait / run / are done / failed, who's working on what."""
    kinds = {k: {"label": v["label"], "queued": 0, "running": 0, "done": 0, "failed": 0, "hours": 0.0}
             for k, v in TASK_KINDS.items()}
    for r in q("SELECT kind, state, COUNT(*) n, SUM(CASE WHEN state IN ('queued','running') "
               "THEN json_extract(payload, '$.duration') ELSE 0 END) secs FROM tasks GROUP BY kind, state"):
        if r["kind"] in kinds:
            kinds[r["kind"]][r["state"]] = r["n"]
            kinds[r["kind"]]["hours"] += (r["secs"] or 0) / 3600
    for k in kinds.values():
        k["hours"] = round(k["hours"], 1)
    running = [{"id": r["id"], "kind": r["kind"], "label": TASK_KINDS.get(r["kind"], {}).get("label", r["kind"]),
                "worker": r["worker"], "title": json.loads(r["payload"]).get("title"),
                "pct": (json.loads(r["progress"] or "{}") or {}).get("pct")}
               for r in q("SELECT * FROM tasks WHERE state='running' ORDER BY updated DESC")]
    failed = [{"id": r["id"], "label": TASK_KINDS.get(r["kind"], {}).get("label", r["kind"]),
               "title": json.loads(r["payload"]).get("title"), "error": (r["error"] or "")[:200]}
              for r in q("SELECT * FROM tasks WHERE state='failed' ORDER BY updated DESC LIMIT 5")]
    workers = [{"name": r["name"], "caps": json.loads(r["caps"]), "seen": r["seen"],
                "online": time.time() - r["seen"] < WORKER_FRESH, "task": r["task"],
                "paused": json.loads(r["paused"])["why"] if r["paused"] else None}
               for r in q("SELECT * FROM workers ORDER BY seen DESC")]
    return {"kinds": kinds, "running": running, "failed": failed, "workers": workers, "enabled": IDLE_WORK,
            "busy": box_busy()}


def publish_chapters_once():
    """Once: chapters for the videos that have subtitles already (off-peak: half price)."""
    if kv_get("board_chapters"):
        return
    when = offpeak_from()
    for r in q("SELECT DISTINCT ref, part FROM seg WHERE kind='job' AND src='字幕'"):
        try:
            publish("chapters", f"job:{r['ref']}:{r['part']}", 5, not_before=when)
        except ValueError:
            pass
    kv_set("board_chapters", True)


def publish_pending_notes():
    """Notes whose attachments the old loop hadn't done yet (before notes were tasks)."""
    for r in q("SELECT id FROM notes WHERE pending=1"):
        try:
            publish("note_media", f"note:{r['id']}", 60)
        except ValueError:
            pass


def publish_similar_once():
    """Once: look for re-uploads and clips among what's already there (after its frames and subtitles are in)."""
    if kv_get("board_similar"):
        return
    for r in q("SELECT id FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        try:
            publish("similar", f"job:{r['id']}", 1)
        except ValueError:
            pass
    kv_set("board_similar", True)


def publish_translations_once():
    """Once: English videos already in the library get Chinese subtitles too."""
    if kv_get("board_translate"):
        return
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        for n, m in enumerate(playable(job_dict(row))):
            if m["subs"]:
                fake = {"target": f"job:{r['id']}:{n}", "priority": 9 if r["backfill"] else 29, "id": None}
                maybe_translate(fake, Path(m["path"]), m["subs"])
    kv_set("board_translate", True)


def publish_frames_once():
    """Once (boards made before frames were a task): what's on screen in every video there is."""
    if kv_get("board_frames"):
        return
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        for n, m in enumerate(playable(job_dict(row))):
            if Path(m["path"]).suffix.lower() in VIDEO_EXT:
                publish("frames", f"job:{r['id']}:{n}", 9 if r["backfill"] else 29)
    kv_set("board_frames", True)


def migrate_to_board():
    """Once: the work the old idle loop still had to do becomes tasks (newest videos get the higher ids, so they go
    first); its bookkeeping tables go."""
    if kv_get("board_migrated"):
        return
    states = {r["key"]: r["state"] for r in q("SELECT key, state FROM idle")} if \
        q("SELECT name FROM sqlite_master WHERE name='idle'", one=True) else {}
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        a = json.loads(row["analysis"] or "{}")
        music = a.get("library") == "Music" or a.get("folder") == "Music Videos"
        pri = 10 if r["backfill"] else 30
        for n, m in enumerate(playable(job_dict(row))):
            if Path(m["path"]).suffix.lower() in VIDEO_EXT:
                publish("frames", f"job:{r['id']}:{n}", pri - 1)
            if m["subs"] and states.get(f"cues:{r['id']}:{n}") != "done" and states.get(f"subs:{r['id']}:{n}") != "done":
                publish("index_subs", f"job:{r['id']}:{n}", pri)
            elif not m["subs"] and not music and states.get(f"subs:{r['id']}:{n}") != "done":
                publish("transcribe", f"job:{r['id']}:{n}", pri)
        if row["thumb"] and Path(row["thumb"]).exists() and states.get(f"cover:{r['id']}") != "done":
            publish("cover", f"job:{r['id']}", pri)
    _write("DROP TABLE IF EXISTS lease")
    kv_set("board_frames", True)  # a fresh migration already published frames below
    _write("DROP TABLE IF EXISTS idle")
    kv_set("board_migrated", True)
    kv_set("idle_now", None)
    kv_set("mac_seen", None)


# ---------------------------------------------------------------- 资源使用: what the box has been doing

XRAY = shutil.which("xray") or "/usr/local/bin/xray"


def record_traffic():
    """Add the traffic since the last look to today's totals. Counters restart from zero when Xray or the Pi
    restarts; a counter lower than last time is taken as such a restart."""
    now = {}
    try:
        out = subprocess.run([XRAY, "api", "statsquery", "--server=127.0.0.1:10085"], capture_output=True, text=True,
                             timeout=20).stdout
        for st in json.loads(out or "{}").get("stat", []):
            kind, name, _, way = (st["name"].split(">>>") + ["", "", "", ""])[:4]
            if kind == "outbound" and name not in ("block", "dns-out", "api"):
                route = "direct" if name == "direct" else "proxy"
                now[f"{st['name']}"] = (f"{route}_{'up' if way == 'uplink' else 'down'}", int(st.get("value") or 0))
    except Exception:
        pass
    try:
        for line in open("/proc/net/dev"):
            if line.strip().startswith(("wlan0:", "eth0:")):
                dev, rest = line.split(":", 1)
                f = rest.split()
                now[dev.strip() + ":rx"] = ("pi_down", int(f[0]))
                now[dev.strip() + ":tx"] = ("pi_up", int(f[8]))
    except OSError:
        pass
    last = kv_get("traffic_last", {})
    day = time.strftime("%Y-%m-%d")
    for counter, (bucket, value) in now.items():
        before = last.get(counter)
        delta = value if before is None or value < before else value - before
        if before is not None and delta:
            q("INSERT INTO traffic (day, name, bytes) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET "
              "bytes = bytes + excluded.bytes", (day, bucket, delta))
        last[counter] = value
    kv_set("traffic_last", last)


def read_health():
    """The Pi now: CPU temperature, fan, load, memory, SD card, and the disks' SMART readings (written by the
    root timer disk-health, which reads them every 15 minutes without waking sleeping disks)."""
    def first(path, conv=str):
        try:
            return conv(open(path).read().strip())
        except Exception:
            return None
    try:
        disks = json.load(open("/run/disk-health.json"))["disks"]
    except Exception:
        disks = []
    sd = shutil.disk_usage("/")
    return {"cpu_temp": (first("/sys/class/thermal/thermal_zone0/temp", int) or 0) / 1000 or None,
            "fan": first("/run/fan-level"), "load": os.getloadavg(), "cores": os.cpu_count(),
            "mem_available": mem_available_mb(), "mem_total": next((int(l.split()[1]) // 1024 for l in open("/proc/meminfo")
                                                                    if l.startswith("MemTotal")), None),
            "uptime": first("/proc/uptime", lambda x: float(x.split()[0])), "sd_free": sd.free, "sd_total": sd.total,
            "disks": disks}


def record_health():
    h = read_health()
    q("INSERT INTO health (ts, cpu, disks) VALUES (?,?,?)",
      (time.time(), h["cpu_temp"], json.dumps({d["serial"]: d["temp"] for d in h["disks"] if not d.get("asleep")})))
    q("DELETE FROM health WHERE ts < ?", (time.time() - 30 * 86400,))


def health_summary():
    """Now, the day's highs, and what's worth a warning (thresholds: WD Red/white-label drives are rated to 65 °C;
    above ~55 °C wear goes up; any reallocated/pending/uncorrectable sector means the disk has started failing)."""
    h = read_health()
    day = q("SELECT cpu, disks FROM health WHERE ts > ?", (time.time() - 86400,))
    h["cpu_max"] = max([r["cpu"] or 0 for r in day] + [h["cpu_temp"] or 0]) or None
    highs = {}
    for r in day:
        for serial, t in json.loads(r["disks"] or "{}").items():
            if t is not None:
                highs[serial] = max(highs.get(serial, 0), t)
    for d in h["disks"]:
        if d.get("temp") is not None and not d.get("asleep"):
            highs[d["serial"]] = max(highs.get(d["serial"], 0), d["temp"])
    warnings = []
    if (h["cpu_temp"] or 0) >= 75:
        warnings.append(f"CPU {h['cpu_temp']:.0f}°C，偏热")
    for d in h["disks"]:
        d["temp_max"] = highs.get(d["serial"])
        name = f"{d['mount'] or d['dev']}（{round((d.get('size') or 0) / 1e12)}TB）"
        if d.get("passed") is False:
            warnings.append(f"{name} SMART 自检不通过，尽快换盘")
        bad = (d.get("reallocated") or 0) + (d.get("pending") or 0) + (d.get("uncorrectable") or 0)
        if bad:
            warnings.append(f"{name} 有 {bad} 个坏扇区（重映射/待处理/不可修复），开始老化了")
        hot = max(d.get("temp") or 0, d.get("temp_max") or 0)
        if hot >= 55:
            warnings.append(f"{name} 最高到过 {hot}°C，{'过热' if hot >= 60 else '偏热'}，注意散热")
    h["warnings"] = warnings
    return h


def traffic_loop():
    while True:
        try:
            record_traffic()
        except Exception:
            traceback.print_exc()
        try:
            record_health()
        except Exception:
            traceback.print_exc()
        try:
            publish_digests()
        except Exception:
            traceback.print_exc()
        try:
            record_balance()
        except Exception:
            traceback.print_exc()
        time.sleep(300)


def backfill_usage():
    """Once: what was done before usage was recorded, from the jobs themselves."""
    if kv_get("usage_backfilled"):
        return
    for r in q("SELECT id, kind, files, analysis, updated FROM jobs WHERE status='done' AND ref IS NULL"):
        size = sum(Path(f).stat().st_size for f in json.loads(r["files"] or "[]") if Path(f).exists())
        a = json.loads(r["analysis"] or "{}")
        u = a.get("usage") or {}
        q("INSERT INTO usage (ts, kind, purpose, job_id, amount) VALUES (?,?,?,?,?)", (r["updated"], "download", r["kind"], r["id"], size))
        if u.get("calls"):
            q("INSERT INTO usage (ts, kind, purpose, job_id, amount, tokens_in) VALUES (?,?,?,?,?,?)",
              (r["updated"], "llm", "earlier", r["id"], u["calls"], u.get("tokens") or 0))
    kv_set("usage_backfilled", True)


USAGE_NAMES = {("llm", "classify"): "AI 分类（看标题和简介）", ("llm", "summarize"): "AI 总结（看字幕）",
               ("llm", "earlier"): "AI 分类+总结（统计开始前，未细分）", ("llm", "translate"): "AI 翻译字幕",
               ("llm", "digest"): "AI 追更周报", ("llm", "chapters"): "AI 章节", ("llm", "tags"): "AI 合并同义标签",
               ("whisper", "job"): "语音转文字 · 新下载（抽样 6 分钟）", ("whisper", "note"): "语音转文字 · 随记",
               ("whisper", "idle"): "语音转文字 · 闲时生成字幕（Pi）",
               ("whisper", "mac"): "语音转文字 · 完整字幕（Mac）", ("encode", "plex"): "转码（Plex / 手机能播）",
               ("ocr", "picture"): "识别图中文字", ("clip", "cover"): "识别封面", ("clip", "frames"): "识别视频画面"}


@app.get("/api/usage")
def usage_summary():
    days = int(request.args.get("days", "30")) if request.args.get("days", "").isdigit() else 30
    since = time.time() - days * 86400
    day0 = time.strftime("%Y-%m-%d", time.localtime(since))
    traffic = {}
    for r in q("SELECT day, name, bytes FROM traffic WHERE day >= ? ORDER BY day", (day0,)):
        traffic.setdefault(r["day"], {})[r["name"]] = r["bytes"]
    total = {r["name"]: r["b"] for r in q("SELECT name, SUM(bytes) b FROM traffic WHERE day >= ? GROUP BY name", (day0,))}
    jobs = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    work = []
    for r in q("SELECT kind, purpose, COUNT(*) n, SUM(amount) amount, SUM(tokens_in) tin, SUM(tokens_out) tout, "
               "SUM(seconds) secs, SUM(cost) cost, SUM(MAX(cache_hit, 0)) hit, SUM(cache_hit < 0) guessed, COUNT(cost) priced "
               "FROM usage WHERE ts >= ? GROUP BY kind, purpose ORDER BY kind, purpose", (since,)):
        work.append({"kind": r["kind"], "purpose": r["purpose"], "name": USAGE_NAMES.get((r["kind"], r["purpose"]),
                     f"{r['kind']} {r['purpose']}"), "count": r["n"], "amount": r["amount"] or 0,
                     "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0, "seconds": r["secs"] or 0,
                     "cost": r["cost"] if r["priced"] else None, "cache_hit": r["hit"] or 0, "guessed": r["guessed"] or 0})
    bal = q("SELECT ts, currency, total FROM balance ORDER BY ts", ())
    charged = sum(max(0, a["total"] - b["total"]) for a, b in zip(bal, bal[1:]) if b["ts"] >= since)  # rises are top-ups
    finished = q("SELECT COUNT(*) n FROM usage WHERE kind='download' AND ts >= ?", (since,), one=True)["n"]
    return jsonify(days=days, traffic=traffic, traffic_total=total, jobs=jobs, finished=finished, work=work,
                   balance={"total": bal[-1]["total"], "currency": bal[-1]["currency"], "since": bal[0]["ts"],
                            "charged": round(charged, 4)} if bal else None,
                   board=board_summary(), health=health_summary(),
                   index={"lines": q("SELECT COUNT(*) n FROM seg", one=True)["n"],
                          "pictures": q("SELECT COUNT(*) n FROM vec", one=True)["n"]},
                   disk={"free": shutil.disk_usage(MEDIA).free, "total": shutil.disk_usage(MEDIA).total})


# ---------------------------------------------------------------- the task board over HTTP (the Mac mini, scripts)
#
# mac/mac_worker.py on the Mac mini claims tasks here (pull: the Pi never has to reach the Mac, and a Mac that's
# asleep or off simply stops asking) and publishes its own. LAN only, with COMPUTE_TOKEN.

WHISPER_LANGS = {"zh": ("中文", "汉语", "普通话", "国语", "chinese", "mandarin", "zh"), "yue": ("粤语", "广东话", "cantonese"),
                 "en": ("英语", "英文", "english", "en"), "ja": ("日语", "日文", "japanese", "ja"),
                 "ko": ("韩语", "韩文", "korean", "ko"), "fr": ("法语", "french"), "de": ("德语", "german"),
                 "es": ("西班牙语", "spanish"), "ru": ("俄语", "russian")}


def whisper_lang(language):
    """The classifier's idea of the language ("中文", "English", "zh-CN"...) as a Whisper code, if it's clearly one.
    Whisper guessing from a few seconds goes wrong on films with little talk (a Japanese film came out as Korean)."""
    text = str(language or "").casefold()
    found = {code for code, names in WHISPER_LANGS.items()
             if any(n == text or n == text.split("-")[0] or ((len(n) > 2 or not n.isascii()) and n in text) for n in names)}
    return found.pop() if len(found) == 1 else None  # several ("中英双语"): let Whisper find out


def compute_auth():
    if g.external or not COMPUTE_TOKEN or not hmac.compare_digest(request.headers.get("X-Compute-Token", ""), COMPUTE_TOKEN):
        return jsonify(error="forbidden"), 403
    return None


def _worker():
    return str((request.get_json(silent=True) or {}).get("worker") or request.args.get("worker") or "")[:40] or None


@app.post("/api/tasks/publish")
def api_publish():
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    kind, target = str(body.get("kind", "")), str(body.get("target", ""))
    if not TASK_KINDS.get(kind, {}).get("remote"):
        return jsonify(error=f"may not publish {kind!r}"), 403
    try:
        tid = publish(kind, target, int(body.get("priority") or 20), by=_worker() or "remote", force=bool(body.get("force")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=tid)


@app.post("/api/tasks/claim")
def api_claim():
    """The next task this worker can do (caps), waiting up to `wait` seconds (at most 25) for one to come in."""
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    worker, caps = _worker(), [str(c) for c in body.get("caps") or []]
    if not worker:
        return jsonify(error="worker name missing"), 400
    if body.get("paused"):
        # still there, just not taking tasks for a while (a game in front): others keep leaving it its kinds,
        # until it's been paused for PREFER_WAIT
        row = q("SELECT paused, seen FROM workers WHERE name=?", (worker,), one=True)
        since = json.loads(row["paused"]).get("since") if row and row["paused"] else time.time()
        if time.time() - since < PREFER_WAIT_PAUSED:
            seen_worker(worker, caps, paused=json.dumps({"why": str(body["paused"])[:40], "since": since}))
        return jsonify(task=None)
    deadline = time.time() + min(float(body.get("wait") or 0), 25)
    while True:
        mark = bell_mark("tasks")
        task = claim_task(worker, caps)
        if task or time.time() >= deadline:
            return jsonify(task=task)
        bell_wait("tasks", mark, max(0.1, deadline - time.time()))


@app.post("/api/tasks/<int:tid>/heartbeat")
def api_heartbeat(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    return jsonify(ok=heartbeat_task(tid, _worker(), body.get("progress")))


@app.post("/api/tasks/<int:tid>/done")
def api_done(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    if not complete_task(tid, _worker(), body.get("result") or {}):
        return jsonify(error="not your task (any more)"), 409
    return jsonify(ok=True)


@app.post("/api/tasks/<int:tid>/fail")
def api_fail(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    return jsonify(ok=fail_task(tid, _worker(), body.get("error", ""), retry=bool(body.get("retry", True))))


@app.post("/api/tasks/ingest")
def api_ingest():
    """The Mac's drop folder (~/拾光投递): a file dropped there becomes a 随记 of `account`, with the file's date."""
    if (denied := compute_auth()):
        return denied
    request.max_content_length = NOTE_MAX_UPLOAD
    account = str(request.form.get("account", ""))
    if not q("SELECT 1 FROM users WHERE name=?", (account,), one=True):
        return jsonify(error=f"no account {account!r}"), 400
    g.owner, g.device_label = f"user:{account}", "Mac mini · 投递"
    return note_add()


@app.post("/api/tasks/<int:tid>/release")
def api_release(tid):
    """Not a failure: the worker is wanted for something else (a game on the Mac). Back on the board, progress kept."""
    if (denied := compute_auth()):
        return denied
    release_task(tid, _worker())
    return jsonify(ok=True)


@app.get("/api/tasks/<int:tid>/audio")
def api_task_audio(tid):
    """The task's video's first sound track as it is (no re-encoding: the Pi only unpacks it, ~60 MB an hour of
    AAC); the worker decodes it. Re-encoding here made the Pi the bottleneck: 2 minutes for 27 minutes of sound."""
    if (denied := compute_auth()):
        return denied
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, _worker()), one=True)
    if not row:
        return jsonify(error="not your task"), 404
    try:
        path, _ = task_media({"payload": json.loads(row["payload"])})
    except ValueError as e:
        return jsonify(error=str(e)), 404
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "matroska", "-"],
                            stdout=subprocess.PIPE)

    def stream():
        try:
            while chunk := proc.stdout.read(1 << 16):
                yield chunk
        finally:
            proc.kill()
            proc.wait()
    return Response(stream(), mimetype="audio/x-matroska")


def my_task(tid):
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, _worker()), one=True)
    return task_dict(row) if row else None


@app.get("/api/tasks/<int:tid>/keyframes")
def api_task_keyframes(tid):
    """The task's video's keyframes as a tar of 224x224 JPEGs named by their time in seconds (~3 MB for 24 min)."""
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    if not task:
        return jsonify(error="not your task"), 404
    import tarfile
    path, _ = task_media(task)
    (INCOMPLETE / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=INCOMPLETE / "tmp") as tmp:
        frames = keyframes(path, Path(tmp))
        out = tempfile.NamedTemporaryFile(dir=INCOMPLETE / "tmp", suffix=".tar", delete=False)
        with tarfile.open(fileobj=out, mode="w") as tar:
            for t, f in frames:
                tar.add(f, arcname=f"{t:.2f}.jpg")
        out.close()

    def stream():
        try:
            with open(out.name, "rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk
        finally:
            os.unlink(out.name)
    return Response(stream(), mimetype="application/x-tar")


@app.get("/api/tasks/<int:tid>/note-file")
def api_task_note_file(tid):
    """One attachment of the note a note_media task is about."""
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    name = request.args.get("file", "")
    if not task or name not in [f["file"] for f in task["payload"].get("files", [])]:
        return jsonify(error="not your task / not its file"), 404
    return send_file(NOTES_DIR / name, conditional=True)


@app.get("/api/tasks/<int:tid>/cover")
def api_task_cover(tid):
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    if not task:
        return jsonify(error="not your task"), 404
    return send_file(job_thumb(task))


@app.get("/api/tasks")
def api_board():
    """The board (for `task board` on the Mac and for 资源使用)."""
    if g.external and not g.user and (denied := compute_auth()):
        return denied
    return jsonify(board_summary())


# ---------------------------------------------------------------- 随记: everyday notes

NOTE_KINDS = {"image": "image/", "video": "video/", "audio": "audio/"}


def note_kind(f):
    kind = next((k for k, p in NOTE_KINDS.items() if (f.mimetype or "").startswith(p)), None)
    if kind:
        return kind
    ext = Path(f.filename or "").suffix.lower()
    return "image" if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"} else \
        "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else None


def note_row(nid):
    """The note if it's this browser's / account's own: notes are private, the admin doesn't see other people's."""
    row = q("SELECT * FROM notes WHERE id=?", (nid,), one=True)
    return row if row and row["owner"] == g.owner else None


def _within(a, b, k):
    """Whether the edit distance between a and b is at most k."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > k:
            return False
        prev = cur
    return prev[-1] <= k


@functools.lru_cache(maxsize=20000)
def _pinyin(ch):
    try:
        from pypinyin import lazy_pinyin
        return lazy_pinyin(ch)[0]
    except Exception:  # pypinyin not installed: no pinyin search
        return ""


def _note_find(hay, low, tok):
    """Where `tok` (one search word, casefolded) is in a note: as typed, with a typo or a letter more or less
    (Yannan ~ yanan), or as the pinyin / pinyin initials of Chinese (yanan, ya → 延安). The matched text, or None."""
    i = low.find(tok)
    if i >= 0:
        return hay[i:i + len(tok)]
    if not re.fullmatch(r"[a-z0-9]+", tok):
        return None
    if len(tok) >= 4:
        k = 1 if len(tok) < 8 else 2
        for m in re.finditer(r"[a-z0-9]+", low):
            if _within(tok, m.group(), k):
                return hay[m.start():m.end()]
    if not tok.isalpha() or len(tok) < 2:
        return None
    for run in re.finditer(r"[\u4e00-\u9fff]+", hay):
        py = [_pinyin(c) for c in run.group()]
        for a in range(len(py)):
            full = initials = ""
            for b in range(a, len(py)):
                full += py[b]
                initials += py[b][:1]
                if full == tok or initials == tok or (len(tok) >= 4 and _within(tok, full, 1)) \
                        or (b > a and full.startswith(tok) and len(tok) > len(full) - len(py[b])):
                    return run.group()[a:b + 1]
                if len(full) > len(tok) + 1 and len(initials) >= len(tok):
                    break
    return None


# ---- where a photo or video was taken: its GPS, named offline from GeoNames' cities (nothing is sent anywhere)

GEO_DIR = STATE / "models" / "geo"
COUNTRY_ZH = {"CN": "", "HK": "香港", "MO": "澳门", "TW": "台湾", "US": "美国", "CA": "加拿大", "JP": "日本", "KR": "韩国",
              "GB": "英国", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙", "PT": "葡萄牙", "NL": "荷兰",
              "CH": "瑞士", "AT": "奥地利", "TH": "泰国", "SG": "新加坡", "MY": "马来西亚", "ID": "印度尼西亚", "VN": "越南",
              "PH": "菲律宾", "AU": "澳大利亚", "NZ": "新西兰", "IN": "印度", "RU": "俄罗斯", "AE": "阿联酋", "TR": "土耳其",
              "EG": "埃及", "MX": "墨西哥", "BR": "巴西", "IS": "冰岛", "NO": "挪威", "SE": "瑞典", "FI": "芬兰", "DK": "丹麦"}
_places = {}


def place_name(lat, lon):
    """The biggest town within ~30 km (else the nearest one): "上海", "温莎 · 加拿大". (GeoNames lists city
    districts too; the nearest alone gave "黄浦" for the Bund and "Yoyogi" for Tokyo.)"""
    import numpy as np
    if "names" not in _places:
        import zipfile
        from opencc import OpenCC
        cc = OpenCC("t2s")
        rows = []
        with zipfile.ZipFile(GEO_DIR / "cities15000.zip") as z:
            for line in z.read("cities15000.txt").decode().splitlines():
                f = line.split("\t")
                # the Chinese name most of its Chinese alternatives agree on (深圳/深圳市 over an old name 宝安)
                alts = [cc.convert(a).removesuffix("市") for a in f[3].split(",") if re.fullmatch(r"[\u4e00-\u9fff]{1,8}", a)]
                zh = max(set(alts), key=lambda a: (alts.count(a), -len(a))) if alts else None
                if f[7] == "PPLX":  # a section of a city
                    continue
                rows.append((float(f[4]), float(f[5]), zh or f[1], f[8],
                             int(f[14] or 0)))
        _places["xy"] = np.radians(np.array([(r[0], r[1]) for r in rows]))
        _places["names"] = [(r[2], r[3]) for r in rows]
        _places["pop"] = np.array([r[4] for r in rows], np.float64)
    la, lo = np.radians(lat), np.radians(lon)
    xy = _places["xy"]
    d = (xy[:, 0] - la) ** 2 + ((xy[:, 1] - lo) * np.cos(la)) ** 2  # flat-earth distance: fine at this scale
    nearest = int(np.argmin(d))
    # the biggest town nearby in the same country (Windsor stays in Canada, not Detroit across the river)
    near = [i for i in np.where(d < (30 / 6371) ** 2)[0] if _places["names"][i][1] == _places["names"][nearest][1]]
    best = max(near, key=lambda i: _places["pop"][i]) if near else nearest
    name, country = _places["names"][best]
    zh = COUNTRY_ZH.get(country, country)
    return f"{name} · {zh}" if zh and zh != name else name


def photo_gps(path):
    """(lat, lon) from a photo's EXIF, or None (most pictures sent from a phone's browser have it stripped)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            gps = im.getexif().get_ifd(0x8825)
        if not gps or 2 not in gps or 4 not in gps:
            return None
        def deg(v, ref):
            x = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -x if ref in ("S", "W") else x
        return deg(gps[2], gps.get(1, "N")), deg(gps[4], gps.get(3, "E"))
    except Exception:
        return None


def video_gps(path):
    """(lat, lon) from a phone video's metadata (ISO 6709, e.g. "+31.2304+121.4737+004.000/"), or None."""
    tags = ffprobe(path).get("format", {}).get("tags", {})
    loc = next((v for k, v in tags.items() if "location" in k.lower()), "")
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", loc)
    return (float(m.group(1)), float(m.group(2))) if m else None


def note_marks(r, term):
    """The matched bits of text when every word of `term` is in the note (text or what's said in it), else None."""
    hay = r["text"] + "\n" + "\n".join(m.get("transcript", "") + "\n" + m.get("ocr", "") + "\n" + m.get("place", "")
                                       for m in json.loads(r["media"]))
    low, marks = hay.casefold(), []
    for tok in term.casefold().split():
        m = _note_find(hay, low, tok)
        if m is None:
            return None
        marks.append(m)
    return marks


def notes_search(term):
    """[(note, matched text bits, photos that look like it)]: by text, what's said or written in it, and by look."""
    rows = q("SELECT * FROM notes WHERE owner=? ORDER BY created DESC, id DESC", (g.owner,))
    looks = visual_hits(term, "note", {r["id"] for r in rows})
    out = []
    for r in rows:
        marks = note_marks(r, term)
        files = [m["file"] for m in json.loads(r["media"])]
        seen = [files.index(src[3:]) for _, _, _, src in looks.get(r["id"], []) if src[3:] in files]
        if marks is not None or seen:
            out.append((r, marks or [], seen))
    return out


def note_dict(r, marks=None, seen=None):
    media = json.loads(r["media"])
    d = {"id": r["id"], "text": r["text"], "created": r["created"], "updated": r["updated"], "device": r["device"],
         "pending": bool(r["pending"]),
         "media": [{"kind": m["kind"], "duration": m.get("duration"), "poster": bool(m.get("poster")),
                    "transcript": m.get("transcript", ""), "place": m.get("place")} for m in media],
         "place": next((m["place"] for m in media if m.get("place")), None)}
    if marks:
        d["marks"] = marks
        missing = [k for k in marks if k not in r["text"]]
        if missing:  # found in what's said in a voice note / video, or written on a photo: show where
            d["match"], d["match_where"] = next(((snippet(m.get(k, ""), missing[0]), label) for m in media
                                                 for k, label in (("transcript", "语音"), ("ocr", "图中文字"))
                                                 if missing[0] in m.get(k, "")), ("", ""))
    if seen:
        d["seen"] = seen  # photos that look like the search
        if not marks:
            d["match"], d["match_where"] = "照片看起来像", "照片"
    return d


def save_note_files(nid, files):
    NOTES_DIR.mkdir(mode=0o700, exist_ok=True)
    out = []
    for f in files:
        kind = note_kind(f)
        if not kind:
            continue
        ext = (Path(f.filename or "").suffix.lower() or {"image": ".jpg", "video": ".mp4", "audio": ".m4a"}[kind])[:8]
        name = f"{nid}-{secrets.token_hex(4)}{ext}"
        f.save(NOTES_DIR / name)
        out.append({"kind": kind, "file": name, "todo": True})
    return out


def note_time(v):
    """A note date from the page (unix seconds), or None when missing or absurd."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t if 0 < t < time.time() + 366 * 86400 else None


@app.get("/api/notes")
def notes_list():
    term = request.args.get("q", "").strip()
    where, args = "owner = ?", [g.owner]
    # newest first by the note's date (which can be changed); 更早的 continues after the last one shown
    before, before_id = note_time(request.args.get("before")), request.args.get("before_id", "")
    if before and before_id.isdigit():
        where, args = where + " AND (created < ? OR (created = ? AND id < ?))", args + [before, before, int(before_id)]
    if term:  # the text and what's said in voice notes and videos; forgiving (see _note_find)
        found = notes_search(term)
        return jsonify(notes=[note_dict(r, m, seen) for r, m, seen in found[:200]], more=False)
    rows = q(f"SELECT * FROM notes WHERE {where} ORDER BY created DESC, id DESC LIMIT 51", args)
    extra = {}
    if not before:  # first page: 那年今天, and the places your notes were made
        today = time.strftime("%m-%d")
        extra["onthisday"] = [note_dict(r) for r in q(
            "SELECT * FROM notes WHERE owner=? AND strftime('%m-%d', created, 'unixepoch', 'localtime')=? "
            "AND strftime('%Y', created, 'unixepoch', 'localtime') < strftime('%Y', 'now', 'localtime') "
            "ORDER BY created DESC", (g.owner, today))]
        places = {}
        for r in q("SELECT media FROM notes WHERE owner=?", (g.owner,)):
            for p in {m.get("place") for m in json.loads(r["media"]) if m.get("place")}:
                places[p] = places.get(p, 0) + 1
        extra["places"] = sorted(places.items(), key=lambda x: -x[1])[:20]
    return jsonify(notes=[note_dict(r, term) for r in rows[:50]], more=len(rows) > 50, **extra)


@app.post("/api/notes")
def note_add():
    text = (request.form.get("text") or "").strip()[:20000]
    files = request.files.getlist("media")
    if not text and not files:
        return jsonify(error="空的"), 400
    now = time.time()
    with db_lock:
        cur = DB.execute("INSERT INTO notes (owner, text, device, created, updated) VALUES (?,?,?,?,?)",
                         (g.owner, text, g.device_label, note_time(request.form.get("created")) or now, now))
        DB.commit()
        nid = cur.lastrowid
    media = save_note_files(nid, files)
    q("UPDATE notes SET media=?, pending=? WHERE id=?",
      (json.dumps(media, ensure_ascii=False), int(any(m["todo"] for m in media)), nid))
    if media:
        publish("note_media", f"note:{nid}", 60, force=True)
    return jsonify(note=note_dict(note_row(nid)))


@app.post("/api/notes/<int:nid>")
def note_edit(nid):
    """Change the text and/or drop attachments (`drop`: their positions)."""
    row = note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    media = json.loads(row["media"])
    drop = {int(i) for i in body.get("drop", []) if str(i).isdigit()}
    for i in drop:
        if i < len(media):
            for k in ("file", "poster"):
                if media[i].get(k):
                    (NOTES_DIR / media[i][k]).unlink(missing_ok=True)
            q("DELETE FROM vec WHERE kind='note' AND ref=? AND src=?", (nid, "照片:" + media[i]["file"]))
    media = [m for i, m in enumerate(media) if i not in drop]
    text = str(body.get("text", row["text"])).strip()[:20000]
    if not text and not media:
        return note_delete(nid)
    created = note_time(body.get("created")) or row["created"]
    q("UPDATE notes SET text=?, media=?, created=?, updated=? WHERE id=?",
      (text, json.dumps(media, ensure_ascii=False), created, time.time(), nid))
    return jsonify(note=note_dict(note_row(nid)))


@app.post("/api/notes/<int:nid>/media")
def note_add_media(nid):
    """More photos / videos / voice for a note that's already there."""
    row = note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    added = save_note_files(nid, request.files.getlist("media"))
    if not added:
        return jsonify(error="不是照片、视频或音频"), 400
    row = note_row(nid)  # re-read: saving big files takes a while
    q("UPDATE notes SET media=?, pending=1, updated=? WHERE id=?",
      (json.dumps(json.loads(row["media"]) + added, ensure_ascii=False), time.time(), nid))
    publish("note_media", f"note:{nid}", 60, force=True)
    return jsonify(note=note_dict(note_row(nid)))


@app.post("/api/notes/<int:nid>/delete")
def note_delete(nid):
    row = note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    for m in json.loads(row["media"]):
        for k in ("file", "poster"):
            if m.get(k):
                (NOTES_DIR / m[k]).unlink(missing_ok=True)
    q("DELETE FROM notes WHERE id=?", (nid,))
    q("DELETE FROM vec WHERE kind='note' AND ref=?", (nid,))
    return jsonify(ok=True)


@app.get("/notefile/<int:nid>/<int:n>")
@app.get("/notefile/<int:nid>/<int:n>/<what>")
def note_file(nid, n, what="file"):
    row = note_row(nid)
    media = json.loads(row["media"]) if row else []
    if n >= len(media) or what not in ("file", "poster") or not media[n].get(what):
        return "", 404
    path = NOTES_DIR / media[n][what]
    if not path.exists():
        return "", 404
    return send_file(path, conditional=True, max_age=86400)  # Range requests: videos seek, iPhones play them


def note_files(nid):
    row = q("SELECT media FROM notes WHERE id=?", (nid,), one=True)
    return [m for m in json.loads(row["media"]) if m.get("todo")] if row else []


@task("note_media", "识别随记附件", "now", prefer="gpu", prefer_wait=90, then="save_note_media")
def pi_note_media(task, beat):
    """Photos: the text on them and what they look like; videos: what's said and what the first frame looks like;
    voice: what's said. Worked out here when the Mac doesn't take it within a minute and a half."""
    out = {}
    for m in note_files(task["payload"]["note"]):
        path, item = NOTES_DIR / m["file"], {}
        try:
            if m["kind"] == "image":
                item["ocr"] = "\n".join(ocr_text(path))
                v = clip_image(path)
            else:
                item["duration"] = float(ffprobe(path).get("format", {}).get("duration") or 0) or None
                v = clip_image(path, t=min(1.0, (item["duration"] or 2) / 2)) if m["kind"] == "video" else None
                # the prompt steers Chinese towards simplified characters (search is by simplified text)
                text, _ = transcribe(None, path, None, prompt="以下是普通话的日常随记，用简体中文。", model=NOTE_WHISPER_MODEL,
                                     purpose="note")
                item["transcript"] = (text or "").strip()
            item["vector"] = vec_to(v) if v is not None else None
        except Exception as e:
            traceback.print_exc()
            item["error"] = str(e)[:200]
        out[m["file"]] = item
    return {"items": out}


@task("save_note_media", "保存随记识别", "light")
def pi_save_note_media(task, beat):
    """Write what was worked out into the note; the Pi adds what needs the file itself (a video's poster, where a
    photo or video was taken)."""
    nid = task["payload"]["note"]
    items = parent_result(task).get("items") or {}
    row = q("SELECT media FROM notes WHERE id=?", (nid,), one=True)
    if not row:
        return {"skipped": "note deleted"}
    media = json.loads(row["media"])
    for m in media:
        it = items.get(m["file"])
        if it is None:
            continue
        path = NOTES_DIR / m["file"]
        m.update({k: it[k] for k in ("ocr", "transcript", "duration") if k in it}, todo=False)
        try:
            gps = photo_gps(path) if m["kind"] == "image" else video_gps(path) if m["kind"] == "video" else None
            if gps:
                m["gps"], m["place"] = [round(gps[0], 5), round(gps[1], 5)], place_name(*gps)
            if m["kind"] == "video" and grab_frame(path, path.with_suffix(".poster.jpg")):
                m["poster"] = path.with_suffix(".poster.jpg").name
        except Exception:
            traceback.print_exc()
        q("DELETE FROM vec WHERE kind='note' AND ref=? AND src=?", (nid, "照片:" + m["file"]))
        if it.get("vector"):
            store_vec("note", nid, 0, None, "照片:" + m["file"], vec_from(it["vector"]))
    q("UPDATE notes SET media=?, pending=? WHERE id=?",
      (json.dumps(media, ensure_ascii=False), int(any(m.get("todo") for m in media)), nid))
    drop_parent_result(task, {"items": len(items)})
    return {"items": len(items)}


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
    forget_index(jid)
    if with_files and shared:
        return "kept_shared", 0
    return "removed", files


def point_times(jid):
    """When each of the summary's key points is talked about: the 45-second stretch of subtitles that shares the
    most (rarer) two-character pieces with the point. Free (no AI), and good enough to jump close to it;
    points that match nothing well get no time."""
    import math
    row = q("SELECT analysis, ref FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    points = a.get("key_points") or []
    source = row["ref"] or jid  # an entry linked to another account's download shares its subtitles
    lines = q("SELECT part, t, text FROM seg WHERE kind='job' AND ref=? AND src IN ('字幕','译文') AND t IS NOT NULL "
              "ORDER BY src='译文' DESC, part, t", (source,))
    if not points or not lines:
        return []
    zh_first = any(r["text"] and re.search(r"[\u4e00-\u9fff]", r["text"]) for r in lines[:50])
    lines = [r for r in lines if not zh_first or re.search(r"[\u4e00-\u9fff]", r["text"] or "")]

    def grams(text):
        t = re.sub(r"[\W_]+", "", text.casefold())
        return {t[i:i + 2] for i in range(len(t) - 1)}
    windows = []
    for i, r in enumerate(lines):
        text, j = [], i
        while j < len(lines) and lines[j]["part"] == r["part"] and lines[j]["t"] - r["t"] < 45:
            text.append(lines[j]["text"])
            j += 1
        windows.append((r["part"], r["t"], grams(" ".join(text))))
    df = {}
    for _, _, gr in windows:
        for x in gr:
            df[x] = df.get(x, 0) + 1
    idf = {x: math.log(len(windows) / n) for x, n in df.items()}
    out = []
    for p in points:
        pg = grams(p)
        total = sum(idf.get(x, 0) for x in pg) or 1
        best = max(windows, key=lambda w: sum(idf.get(x, 0) for x in pg & w[2]))
        score = sum(idf.get(x, 0) for x in pg & best[2]) / total
        out.append({"part": best[0], "t": round(best[1], 1)} if score >= 0.3 else None)
    return out


@app.get("/api/points/<int:jid>")
def key_point_times(jid):
    if not visible(jid):
        return jsonify(error="not found"), 404
    row = q("SELECT analysis, ref FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    if row["ref"]:  # linked entry: the downloading job has the chapters
        a = {**json.loads(q("SELECT analysis FROM jobs WHERE id=?", (row["ref"],), one=True)["analysis"] or "{}"), **a}
    times = [{"part": 0, "t": t} if t is not None else None for t in a["point_times"]] if a.get("point_times") \
        else point_times(jid)
    return jsonify(times=times, chapters=a.get("chapters") or {})


@app.get("/api/similar/<int:jid>")
def similar_of(jid):
    """Same content elsewhere in the library: re-uploads, clips of it, or what it's a clip of."""
    if not visible(jid):
        return jsonify(error="not found"), 404
    out = []
    for r in q("SELECT * FROM similar WHERE a=? OR b=?", (jid, jid)):
        other = r["b"] if r["a"] == jid else r["a"]
        if not visible(other):
            continue
        mine, theirs = (r["a_in_b"], r["b_in_a"]) if r["a"] == jid else (r["b_in_a"], r["a_in_b"])
        row = q("SELECT title, analysis FROM jobs WHERE id=?", (other,), one=True)
        rel = "same" if r["kind"] == "same" else "part_of" if mine >= theirs else "has_part"
        out.append({"job": other, "rel": rel, "mine": mine, "theirs": theirs,
                    "title": json.loads(row["analysis"] or "{}").get("title") or row["title"]})
    return jsonify(similar=out)


@app.get("/api/digests")
def digests_list():
    rows = q("SELECT * FROM digests WHERE owner=? ORDER BY end DESC, id DESC LIMIT 8", (g.owner,))
    pending = q("SELECT state FROM tasks WHERE kind='digest' AND target LIKE ? AND state IN ('queued','running')",
                (f"digest:{g.owner}:%",), one=True)
    return jsonify(digests=[{"id": r["id"], **json.loads(r["body"]), "created": r["created"]} for r in rows],
                   pending=bool(pending))


@app.post("/api/digests/now")
def digest_now():
    """The last 7 days, now (the automatic one comes on Monday mornings)."""
    if not q("SELECT 1 FROM subs WHERE owner=?", (g.owner,), one=True):
        return jsonify(error="还没有追更的 UP 主"), 400
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    publish("digest", f"digest:{g.owner}:{start}:{end}", 70, force=True)
    return jsonify(ok=True)


@app.post("/api/watch/<int:jid>")
def save_watch(jid):
    """Where playback is (sent every ~10 s, on pause and when the page is left): picked up on any device."""
    if not visible(jid):
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    try:
        pos, dur, part = float(body.get("pos") or 0), float(body.get("dur") or 0), int(body.get("part") or 0)
    except (TypeError, ValueError):
        return jsonify(error="bad position"), 400
    done = bool(body.get("ended")) or (dur > 0 and (pos > dur - 30 or pos / dur > 0.95))
    prev = q("SELECT done FROM watch WHERE owner=? AND job_id=?", (g.owner, jid), one=True)
    q("INSERT INTO watch (owner, job_id, part, pos, dur, done, updated) VALUES (?,?,?,?,?,?,?) "
      "ON CONFLICT(owner, job_id) DO UPDATE SET part=excluded.part, pos=excluded.pos, dur=excluded.dur, "
      "done=excluded.done, updated=excluded.updated",
      # once seen to the end it stays 已看完, even when it's watched again
      (g.owner, jid, part, pos, dur, int(done or bool(prev and prev["done"])), time.time()))
    return jsonify(ok=True, done=done)


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
             "subs": [Path(x).name[len(Path(m["path"]).stem) + 1:].rsplit(".", 1)[0] or "字幕" for x in m["subs"]],
             "bilingual": f"/subs/{jid}/{i}/bi?t={media_token(jid, i)}" if bilingual_pair(m) else None}
            for i, m in enumerate(playable(job))]


def bilingual_pair(m):
    """(Chinese, English) subtitle files of a media item, if it has both."""
    stem = Path(m["path"]).stem
    by = {sub_lang(x, stem): x for x in m["subs"]}
    zh = next((by[k] for k in by if k.split("-")[0] == "zh"), None)
    en = by.get("en") or next((by[k] for k in by if k.split("-")[0] == "en"), None)
    return (zh, en) if zh and en else None


@app.get("/subs/<int:jid>/<int:n>/bi")
def subs_bilingual(jid, n):
    """Chinese above English in one track: the Chinese cues, each with the English said meanwhile under it."""
    m = job_media(jid, n)
    pair = bilingual_pair(m) if m else None
    if not pair:
        return "", 404
    zh, en = srt_cues(pair[0]), srt_cues(pair[1])

    def ts(t):
        h, rem = divmod(t, 3600)
        mm, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(mm):02}:{sec:06.3f}"
    out, j = ["WEBVTT", ""], 0
    same = len(zh) == len(en) and all(abs(x[0] - y[0]) < 0.01 for x, y in zip(zh, en))  # translated line by line
    for i, (a, b, t) in enumerate(zh):
        if same:
            said = en[i][2]
        else:  # different files: the English said mostly within this cue
            while j < len(en) and en[j][1] <= a:
                j += 1
            said = " ".join(x for s, e, x in en[j:j + 4] if min(b, e) - max(a, s) > (e - s) / 2)
        out += [f"{ts(a)} --> {ts(b)}", t] + ([said] if said else []) + [""]
    return Response("\n".join(out), mimetype="text/vtt")


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


@app.get("/frame/<int:jid>/<int:n>")
def frame(jid, n):
    """The frame `t` seconds into a video (a search found something there), cached."""
    m = job_media(jid, n)
    try:
        t = max(0.0, float(request.args.get("t", "0")))
    except ValueError:
        return "", 400
    if not m:
        return "", 404
    out = MEDIA / ".cache" / "frames" / f"{jid}-{n}-{int(t)}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", m["path"], "-frames:v", "1", "-vf", "scale=640:-2",
                        str(out)], capture_output=True, timeout=60)
    return send_file(out, max_age=86400) if out.exists() else ("", 404)


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
    # big uploads (随记 videos) are spooled to temp files: keep those on the disks, not the SD card / RAM
    tmp = INCOMPLETE / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)


def main_web():
    init_db()
    setup_app()
    listen_bell("web")
    ensure_admin()
    threading.Thread(target=warm_probes, daemon=True).start()
    if (CLIP_DIR / "text.onnx").exists():
        threading.Thread(target=clip_text, args=("拾光",), daemon=True).start()  # first search needn't wait for it
    from waitress import serve
    serve(app, listen=f"0.0.0.0:{PORT} 127.0.0.1:{EXTERNAL_PORT}", threads=16, channel_timeout=300,
          ident="shiguang", max_request_body_size=NOTE_MAX_UPLOAD, trusted_proxy="127.0.0.1", trusted_proxy_count=1,
          trusted_proxy_headers="x-forwarded-for", clear_untrusted_proxy_headers=True)


def main_worker():
    init_db(reset=True)
    setup_app()
    listen_bell("worker")
    INCOMPLETE.mkdir(parents=True, exist_ok=True)
    for _ in range(DOWNLOAD_WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=sweep_loop, daemon=True).start()
    threading.Thread(target=sub_loop, daemon=True).start()
    threading.Thread(target=now_loop, daemon=True).start()
    migrate_to_board()
    publish_frames_once()
    publish_translations_once()
    publish_similar_once()
    publish_pending_notes()
    publish_chapters_once()
    threading.Thread(target=backfill_published, daemon=True).start()
    threading.Thread(target=light_loop, daemon=True).start()
    threading.Thread(target=light_loop, args=("ai",), daemon=True).start()
    threading.Thread(target=light_loop, args=("ai-quick",), daemon=True).start()
    threading.Thread(target=heavy_loop, daemon=True).start()
    threading.Thread(target=traffic_loop, daemon=True).start()
    backfill_usage()
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
    elif mode == "task":
        init_db()
        run_task_process(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else PI_WORKERS["cpu"])
    else:
        main_web()
