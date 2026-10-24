"""Changes to the database's shape, numbered and run once each (SQLite's user_version holds the last one done),
and data jobs that run once in the worker after an upgrade. New ones go at the end; never renumber.

Every step also checks for itself, so a database from before the numbering (user_version 0) can run them all."""
import json
import threading
import time
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


def _directory_crawls(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS crawls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, url TEXT NOT NULL,
        url_key TEXT NOT NULL, title TEXT DEFAULT '', status TEXT DEFAULT 'queued',
        max_pages INTEGER NOT NULL DEFAULT 50, merged INTEGER NOT NULL DEFAULT 0,
        current_url TEXT DEFAULT '', error TEXT DEFAULT '', created REAL, updated REAL,
        refresh_hours REAL NOT NULL DEFAULT 6, next_refresh REAL, last_refresh REAL,
        generation INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX IF NOT EXISTS crawls_owner ON crawls(owner, id);
    CREATE TABLE IF NOT EXISTS crawl_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, crawl_id INTEGER NOT NULL, url_key TEXT NOT NULL,
        url TEXT NOT NULL, kind TEXT NOT NULL, source TEXT DEFAULT '', title TEXT DEFAULT '',
        state TEXT DEFAULT 'queued', error TEXT DEFAULT '', fingerprint TEXT DEFAULT '',
        generation INTEGER NOT NULL DEFAULT 1,
        UNIQUE(crawl_id, url_key));
    CREATE INDEX IF NOT EXISTS crawl_pages_queue ON crawl_pages(crawl_id, state, kind, id);
    CREATE TABLE IF NOT EXISTS crawl_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, url TEXT NOT NULL,
        page_url TEXT NOT NULL, title TEXT DEFAULT '', description TEXT DEFAULT '',
        media_urls TEXT DEFAULT '[]', sources TEXT DEFAULT '[]', job_id INTEGER,
        created REAL, updated REAL);
    CREATE INDEX IF NOT EXISTS crawl_items_owner ON crawl_items(owner, id);
    CREATE TABLE IF NOT EXISTS crawl_aliases (
        owner TEXT NOT NULL, url_key TEXT NOT NULL, item_id INTEGER NOT NULL,
        PRIMARY KEY(owner, url_key));
    CREATE TABLE IF NOT EXISTS crawl_results (
        crawl_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
        PRIMARY KEY(crawl_id, item_id));
    CREATE TABLE IF NOT EXISTS crawl_downloads (
        job_id INTEGER PRIMARY KEY, context TEXT NOT NULL);
    """)


def _recycle_bin(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS job_trash (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
        job_id INTEGER, snapshot TEXT NOT NULL, deleted_at REAL NOT NULL,
        orphan INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS job_trash_owner ON job_trash(owner, deleted_at, id);
    CREATE INDEX IF NOT EXISTS job_trash_expiry ON job_trash(orphan, deleted_at);
    """)


def _directory_sources(db):
    add(db, 'crawls', 'source_classification', "TEXT NOT NULL DEFAULT 'unknown'")
    add(db, 'crawls', 'source_reason', "TEXT NOT NULL DEFAULT ''")
    add(db, 'crawls', 'source_checked', 'REAL')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS crawl_sources (
        owner TEXT NOT NULL, url_key TEXT NOT NULL,
        classification TEXT NOT NULL DEFAULT 'unknown', reason TEXT NOT NULL DEFAULT '', checked REAL,
        PRIMARY KEY(owner,url_key));
    CREATE TABLE IF NOT EXISTS crawl_source_jobs (
        owner TEXT NOT NULL, job_id INTEGER NOT NULL, url_key TEXT NOT NULL,
        PRIMARY KEY(owner,job_id,url_key));
    INSERT OR IGNORE INTO crawl_sources(owner,url_key) SELECT owner,url_key FROM crawls;
    INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key)
        SELECT c.owner,i.job_id,c.url_key FROM crawls c JOIN crawl_results r ON r.crawl_id=c.id
        JOIN crawl_items i ON i.id=r.item_id AND i.owner=c.owner WHERE i.job_id IS NOT NULL;
    INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key)
        SELECT j.owner,j.id,c.url_key FROM jobs j JOIN crawls c ON j.source='crawl:'||c.id AND j.owner=c.owner;
    INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key)
        SELECT t.owner,t.job_id,c.url_key FROM job_trash t JOIN crawls c
        ON json_extract(t.snapshot,'$.source')='crawl:'||c.id AND t.owner=c.owner WHERE t.job_id IS NOT NULL;
    """)


def _crawl_page_retries(db):
    add(db, 'crawl_pages', 'failures', 'INTEGER NOT NULL DEFAULT 0')
    add(db, 'crawl_pages', 'retry_at', 'REAL')
    # The incident ID links every affected page to one verified repair.
    add(db, 'crawl_pages', 'heal_reported', 'INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE INDEX IF NOT EXISTS crawl_pages_retry ON crawl_pages(crawl_id,generation,state,retry_at)')


def _saved_searches(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS saved_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL, name TEXT NOT NULL, query TEXT NOT NULL, mode TEXT NOT NULL,
        private INTEGER NOT NULL DEFAULT 0 CHECK(private IN (0,1)),
        created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(owner,query,mode,private));
    CREATE INDEX IF NOT EXISTS saved_searches_owner ON saved_searches(owner,updated,id);
    """)


def _source_inbox(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS source_inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'unread' CHECK(state IN ('unread','read','ignored')),
        created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS source_inbox_owner ON source_inbox(owner,state,created,id);
    CREATE TABLE IF NOT EXISTS source_inbox_aliases (
        owner TEXT NOT NULL, key TEXT NOT NULL, item_id INTEGER NOT NULL,
        PRIMARY KEY(owner,key));
    CREATE INDEX IF NOT EXISTS source_inbox_alias_item ON source_inbox_aliases(item_id);
    CREATE TABLE IF NOT EXISTS source_inbox_links (
        item_id INTEGER NOT NULL, source_kind TEXT NOT NULL, source_id INTEGER NOT NULL,
        source_key TEXT NOT NULL, content_kind TEXT NOT NULL, content_id INTEGER NOT NULL,
        PRIMARY KEY(item_id,source_kind,source_id,source_key,content_kind,content_id));
    CREATE INDEX IF NOT EXISTS source_inbox_content ON source_inbox_links(content_kind,content_id);
    CREATE TABLE IF NOT EXISTS source_inbox_meta (key TEXT PRIMARY KEY,value REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS source_inbox_baselines (
        source_id INTEGER NOT NULL, source_key TEXT NOT NULL, generation INTEGER NOT NULL,
        PRIMARY KEY(source_id,source_key));
    INSERT OR IGNORE INTO source_inbox_baselines(source_id,source_key,generation)
        SELECT id,url_key,generation FROM crawls;
    """)
    db.execute("INSERT OR IGNORE INTO source_inbox_meta(key,value) VALUES('started',?)", (time.time(),))


def _ai_activity(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ai_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        owner TEXT NOT NULL DEFAULT '', private INTEGER NOT NULL DEFAULT 0 CHECK(private IN (0,1)),
        job_id INTEGER, purpose TEXT NOT NULL, engine TEXT, model TEXT, status TEXT NOT NULL,
        fallback_code TEXT, queue_seconds REAL, run_seconds REAL, total_seconds REAL, retries INTEGER);
    CREATE INDEX IF NOT EXISTS ai_activity_owner ON ai_activity(owner,ts,id);
    CREATE INDEX IF NOT EXISTS ai_activity_time ON ai_activity(ts,id);
    """)


def _substring_search(db):
    from . import search_index
    search_index.install(db)


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
    (26, _directory_crawls),
    (27, _recycle_bin),
    (28, _directory_sources),
    (29, _crawl_page_retries),
    (30, _saved_searches),
    (31, _source_inbox),
    (32, _ai_activity),
    (33, _substring_search),
    (34, lambda db: (add(db, 'crawl_items', 'duration', 'REAL'), add(db, 'crawl_pages', 'duration', 'REAL'))),
    (35, lambda db: __import__('shiguang.directory_retired', fromlist=['clear_cache']).clear_cache(db)),
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
