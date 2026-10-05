"""Configuration, the database, wake-ups between processes and small helpers everything else uses."""
import contextlib
import fcntl
import json
import os
import re
import requests
import secrets
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.parse

from flask import Flask
from pathlib import Path


MEDIA = Path(os.environ.get("MEDIA_ROOT", "/mnt/media"))
INCOMPLETE = MEDIA / ".incomplete"
# 随记 files live on the disks, not the SD card; the folder is private to the grabber user (the Samba share is public)
NOTES_DIR = MEDIA / ".notes"
NOTE_MAX_UPLOAD = 4 << 30  # one note's files at most (phone videos are big)
# 书架: e-books (the file as sent, a cover, the readable "pack"); private to the grabber user like the notes
BOOKS_DIR = MEDIA / ".books"
BOOK_MAX_UPLOAD = 1 << 30  # one upload of books at most (scanned PDFs get big)
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
    -- 书架: e-books per account (private like 随记). `fmt` = what was sent (epub, txt, pdf, mobi, azw3), `view` =
    -- how it's read (flow: reflowed chapters; pdf: pages); `version` goes up when the file is replaced, so copies
    -- cached on phones know they're stale; `shelf` = 'want' for 想读
    CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, title TEXT DEFAULT '',
        author TEXT DEFAULT '', lang TEXT DEFAULT '', fmt TEXT, view TEXT DEFAULT '', file TEXT DEFAULT '', size INTEGER DEFAULT 0,
        sha1 TEXT, cover INTEGER DEFAULT 0, chars INTEGER DEFAULT 0, chapters INTEGER DEFAULT 0, toc TEXT DEFAULT '[]',
        status TEXT DEFAULT 'importing', error TEXT DEFAULT '', url TEXT DEFAULT '', shelf TEXT DEFAULT '',
        version INTEGER DEFAULT 1, device TEXT, created REAL, updated REAL);
    CREATE INDEX IF NOT EXISTS books_owner ON books (owner, id);
    -- a book's text per chapter (per page for PDFs): search, how much there is to read
    CREATE TABLE IF NOT EXISTS book_ch (book INTEGER, idx INTEGER, title TEXT, text TEXT, chars INTEGER,
        PRIMARY KEY (book, idx));
    -- where each account is in each book (继续阅读 on every device), reading time, 读完
    CREATE TABLE IF NOT EXISTS book_read (owner TEXT, book INTEGER, pos TEXT, pct REAL DEFAULT 0, done INTEGER DEFAULT 0,
        seconds REAL DEFAULT 0, started REAL, finished REAL, updated REAL, PRIMARY KEY (owner, book));
    CREATE TABLE IF NOT EXISTS book_marks (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, book INTEGER, pos TEXT,
        pct REAL, text TEXT, created REAL);
    -- time spent per day reading a book / watching a video, and how far it got that day (每周总结)
    CREATE TABLE IF NOT EXISTS activity (owner TEXT, day TEXT, kind TEXT, ref INTEGER, seconds REAL DEFAULT 0,
        pct0 REAL, pct1 REAL, PRIMARY KEY (owner, day, kind, ref));
    -- 每周总结: a week's numbers per account, kept as they were that Monday (what was still left to watch / read)
    CREATE TABLE IF NOT EXISTS weekly (owner TEXT, start TEXT, end TEXT, body TEXT, created REAL, PRIMARY KEY (owner, start));
    """)
    from . import migrations  # imports core: only here, at run time
    migrations.run(DB)
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


# ---------------------------------------------------------------- web

HERE = Path(__file__).resolve().parent.parent  # the app: grabber.py, index.html, static/
ENTRY = HERE / "grabber.py"  # what the worker starts job and task processes with


def secret_key():
    path = STATE / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


def _write(sql, args=()):
    """One statement; how many rows it changed (for compare-and-set updates other processes may race)."""
    with db_lock:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur.rowcount


def mem_available_mb():
    try:
        return next(int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable"))
    except Exception:
        return 0
