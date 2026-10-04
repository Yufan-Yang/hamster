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
