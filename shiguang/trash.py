"""Owner-scoped recycle bin and recovery of unclaimed legacy library files."""
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

from flask import g, jsonify, request

from . import core
from .core import AUDIO_EXT, MEDIA, SUB_EXT, VIDEO_EXT, app, db_lock, q

RETENTION_DAYS = 30
RETENTION_SECONDS = RETENTION_DAYS * 86400


def _json(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _paths(row):
    paths = [value for value in _json(row.get('files'), []) if isinstance(value, str)]
    if row.get('thumb') and isinstance(row['thumb'], str):
        paths.append(row['thumb'])
    return list(dict.fromkeys(paths))


def _safe_path(value, *, root=None):
    try:
        root = MEDIA.resolve() if root is None else root
        path = Path(value).resolve()
        parts, base = path.parts, root.parts
        return path if len(parts) > len(base) and parts[:len(base)] == base else None
    except (ValueError, OSError, RuntimeError, TypeError):
        return None


class _FileProtection:
    """Resolve references once per transaction; index directory and sidecar protection."""
    def __init__(self, references):
        self.root = MEDIA.resolve()
        self.paths = {_safe_path(value, root=self.root) for value in references} - {None}
        self.exact = {path.parts for path in self.paths}
        self.ancestors = set()
        self.sidecars = {}
        start = len(self.root.parts)
        for path in self.paths:
            parts = path.parts
            self.ancestors.update(parts[:end] for end in range(start, len(parts)))
            if path.suffix.lower() in VIDEO_EXT | AUDIO_EXT:
                self.sidecars.setdefault(parts[:-1], set()).add(path.stem + '.')

    def overlaps(self, path):
        parts = path.parts
        return (parts in self.ancestors or
                any(parts[:end] in self.exact for end in range(len(self.root.parts), len(parts) + 1)))

    def shared(self, path):
        return self.overlaps(path) or any(path.name.startswith(prefix)
                                          for prefix in self.sidecars.get(path.parts[:-1], ()))


def _referenced(db, except_trash=None):
    paths = set()
    for row in db.execute('SELECT files, thumb FROM jobs').fetchall():
        paths.update(_paths(dict(row)))
    for row in db.execute('SELECT id, snapshot FROM job_trash').fetchall():
        if row['id'] != except_trash:
            paths.update(_paths(_json(row['snapshot'], {})))
    return paths


def move_owner(old, new):
    if old != new:
        # The ledger owner is authoritative; restoration updates the snapshot owner.
        q('UPDATE job_trash SET owner=? WHERE owner=?', (new, old))


def snapshot_private(db, owner, job_id, snapshot):
    """Keep per-entry privacy independent of an archived/reused database ID.

    Tags remain governed by the current account preferences; this flag captures
    an explicit hidden choice or inherited source classification.
    """
    if type(snapshot.get('_trash_private')) is bool:
        return snapshot['_trash_private']
    prefs = db.execute('SELECT ids FROM privacy WHERE owner=?', (owner,)).fetchone()
    if prefs and job_id in _json(prefs['ids'], []):
        return True
    keys = snapshot.get('_trash_source_keys')
    if not isinstance(keys, list):
        keys = [row['url_key'] for row in db.execute('SELECT url_key FROM crawl_source_jobs WHERE owner=? AND job_id=?',
                                                    (owner, job_id))]
    if keys:
        return not db.execute("SELECT 1 FROM crawl_sources WHERE owner=? AND classification='general' "
                              'AND url_key IN (SELECT value FROM json_each(?)) LIMIT 1', (owner, json.dumps(keys))).fetchone()
    source = str(snapshot.get('source') or '')
    if source.startswith('crawl:'):
        row = db.execute('SELECT source_classification FROM crawls WHERE owner=? AND id=?', (owner, source[6:])).fetchone()
        return not row or row['source_classification'] != 'general'
    return False


def _manual_ids(db, owner, *, add=(), remove=()):
    prefs = db.execute('SELECT ids FROM privacy WHERE owner=?', (owner,)).fetchone()
    before = set(_json(prefs['ids'], [])) if prefs else set()
    ids = (before - set(remove)) | set(add)
    if ids != before:
        db.execute("INSERT INTO privacy(owner,tags,ids) VALUES(?,'[]',?) ON CONFLICT(owner) DO UPDATE SET ids=excluded.ids",
                   (owner, json.dumps(sorted(ids))))


def trash_job(jid, actor=None):
    """Atomically save the complete row before it leaves the active library."""
    with db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
            if row is None:
                db.rollback()
                return 'gone', 0
            snapshot = dict(row)
            if snapshot['status'] in ('downloading', 'processing'):
                db.rollback()
                return 'stopping', 0
            owner = snapshot.get('owner') or actor or 'user:admin'
            snapshot['owner'] = owner
            snapshot['_trash_index_ids'] = {
                table: [r['id'] for r in db.execute(f"SELECT id FROM {table} WHERE kind='job' AND ref=?", (jid,)).fetchall()]
                for table in ('seg', 'vec')}
            snapshot['_trash_source_keys'] = [r['url_key'] for r in db.execute(
                'SELECT url_key FROM crawl_source_jobs WHERE owner=? AND job_id=?', (owner, jid)).fetchall()]
            context = db.execute('SELECT context FROM crawl_downloads WHERE job_id=?', (jid,)).fetchone()
            if context:
                snapshot['_trash_download_context'] = context['context']
            snapshot['_trash_private'] = snapshot_private(db, owner, jid, snapshot)
            # Older archived entries may share this ID. Capture their own flag
            # before removing a now-dangling explicit ID preference.
            for previous in db.execute('SELECT id,snapshot FROM job_trash WHERE owner=? AND job_id=?', (owner, jid)).fetchall():
                saved = _json(previous['snapshot'], {})
                saved['_trash_private'] = snapshot_private(db, owner, jid, saved)
                db.execute('UPDATE job_trash SET snapshot=? WHERE id=?', (json.dumps(saved), previous['id']))
            db.execute('INSERT INTO job_trash (owner, job_id, snapshot, deleted_at) VALUES (?,?,?,?)',
                       (owner, jid, json.dumps(snapshot, ensure_ascii=False), time.time()))
            db.execute('DELETE FROM jobs WHERE id=?', (jid,))
            _manual_ids(db, owner, remove=(jid,))
            db.execute('UPDATE jobs SET ref=NULL WHERE ref=?', (jid,))
            db.execute('UPDATE shares SET revoked=1 WHERE job_id=?', (jid,))
            db.execute('DELETE FROM tasks WHERE target=? OR target LIKE ?', (f'job:{jid}', f'job:{jid}:%'))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return 'trashed', 0


def _owned(db, trash_id, owner):
    row = db.execute('SELECT * FROM job_trash WHERE id=? AND owner=?', (trash_id, owner)).fetchone()
    if row is None:
        raise LookupError('回收站记录不存在')
    return dict(row)


def restore(trash_id, owner):
    """Restore metadata and playback position; restoration never starts a download."""
    with db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            entry = _owned(db, trash_id, owner)
            snapshot = _json(entry['snapshot'], {})
            private = snapshot_private(db, owner, entry['job_id'], snapshot)
            if not snapshot.get('url'):
                raise ValueError('回收站记录不完整')
            fields = {row[1] for row in db.execute('PRAGMA table_info(jobs)').fetchall()}
            row = {key: value for key, value in snapshot.items() if key in fields}
            row['owner'] = owner
            old_id = entry['job_id']
            existing = db.execute('SELECT id FROM jobs WHERE owner IS ? AND key=? AND status NOT IN (\'failed\',\'cancelled\') ORDER BY id DESC LIMIT 1',
                                  (owner, row.get('key') or core.link_key(row['url']))).fetchone()
            if existing:
                raise ValueError('资料库已有相同视频，请先查看现有条目')
            media = [path for value in _paths(row) if (path := _safe_path(value)) and path.exists()
                     and (path.is_dir() or path.suffix.lower() in VIDEO_EXT | AUDIO_EXT)]
            row.update(status='done' if media else 'failed', ref=None, cancel=0, attempts=0,
                       retry_at=None, stage='', speed='', progress=100 if media else 0,
                       updated=time.time(), key=row.get('key') or core.link_key(row['url']))
            if media:
                row['error'] = ''
            else:
                row['error'] = '原文件已不存在，可点击重试重新获取。'
            row['id'] = old_id
            other_entries = db.execute('SELECT id,snapshot FROM job_trash WHERE job_id=? AND id!=?',
                                       (old_id, trash_id)).fetchall() if old_id else []
            if not old_id or other_entries or db.execute('SELECT 1 FROM jobs WHERE id=?', (old_id,)).fetchone():
                row.pop('id', None)
            columns = list(row)
            cur = db.execute('INSERT INTO jobs (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                             tuple(row[key] for key in columns))
            jid = cur.lastrowid
            _manual_ids(db, owner, add=(jid,) if private else (), remove=() if private else (jid,))
            if old_id and old_id != jid:
                db.execute('UPDATE OR IGNORE watch SET job_id=? WHERE job_id=? AND owner=?', (jid, old_id, owner))
                indexes = snapshot.get('_trash_index_ids') or {}
                # An imported job can reuse an ID while an earlier job with
                # that ID is still in trash. Its snapshot must not claim the
                # earlier entry's retained indexes merely because ref matches.
                prior = {table: set() for table in ('seg', 'vec')}
                for other in other_entries:
                    if other['id'] < trash_id:
                        saved = _json(other['snapshot'], {}).get('_trash_index_ids') or {}
                        for table in prior:
                            prior[table].update(saved.get(table, []))
                db.executemany("UPDATE seg SET ref=? WHERE id=? AND kind='job' AND ref=?",
                               [(jid, index_id, old_id) for index_id in indexes.get('seg', [])
                                if type(index_id) is int and index_id not in prior['seg']])
                for index_id in indexes.get('vec', []):
                    if type(index_id) is not int or index_id in prior['vec']:
                        continue
                    # Picture caches detect MAX(id)/COUNT changes. A fresh row
                    # invalidates them in every process when its job id changes.
                    db.execute("INSERT INTO vec (kind,ref,part,t,src,base,v,model) "
                               "SELECT kind,?,part,t,src,base,v,model FROM vec WHERE id=? AND kind='job' AND ref=?",
                               (jid, index_id, old_id))
                    db.execute("DELETE FROM vec WHERE id=? AND kind='job' AND ref=?", (index_id, old_id))
            if snapshot.get('_trash_download_context'):
                db.execute('INSERT OR IGNORE INTO crawl_downloads (job_id, context) VALUES (?,?)',
                           (jid, snapshot['_trash_download_context']))
            source_keys = snapshot.get('_trash_source_keys')
            if not isinstance(source_keys, list):
                # Snapshots saved before source inheritance retain the legacy
                # ledger; a new snapshot records only its original source keys.
                source_keys = [r['url_key'] for r in db.execute(
                    'SELECT url_key FROM crawl_source_jobs WHERE owner=? AND job_id=?', (owner, old_id)).fetchall()]
            db.executemany('INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)',
                           [(owner, jid, key) for key in source_keys if isinstance(key, str)])
            db.execute('DELETE FROM job_trash WHERE id=? AND owner=?', (trash_id, owner))
            if old_id and old_id != jid:
                _clear_unused_source_ledger(db, old_id, owner)
            db.commit()
        except Exception:
            db.rollback()
            raise
    core.ring()
    return jid


def _clear_unused_source_ledger(db, jid, owner):
    if (not db.execute('SELECT 1 FROM jobs WHERE id=? AND owner=?', (jid, owner)).fetchone()
            and not db.execute('SELECT 1 FROM job_trash WHERE job_id=? AND owner=?', (jid, owner)).fetchone()):
        db.execute('DELETE FROM crawl_source_jobs WHERE owner=? AND job_id=?', (owner, jid))


def purge(trash_id, owner, now=None, expired_only=False):
    """Permanently delete unshared files and then the ledger record, under one lock."""
    from . import pipeline
    with db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            entry = _owned(db, trash_id, owner)
            if expired_only and (entry['orphan'] or entry['deleted_at'] > (time.time() if now is None else now) - RETENTION_SECONDS):
                db.rollback()
                return {'files': 0, 'kept_shared': False, 'skipped': True}
            snapshot = _json(entry['snapshot'], {})
            references = _referenced(db, except_trash=trash_id)
            protection = _FileProtection(references)
            own_paths = {_safe_path(path, root=protection.root) for path in _paths(snapshot)} - {None}
            shared = any(protection.overlaps(path) for path in own_paths)
            removed = pipeline.delete_job_files(snapshot, protection=protection)
            jid = entry['job_id']
            other_entries = db.execute('SELECT id,snapshot FROM job_trash WHERE job_id=? AND id!=?',
                                       (jid, trash_id)).fetchall() if jid else []
            if jid and not other_entries and not db.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone():
                db.execute('DELETE FROM watch WHERE job_id=?', (jid,))
                db.execute('DELETE FROM shares WHERE job_id=?', (jid,))
                db.execute('DELETE FROM fingerprints WHERE job_id=?', (jid,))
                db.execute('DELETE FROM similar WHERE a=? OR b=?', (jid, jid))
                db.execute('DELETE FROM crawl_downloads WHERE job_id=?', (jid,))
                db.execute('DELETE FROM tasks WHERE target=? OR target LIKE ?', (f'job:{jid}', f'job:{jid}:%'))
            if jid:
                # Only saved index IDs belong to this entry. Earlier snapshots
                # take precedence if a reused job ID captured those same rows.
                indexes = snapshot.get('_trash_index_ids') or {}
                prior = {table: set() for table in ('seg', 'vec')}
                for other in other_entries:
                    if other['id'] < trash_id:
                        saved = _json(other['snapshot'], {}).get('_trash_index_ids') or {}
                        for table in prior:
                            prior[table].update(saved.get(table, []))
                for table in ('seg', 'vec'):
                    db.executemany(f"DELETE FROM {table} WHERE id=? AND kind='job' AND ref=?",
                                   [(index_id, jid) for index_id in indexes.get(table, [])
                                    if type(index_id) is int and index_id not in prior[table]])
            db.execute('DELETE FROM job_trash WHERE id=? AND owner=?', (trash_id, owner))
            if jid:
                _clear_unused_source_ledger(db, jid, owner)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'files': removed, 'kept_shared': shared}


def cleanup_expired(now=None, limit=100):
    now = time.time() if now is None else float(now)
    rows = q('SELECT id, owner FROM job_trash WHERE orphan=0 AND deleted_at<=? ORDER BY deleted_at LIMIT ?',
             (now - RETENTION_SECONDS, max(1, min(int(limit), 1000))))
    count = 0
    for row in rows:
        try:
            result = purge(row['id'], row['owner'], now=now, expired_only=True)
            count += not result.get('skipped')
        except LookupError:
            continue
        except Exception:
            traceback.print_exc()
    return count


def cleanup_loop():
    while True:
        try:
            cleanup_expired()
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


def scan_orphans(owner, limit=300):
    """Admin-triggered recovery candidates. Scanning alone never schedules deletion."""
    root = MEDIA.resolve()
    candidates, examined, truncated = [], 0, False
    started = time.monotonic()
    # Only managed library roots, never private notes/books, caches or incomplete downloads.
    for folder in ('Videos', 'Movies', 'TV', 'Music', 'Music Videos'):
        directory = root / folder
        if not directory.exists() or directory.is_symlink():
            continue
        for parent, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not name.startswith('.') and not (Path(parent) / name).is_symlink())
            for name in sorted(files):
                examined += 1
                path = Path(parent) / name
                if path.suffix.lower() not in VIDEO_EXT | AUDIO_EXT or path.is_symlink() or not _safe_path(str(path)):
                    continue
                candidates.append(path)
                if len(candidates) >= 20000 or time.monotonic() - started > 8:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    found = 0
    with db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            referenced = {_safe_path(value) for value in _referenced(db)} - {None}
            for path in candidates:
                resolved = path.resolve()
                if resolved in referenced or any(p in resolved.parents for p in referenced):
                    continue
                sidecars = [str(path)]
                for sibling in path.parent.iterdir():
                    if sibling.is_file() and not sibling.is_symlink() and sibling.name.startswith(path.stem + '.') and sibling.suffix.lower() in SUB_EXT | {'.jpg', '.jpeg', '.png', '.webp', '.nfo'}:
                        sidecars.append(str(sibling))
                stamp = time.time()
                url = 'recovered:' + hashlib.sha256(str(resolved).encode()).hexdigest()
                relative = resolved.relative_to(root)
                analysis = {'title': path.stem, 'library': relative.parts[0]}
                if relative.parts[0] == 'Videos' and len(relative.parts) > 2:
                    analysis['category'] = relative.parts[1]
                snapshot = {'url': url, 'key': core.link_key(url), 'source': 'recovered', 'owner': owner,
                            'status': 'done', 'kind': 'audio' if path.suffix.lower() in AUDIO_EXT else 'video',
                            'title': path.stem, 'files': json.dumps(sidecars, ensure_ascii=False),
                            'analysis': json.dumps(analysis, ensure_ascii=False), 'created': stamp, 'updated': stamp,
                            'thumb': next((p for p in sidecars if Path(p).suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}), '')}
                db.execute('INSERT INTO job_trash (owner, job_id, snapshot, deleted_at, orphan) VALUES (?,NULL,?,?,1)',
                           (owner, json.dumps(snapshot, ensure_ascii=False), stamp))
                referenced.update(_safe_path(value) for value in sidecars)
                found += 1
                if found >= limit:
                    truncated = True
                    break
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'found': found, 'examined': examined, 'truncated': truncated}


def _privacy_prefs():
    # Archived jobs no longer participate in web.hidden_ids(). Check their saved
    # metadata against the current owner's settings, including token expiry.
    from . import web
    prefs = web.privacy_get(g.owner)
    return {**prefs, 'ids': set(prefs['ids']), 'private_view': web.revealed()}


def _hidden_snapshot(job_id, snapshot, prefs):
    from . import web
    flag = snapshot.get('_trash_private')
    private = flag if type(flag) is bool else job_id in prefs['ids'] or job_id in web.source_hidden_ids()
    private = private or web.hides_tags(_json(snapshot.get('analysis'), {}).get('tags'), prefs)
    return bool(private) != prefs['private_view']


@app.before_request
def trash_permission():
    if request.path == '/api/trash' or request.path.startswith('/api/trash/'):
        from . import web
        if request.headers.get('X-Privacy-Token') and not web.privacy_token():
            return jsonify(error='隐私权限已失效，请重新解锁'), 403, {'Cache-Control': 'no-store'}


@app.after_request
def trash_response(response):
    if request.path == '/api/trash' or request.path.startswith('/api/trash/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def _require_visible(trash_id):
    row = q('SELECT job_id, snapshot FROM job_trash WHERE id=? AND owner=?', (trash_id, g.owner), one=True)
    if row is None or _hidden_snapshot(row['job_id'], _json(row['snapshot'], {}), _privacy_prefs()):
        raise LookupError('回收站记录不存在')


@app.get('/api/trash')
def list_trash():
    items, total, before = [], 0, None
    prefs = _privacy_prefs()
    # Filter before limiting the response: hidden entries must neither displace
    # visible ones nor appear in total/has_more. Read bounded batches of snapshots.
    while True:
        condition = ' AND (deleted_at<? OR (deleted_at=? AND id<?))' if before else ''
        args = (g.owner, before[0], before[0], before[1]) if before else (g.owner,)
        rows = q('SELECT * FROM job_trash WHERE owner=?' + condition +
                 ' ORDER BY deleted_at DESC, id DESC LIMIT 1000', args)
        for row in rows:
            data = _json(row['snapshot'], {})
            if _hidden_snapshot(row['job_id'], data, prefs):
                continue
            total += 1
            if len(items) < 1000:
                items.append({'id': row['id'], 'job_id': row['job_id'], 'title': data.get('title') or '未命名视频',
                              'url': '' if row['orphan'] else data.get('url', ''), 'kind': data.get('kind', ''),
                              'deleted_at': row['deleted_at'], 'expires_at': None if row['orphan'] else row['deleted_at'] + RETENTION_SECONDS,
                              'files_count': len(_json(data.get('files'), [])), 'orphan': bool(row['orphan'])})
        if len(rows) < 1000:
            break
        before = (rows[-1]['deleted_at'], rows[-1]['id'])
    response = jsonify(items=items, total=total, has_more=total > len(items),
                       retention_days=RETENTION_DAYS, can_scan=bool(g.admin))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/api/trash/<int:trash_id>/restore')
def restore_trash(trash_id):
    try:
        _require_visible(trash_id)
        return jsonify(ok=True, job_id=restore(trash_id, g.owner))
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 409


@app.post('/api/trash/<int:trash_id>/delete')
def delete_trash(trash_id):
    try:
        _require_visible(trash_id)
        result = purge(trash_id, g.owner)
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except OSError:
        return jsonify(error='部分文件暂时无法删除，记录已保留，请稍后重试。'), 409
    if result['files']:
        from . import library
        library.plex_refresh()
    return jsonify(ok=True, **result)


@app.post('/api/trash/scan')
def scan_trash():
    if not g.admin:
        return jsonify(error='仅管理员可扫描历史遗留文件'), 403
    return jsonify(ok=True, **scan_orphans(g.owner))
