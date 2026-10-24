"""Optional trigram candidates; original scoped LIKE remains the final authority.

External-content indexes keep no second copy of transcripts. A small cursor marks
the built prefix, maintained by triggers while the remaining rows build in the
background. Until each table is complete, callers use their existing query.
"""
import fcntl
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import core


TABLES = {
    'jobs': ('jobs_search_fts', ('title', 'url', 'analysis', 'files', 'transcript'), 2),
    'seg': ('seg_search_fts', ('text',), 256),
}
_threads = {}
_thread_lock = threading.Lock()


def install(db):
    """Migration-only schema work: never scan or rebuild the source tables here."""
    db.execute('CREATE TABLE IF NOT EXISTS search_index_state ('
               'name TEXT PRIMARY KEY,cursor INTEGER,ready INTEGER NOT NULL DEFAULT 0,'
               "supported INTEGER NOT NULL DEFAULT 1,error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL DEFAULT 0)")
    for name, (fts, columns, _batch) in TABLES.items():
        db.execute('INSERT OR IGNORE INTO search_index_state(name) VALUES(?)', (name,))
        db.execute('SAVEPOINT search_index_install')
        try:
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5("
                       f"{','.join(columns)},content='{name}',content_rowid='id',"
                       "tokenize='trigram',detail=none,columnsize=0)")
            def covered(prefix):
                return (f"EXISTS(SELECT 1 FROM search_index_state WHERE name='{name}' AND supported=1 "
                        f"AND (ready=1 OR {prefix}.id<=cursor))")
            new = ','.join('new.' + column for column in columns)
            old = ','.join('old.' + column for column in columns)
            fields = ','.join(columns)
            changed = ' OR '.join(f'old.{column} IS NOT new.{column}' for column in ('id', *columns))
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {fts}_insert AFTER INSERT ON {name} '
                       f'WHEN {covered("new")} BEGIN INSERT INTO {fts}(rowid,{fields}) '
                       f'VALUES(new.id,{new}); END')
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {fts}_delete AFTER DELETE ON {name} '
                       f'WHEN {covered("old")} BEGIN INSERT INTO {fts}({fts},rowid,{fields}) '
                       f"VALUES('delete',old.id,{old}); END")
            # UPDATE OF avoids even entering the trigger for progress/updated,
            # and IS NOT avoids reindexing unchanged text assignments.
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {fts}_update AFTER UPDATE OF id,{fields} ON {name} '
                       f'WHEN {changed} BEGIN INSERT INTO {fts}({fts},rowid,{fields}) '
                       f"SELECT 'delete',old.id,{old} WHERE {covered('old')}; "
                       f'INSERT INTO {fts}(rowid,{fields}) SELECT new.id,{new} WHERE {covered("new")}; END')
            db.execute('UPDATE search_index_state SET supported=1,error=? WHERE name=?', ('', name))
            db.execute('RELEASE search_index_install')
        except sqlite3.OperationalError as exc:
            db.execute('ROLLBACK TO search_index_install')
            db.execute('RELEASE search_index_install')
            if not re.search(r'no such (?:module|tokenizer)', str(exc), re.I):
                raise
            db.execute('UPDATE search_index_state SET supported=0,ready=0,error=? WHERE name=?',
                       ('trigram_unavailable', name))


def status(db=None):
    if db is not None:
        return {row['name']: dict(row) for row in db.execute('SELECT * FROM search_index_state')}
    with core.db_lock:
        try:
            return status(core.DB) if core.DB is not None else {}
        except sqlite3.OperationalError:
            return {}  # Older schemas and unavailable optional indexes use LIKE.


def build_batch(db, name, limit=None):
    """Build one bounded batch on a dedicated connection; never commit a caller's transaction."""
    if name not in TABLES:
        raise ValueError('unknown search index')
    if db.in_transaction:
        raise RuntimeError('search index requires its own transaction')
    fts, columns, default = TABLES[name]
    limit = default if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid search index batch size')
    db.execute('BEGIN IMMEDIATE')
    try:
        state = db.execute('SELECT * FROM search_index_state WHERE name=?', (name,)).fetchone()
        if not state or not state['supported'] or state['ready']:
            db.rollback()
            return 0
        where, args = ('', ()) if state['cursor'] is None else ('WHERE id>?', (state['cursor'],))
        rows = db.execute(f"SELECT id,{','.join(columns)} FROM {name} {where} ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        if rows:
            db.executemany(f"INSERT INTO {fts}(rowid,{','.join(columns)}) VALUES({','.join('?' for _ in range(len(columns) + 1))})",
                           [tuple(row) for row in rows])
            db.execute("UPDATE search_index_state SET cursor=?,error='',updated=? WHERE name=?",
                       (rows[-1]['id'], time.time(), name))
        else:
            db.execute("UPDATE search_index_state SET ready=1,error='',updated=? WHERE name=?", (time.time(), name))
        db.commit()
        return len(rows)
    except BaseException:
        db.rollback()
        raise


def _anchors(query):
    if not isinstance(query, str) or len(query) < 3 or '\x00' in query:
        return None
    # Every anchor must occur in a true substring hit. Quoting each individual
    # three-character token also works with FTS5's compact detail=none format.
    last = len(query) - 3
    positions = sorted({round(last * index / 5) for index in range(6)})
    return ' AND '.join('"' + query[position:position + 3].replace('"', '""') + '"' for position in positions)


def _condition(name, query, mode, alias, db=None):
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias):
        raise ValueError('invalid SQL alias')
    if mode != 'exact' or not (anchors := _anchors(query)):
        return None
    state = status(db).get(name)
    if not state or not state['supported'] or not state['ready']:
        return None
    fts = TABLES[name][0]
    return f'{alias}.id IN (SELECT rowid FROM {fts} WHERE {fts} MATCH ?)', (anchors,)


def job_condition(query, mode='exact', alias='jobs', *, db=None):
    return _condition('jobs', query, mode, alias, db)


def segment_condition(query, mode='exact', alias='seg', *, db=None):
    return _condition('seg', query, mode, alias, db)


def _build(path, stop_event):
    # A process-local thread guard is insufficient: web and worker both start.
    with open(str(path) + '.search-index.lock', 'a') as lease:
        db = sqlite3.connect(str(path), timeout=1)
        db.row_factory = sqlite3.Row
        try:
            # Match core.db()'s WAL durability policy. The index and its cursor
            # commit together; rebuilding need not fsync the HDD on every batch.
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA cache_size=-2048')
            while not stop_event.is_set():
                pending = [name for name, state in status(db).items() if state['supported'] and not state['ready']]
                if not pending:
                    return
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stop_event.wait(2)
                    continue
                try:
                    while not stop_event.is_set():
                        pending = [name for name, state in status(db).items() if state['supported'] and not state['ready']]
                        if not pending:
                            return
                        for name in pending:
                            build_batch(db, name)
                            if stop_event.wait(.03):
                                return
                except sqlite3.Error as exc:
                    db.rollback()
                    try:
                        db.execute('UPDATE search_index_state SET error=?,updated=? WHERE ready=0 AND supported=1',
                                   (type(exc).__name__, time.time()))
                        db.commit()
                    except sqlite3.Error:
                        db.rollback()
                    # Busy writers and disk pressure must not spin or interrupt
                    # startup. The cursor resumes after the condition clears.
                    stop_event.wait(5)
                finally:
                    fcntl.flock(lease, fcntl.LOCK_UN)
        finally:
            db.close()


def start_background(stop_event=None):
    """Start after init_db(); no source rows are read on the calling thread."""
    path = Path(core.DB_PATH).resolve()
    with _thread_lock:
        previous = _threads.get(str(path))
        if previous is not None and previous.is_alive():
            return previous
        thread = threading.Thread(target=_build, args=(path, stop_event or threading.Event()),
                                  name='search-index', daemon=True)
        _threads[str(path)] = thread
        thread.start()
        return thread
