"""Changes to the database's shape, numbered and run once each (SQLite's user_version holds the last one done),
and data jobs that run once in the worker after an upgrade. New ones go at the end; never renumber.

Every step also checks for itself, so a database from before the numbering (user_version 0) can run them all."""
import json
import threading
import traceback

from .core import kv_get, kv_set, link_key, LLM_MODEL


def cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def add(db, table, column, decl):
    if column not in cols(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        return True
    return False


def _job_keys(db):
    if add(db, "jobs", "key", "TEXT"):
        for r in db.execute("SELECT id, url FROM jobs").fetchall():
            db.execute("UPDATE jobs SET key=? WHERE id=?", (link_key(r[1]), r[0]))


def _usage_costs(db):
    from . import llm
    if add(db, "usage", "cost", "REAL"):  # CNY, LLM calls only
        add(db, "usage", "cache_hit", "INTEGER DEFAULT 0")  # input tokens served from DeepSeek's cache
        for r in db.execute("SELECT id, ts, tokens_in, tokens_out FROM usage WHERE kind='llm' AND purpose != 'earlier'").fetchall():
            # before costs were kept: priced as if no input came from the cache (an upper bound)
            db.execute("UPDATE usage SET cost=?, cache_hit=-1 WHERE id=?", (llm.llm_cost(LLM_MODEL, 0, r[2], r[3], r[1]), r[0]))


def _backfill_flag(db):
    # an older video queued when following an uploader: speech-to-text waits for idle time
    if add(db, "jobs", "backfill", "INTEGER DEFAULT 0"):
        db.execute("UPDATE jobs SET backfill=1 WHERE status IN ('queued','downloading','processing','failed') AND source LIKE 'sub:%'")


def _live_clipping(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS live_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, name TEXT NOT NULL,
        room_id INTEGER NOT NULL, url TEXT NOT NULL, enabled INTEGER DEFAULT 1,
        settings TEXT DEFAULT '{}', checked REAL, live_status TEXT DEFAULT 'waiting', error TEXT DEFAULT '',
        created REAL, updated REAL, UNIQUE(owner, room_id));
    CREATE TABLE IF NOT EXISTS live_accounts (
        owner TEXT PRIMARY KEY, mid TEXT, name TEXT, status TEXT DEFAULT 'ready', updated REAL);
    CREATE TABLE IF NOT EXISTS live_sessions (
        key TEXT PRIMARY KEY, source_id INTEGER NOT NULL, owner TEXT NOT NULL, title TEXT DEFAULT '',
        status TEXT DEFAULT 'recording', cursor REAL DEFAULT 0, topic_start REAL DEFAULT 0,
        topic_title TEXT DEFAULT '', started REAL, ended REAL, error TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS live_recordings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, source_id INTEGER NOT NULL,
        session_key TEXT NOT NULL, seq INTEGER NOT NULL, offset REAL DEFAULT 0, duration REAL DEFAULT 0,
        path TEXT NOT NULL, state TEXT DEFAULT 'transcribing', segments TEXT DEFAULT '[]', error TEXT DEFAULT '',
        created REAL, updated REAL, UNIQUE(session_key, seq));
    CREATE INDEX IF NOT EXISTS live_recordings_session ON live_recordings(session_key, seq);
    CREATE TABLE IF NOT EXISTS live_clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, source_id INTEGER NOT NULL,
        session_key TEXT NOT NULL, lane TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
        title TEXT NOT NULL, description TEXT DEFAULT '', tags TEXT DEFAULT '[]', score REAL DEFAULT 0,
        scores TEXT DEFAULT '{}', reason TEXT DEFAULT '', state TEXT DEFAULT 'queued',
        file TEXT DEFAULT '', cover TEXT DEFAULT '', version INTEGER DEFAULT 1, render_version INTEGER DEFAULT 0,
        bvid TEXT DEFAULT '', aid TEXT DEFAULT '', error TEXT DEFAULT '', submit_started REAL,
        created REAL, updated REAL);
    CREATE INDEX IF NOT EXISTS live_clips_owner ON live_clips(owner, id);
    CREATE INDEX IF NOT EXISTS live_clips_session ON live_clips(session_key, lane, start, end);
    CREATE TABLE IF NOT EXISTS live_references (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, source_id INTEGER NOT NULL,
        bvid TEXT NOT NULL, title TEXT, author TEXT, published REAL, duration REAL, views INTEGER,
        metrics TEXT DEFAULT '{}', cohort TEXT DEFAULT 'high', state TEXT DEFAULT 'queued',
        path TEXT DEFAULT '', segments TEXT DEFAULT '[]', error TEXT DEFAULT '', created REAL, updated REAL,
        UNIQUE(source_id, bvid));
    CREATE TABLE IF NOT EXISTS live_rubrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, source_id INTEGER NOT NULL,
        version INTEGER NOT NULL, rubric TEXT NOT NULL, evidence TEXT DEFAULT '{}', active INTEGER DEFAULT 1,
        created REAL, UNIQUE(source_id, version));
    """)
    add(db, "live_sources", "calibrated", "REAL")
    add(db, "live_sources", "uid", "INTEGER")
    add(db, "live_recordings", "bytes", "INTEGER DEFAULT 0")
    add(db, "live_clips", "performance", "TEXT DEFAULT '{}' ")


def _live_review_and_origin(db):
    # Web and worker can start together; serialize column checks before either changes the schema.
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    add(db, 'live_sessions', 'origin', "TEXT DEFAULT '{}'")
    add(db, 'live_clips', 'needs_review', 'INTEGER DEFAULT 0')
    add(db, 'live_references', 'excluded', 'INTEGER DEFAULT 0')
    # Capture the attribution available at upgrade time; future sessions store it at recording start.
    db.execute("UPDATE live_sessions SET origin=(SELECT json_object('name',name,'room_id',room_id,'url',url,'uid',uid) "
               "FROM live_sources WHERE id=live_sessions.source_id) WHERE origin='{}'")


def _live_reference_evidence(db):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    add(db, 'live_references', 'reference_info', "TEXT DEFAULT '{}'")
    add(db, 'live_references', 'source_match', "TEXT DEFAULT 'pending'")
    add(db, 'live_references', 'match_reason', "TEXT DEFAULT ''")


def _live_editorial_titles(db):
    add(db, 'live_clips', 'cover_title', "TEXT DEFAULT ''")
    from .live_bilibili import publication_title
    for row in db.execute("SELECT c.id,c.title,s.origin,p.name,p.settings FROM live_clips c "
                          "JOIN live_sources p ON p.id=c.source_id LEFT JOIN live_sessions s ON s.key=c.session_key "
                          "WHERE c.state NOT IN ('uploading','submitted','uncertain')").fetchall():
        cid, title, origin, name, settings = row
        updated = publication_title({'title': title, 'origin': origin or '{}'}, {'name': name, 'settings': settings})
        if updated != title:
            db.execute('UPDATE live_clips SET title=? WHERE id=?', (updated, cid))


def _live_topic_review(db):
    add(db, 'live_sessions', 'topic_summary', "TEXT DEFAULT ''")
    add(db, 'live_sessions', 'topic_kind', "TEXT DEFAULT 'content'")
    add(db, 'live_clips', 'content_kind', "TEXT DEFAULT 'content'")
    add(db, 'live_clips', 'merged_into', 'INTEGER')


STEPS = [
    (1, lambda db: add(db, "jobs", "transcript", "TEXT DEFAULT ''")),
    (2, lambda db: add(db, "jobs", "owner", "TEXT")),
    (3, lambda db: add(db, "privacy", "pin", "TEXT")),  # privacy password for devices without an account
    (4, lambda db: (add(db, "jobs", "attempts", "INTEGER DEFAULT 0"), add(db, "jobs", "retry_at", "REAL"))),
    (5, lambda db: add(db, "jobs", "ref", "INTEGER")),  # job whose files this entry shares
    (6, _job_keys),
    (7, lambda db: add(db, "jobs", "device", "TEXT")),  # which device sent it, e.g. "Mac · Chrome"
    (8, lambda db: add(db, "devices", "label", "TEXT")),
    (9, lambda db: add(db, "workers", "paused", "TEXT")),  # why a worker isn't taking tasks (a game...)
    (10, _usage_costs),
    (11, lambda db: add(db, "jobs", "segments", "TEXT")),  # speech-to-text lines with their times
    (12, _backfill_flag),
    (13, lambda db: add(db, "jobs", "cancel", "INTEGER DEFAULT 0")),  # set by the page, read by the job
    # which image model made a picture vector: vectors of different models can't be compared
    (14, lambda db: add(db, "vec", "model", "TEXT DEFAULT 'chinese-clip-vit-base-patch16-int8'")),
    (15, lambda db: add(db, "notes", "tags", "TEXT DEFAULT '[]'")),  # AI tags of a 随记
    (16, lambda db: add(db, "jobs", "advice", "TEXT DEFAULT ''")),  # why a download failed and what to do (AI)
    (17, lambda db: add(db, "users", "shortcut_key", "TEXT")),  # the iOS shortcut's secret, so it works from outside too
    # 追更 filters: which videos of the uploader to cache; `refilter` = changed, the next check looks back again
    (18, lambda db: (add(db, "subs", "filter", "TEXT DEFAULT ''"), add(db, "subs", "refilter", "INTEGER DEFAULT 0"))),
    (19, _live_clipping),
    (20, _live_review_and_origin),
    (21, _live_reference_evidence),
    (22, _live_editorial_titles),
    (23, _live_topic_review),
    (24, lambda db: add(db, 'live_sources', 'archived', 'INTEGER NOT NULL DEFAULT 0')),
    (25, lambda db: add(db, 'live_clips', 'removed_at', 'REAL')),
]


def run(db):
    done = db.execute("PRAGMA user_version").fetchone()[0]
    for n, step in STEPS:
        if n > done:
            step(db)
            db.execute(f"PRAGMA user_version = {n}")
    db.commit()


# ---- data jobs after an upgrade, once each, in the worker (named by the kv flag that records them)

ONCE = []


def once(name, background=False):
    def register(fn):
        ONCE.append((name, fn, background))
        return fn
    return register


def run_once_jobs():
    for name, fn, background in ONCE:
        if kv_get(name):
            continue

        def go(name=name, fn=fn):
            try:
                fn()
                kv_set(name, True)
            except Exception:
                traceback.print_exc()  # tried again at the next start
        if background:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()


__all__ = ["run", "once", "run_once_jobs", "json"]
