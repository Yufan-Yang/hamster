"""Owned source overview and durable, deduplicated incremental discoveries."""
import contextlib
import json
import time

from flask import g, jsonify, request

from . import core, web
from .core import app, q


@contextlib.contextmanager
def transaction():
    with core.db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _merge(db, keep, other):
    if keep == other:
        return
    a = db.execute('SELECT * FROM source_inbox WHERE id=?', (keep,)).fetchone()
    b = db.execute('SELECT * FROM source_inbox WHERE id=?', (other,)).fetchone()
    if not a or not b:
        return
    rank = {'unread': 0, 'read': 1, 'ignored': 2}
    state = max((a['state'], b['state']), key=rank.get)
    db.execute('UPDATE source_inbox SET state=?,created=?,updated=? WHERE id=?',
               (state, min(a['created'], b['created']), max(a['updated'], b['updated']), keep))
    db.execute('UPDATE source_inbox_aliases SET item_id=? WHERE item_id=?', (keep, other))
    db.execute('INSERT OR IGNORE INTO source_inbox_links SELECT ?,source_kind,source_id,source_key,content_kind,content_id '
               'FROM source_inbox_links WHERE item_id=?', (keep, other))
    db.execute('DELETE FROM source_inbox_links WHERE item_id=?', (other,))
    db.execute('DELETE FROM source_inbox WHERE id=?', (other,))


def _record(db, owner, aliases, source_kind, source_id, source_key, content_kind, content_id, baseline):
    aliases = sorted({key for key in aliases if isinstance(key, str) and key})
    if not owner or not aliases:
        return
    matches = db.execute('SELECT DISTINCT item_id FROM source_inbox_aliases WHERE owner=? '
                         'AND key IN (SELECT value FROM json_each(?)) ORDER BY item_id', (owner, json.dumps(aliases))).fetchall()
    now = time.time()
    if matches:
        ident = matches[0]['item_id']
        for match in matches[1:]:
            _merge(db, ident, match['item_id'])
    else:
        ident = db.execute('INSERT INTO source_inbox(owner,state,created,updated) VALUES(?,?,?,?)',
                           (owner, 'read' if baseline else 'unread', now, now)).lastrowid
    for key in aliases:
        db.execute('INSERT OR IGNORE INTO source_inbox_aliases(owner,key,item_id) VALUES(?,?,?)', (owner, key, ident))
    db.execute('INSERT OR IGNORE INTO source_inbox_links '
               '(item_id,source_kind,source_id,source_key,content_kind,content_id) VALUES(?,?,?,?,?,?)',
               (ident, source_kind, source_id, source_key, content_kind, content_id))


def crawl_baseline(db, source, *, stopping=False):
    """An empty first scan is still a baseline; stopping is only a worker fence."""
    db.execute('INSERT OR IGNORE INTO source_inbox_baselines(source_id,source_key,generation) VALUES(?,?,?)',
               (source['id'], source['url_key'], source['generation']))
    if stopping:
        db.execute('UPDATE source_inbox_baselines SET generation=generation+1 '
                   'WHERE source_id=? AND source_key=? AND generation=?',
                   (source['id'], source['url_key'], source['generation']))


def record_crawl(db, cid, item_id):
    """Called inside record_video's transaction; first scans are historical baselines."""
    from .crawl import MEDIA_EXT, identity
    source = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
    item = db.execute('SELECT * FROM crawl_items WHERE id=?', (item_id,)).fetchone()
    if not source or not item or source['owner'] != item['owner']:
        return
    started = db.execute("SELECT value FROM source_inbox_meta WHERE key='started'").fetchone()['value']
    crawl_baseline(db, source)
    baseline_generation = db.execute('SELECT generation FROM source_inbox_baselines WHERE source_id=? AND source_key=?',
                                     (cid, source['url_key'])).fetchone()['generation']
    aliases = [r['url_key'] for r in db.execute('SELECT url_key FROM crawl_aliases WHERE owner=? AND item_id=?',
                                              (item['owner'], item_id))]
    aliases.append(identity(item['url'], media=bool(MEDIA_EXT.search(item['url']))))
    _record(db, item['owner'], aliases, 'crawl', cid, source['url_key'], 'crawl', item_id,
            source['generation'] <= baseline_generation or item['created'] <= started)


def record_sub(sub_id, job_id, entry, baseline=False):
    from .crawl import identity
    with transaction() as db:
        source = db.execute('SELECT * FROM subs WHERE id=?', (sub_id,)).fetchone()
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not source or not job or source['owner'] != job['owner']:
            return
        started = db.execute("SELECT value FROM source_inbox_meta WHERE key='started'").fetchone()['value']
        aliases = [identity(entry['url']), identity(job['url'])]
        _record(db, source['owner'], aliases, 'sub', sub_id, source['key'], 'job', job_id,
                baseline or (job['created'] or 0) <= started)


def merge_crawl(db, keep, other):
    links = db.execute("SELECT DISTINCT item_id FROM source_inbox_links WHERE content_kind='crawl' AND content_id IN (?,?)",
                       (keep, other)).fetchall()
    if links:
        primary = links[0]['item_id']
        for link in links[1:]:
            _merge(db, primary, link['item_id'])
    db.execute("UPDATE OR IGNORE source_inbox_links SET content_id=? WHERE content_kind='crawl' AND content_id=?", (keep, other))
    db.execute("DELETE FROM source_inbox_links WHERE content_kind='crawl' AND content_id=?", (other,))


def move_owner(old, new):
    if old == new:
        return
    with transaction() as db:
        for alias in db.execute('SELECT * FROM source_inbox_aliases WHERE owner=?', (old,)).fetchall():
            current = db.execute('SELECT item_id FROM source_inbox_aliases WHERE owner=? AND key=?', (old, alias['key'])).fetchone()
            existing = db.execute('SELECT item_id FROM source_inbox_aliases WHERE owner=? AND key=?', (new, alias['key'])).fetchone()
            if current and existing:
                _merge(db, existing['item_id'], current['item_id'])
                db.execute('DELETE FROM source_inbox_aliases WHERE owner=? AND key=?', (old, alias['key']))
        db.execute('UPDATE source_inbox_aliases SET owner=? WHERE owner=?', (new, old))
        db.execute('UPDATE source_inbox SET owner=? WHERE owner=?', (new, old))
        # Login later removes duplicate channel subscriptions; retain their provenance.
        for sub in db.execute('SELECT a.id old_id,b.id new_id FROM subs a JOIN subs b ON b.owner=? '
                              'AND b.key=a.key WHERE a.owner=?', (new, old)).fetchall():
            db.execute("UPDATE OR IGNORE source_inbox_links SET source_id=? WHERE source_kind='sub' AND source_id=?",
                       (sub['new_id'], sub['old_id']))
            db.execute("DELETE FROM source_inbox_links WHERE source_kind='sub' AND source_id=?", (sub['old_id'],))


def _context():
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        raise PermissionError('隐私权限已失效，请重新解锁')
    return g.owner, web.revealed()


def _guard(owner, reveal, db=None):
    if _context() != (owner, reveal):
        raise PermissionError('账号或隐私权限已变化，请刷新后重试')
    snapshot = g.get('source_access_stamp')
    if snapshot is not None and snapshot != (owner, _source_stamp(owner, db), _privacy_stamp(owner, db)):
        raise PermissionError('来源或隐私权限已变化，请刷新后重试')


def _reply(**values):
    response = jsonify(**values)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _privacy_stamp(owner, db=None):
    def read(sql, args):
        return db.execute(sql, args).fetchone() if db is not None else q(sql, args, one=True)
    prefs = read('SELECT tags,ids FROM privacy WHERE owner=?', (owner,))
    aliases = read("SELECT v FROM kv WHERE k='tag_aliases'", ())
    return (tuple(prefs) if prefs else None, aliases['v'] if aliases else None)


def _source_stamp(owner, db=None):
    # Background progress changes do not invalidate a view. Root replacement,
    # reclassification and subscription replacement can change its visibility.
    def read(sql):
        return db.execute(sql, (owner,)).fetchall() if db is not None else q(sql, (owner,))
    crawls = read('SELECT id,url_key,url,source_classification FROM crawls WHERE owner=? ORDER BY id')
    subs = read('SELECT id,key,url,platform FROM subs WHERE owner=? ORDER BY id')
    return (tuple(tuple(row) for row in crawls), tuple(tuple(row) for row in subs))


def _snapshot(owner, reveal):
    from . import channels, crawl
    from .crawl_source import known_adult
    stamp = _privacy_stamp(owner)
    g.source_access_stamp = (owner, _source_stamp(owner), stamp)
    hidden = set() if reveal else crawl._hidden_collected_job_ids(owner)
    hidden_subs = set() if reveal else web.hidden_subs(hidden)
    sources, crawl_rows, sub_rows = {}, {}, {}
    for row in q('SELECT * FROM crawls WHERE owner=?', (owner,)):
        if not crawl.source_visible(row, reveal):
            continue
        key = f"crawl:{row['id']}"
        crawl_rows[row['id']] = row
        sources[key] = {'key': key, 'type': 'crawl', 'id': row['id'], 'title': row['title'] or row['url'],
                        'url': row['url'], 'status': row['status'], 'unread': 0, 'found': 0,
                        'last_checked': row['last_refresh'], 'next_refresh': row['next_refresh']}
    for row in q('SELECT * FROM subs WHERE owner=?', (owner,)):
        if not reveal and (row['id'] in hidden_subs or row['platform'] in channels.ADULT_PLATFORMS or known_adult(row['url'])):
            continue
        key = f"sub:{row['id']}"
        sub_rows[row['id']] = row
        sources[key] = {'key': key, 'type': 'sub', 'id': row['id'], 'title': row['name'] or row['url'],
                        'url': row['url'], 'status': 'failed' if row['error'] else 'queued' if row['checked'] is None else 'done',
                        'unread': 0, 'found': 0, 'last_checked': row['checked'],
                        'next_refresh': (row['checked'] or time.time()) + (channels.SUB_RETRY if row['error'] else channels.SUB_INTERVAL)}
    jobs = {row['id']: row for row in q('SELECT id,title,url,analysis,status,source FROM jobs WHERE owner=?', (owner,)) if row['id'] not in hidden}
    all_crawled = {row['id']: row for row in q('SELECT id,title,url,page_url,job_id FROM crawl_items WHERE owner=?', (owner,))}
    crawled = {iid: row for iid, row in all_crawled.items() if row['job_id'] not in hidden}
    membership = {(r['crawl_id'], r['item_id']) for r in q('SELECT r.crawl_id,r.item_id FROM crawl_results r '
                   'JOIN crawls c ON c.id=r.crawl_id WHERE c.owner=?', (owner,))}
    for cid, iid in membership:
        if cid in crawl_rows and iid in crawled:
            sources[f'crawl:{cid}']['found'] += 1
    for job in jobs.values():
        if str(job['source']).startswith('sub:') and job['source'] in sources:
            sources[job['source']]['found'] += 1
    found = {}
    rows = q('SELECT i.id,i.state,i.created,i.updated,l.source_kind,l.source_id,l.source_key,l.content_kind,l.content_id '
             'FROM source_inbox i JOIN source_inbox_links l ON l.item_id=i.id WHERE i.owner=? '
             'ORDER BY i.created DESC,i.id DESC', (owner,))
    blocked = {row['id'] for row in rows if (row['content_kind'] == 'job' and row['content_id'] in hidden) or
               (row['content_kind'] == 'crawl' and row['content_id'] in all_crawled and all_crawled[row['content_id']]['job_id'] in hidden)}
    for row in rows:
        if row['id'] in blocked:
            continue
        key = f"{row['source_kind']}:{row['source_id']}"
        if key not in sources:
            continue
        job = None
        if row['content_kind'] == 'crawl':
            source = crawl_rows.get(row['source_id'])
            content = crawled.get(row['content_id'])
            if not source or not content or source['url_key'] != row['source_key'] or (source['id'], content['id']) not in membership:
                continue
            job = jobs.get(content['job_id'])
        else:
            source = sub_rows.get(row['source_id'])
            content = job = jobs.get(row['content_id'])
            if not source or not content or source['key'] != row['source_key']:
                continue
        if row['id'] not in found:
            found[row['id']] = {'id': row['id'], 'state': row['state'], 'title': content['title'] or content['url'],
                                'url': content['url'], 'created': row['created'], 'updated': row['updated'],
                                'sources': [], 'job_id': None, 'job_status': None, 'can_download': False,
                                '_links': [], '_privacy_stamp': stamp}
        item = found[row['id']]
        if not any(entry['key'] == key for entry in item['sources']):
            item['sources'].append({field: sources[key][field] for field in ('key', 'type', 'id', 'title', 'url')})
            if item['state'] == 'unread':
                sources[key]['unread'] += 1
        if job:
            item['job_id'], item['job_status'] = job['id'], job['status']
        if row['content_kind'] == 'crawl' and not job:
            item['can_download'] = True
        item['_links'].append({**{field: row[field] for field in ('source_kind', 'source_id', 'source_key', 'content_kind', 'content_id')},
                               '_job_id': job['id'] if job else None, '_job_analysis': job['analysis'] if job else None,
                               '_content_job_id': content['job_id'] if row['content_kind'] == 'crawl' else None})
    for item in found.values():
        if item['job_id']:
            item['can_download'] = False
    if stamp != _privacy_stamp(owner):
        raise PermissionError('隐私设置已变化，请刷新后重试')
    _guard(owner, reveal)
    return list(sources.values()), list(found.values())


def _public(item):
    return {key: value for key, value in item.items() if not key.startswith('_')}


def _page(name, default, high):
    value = request.args.get(name, str(default))
    if not value.isascii() or not value.isdecimal() or not 0 <= int(value) <= high:
        raise ValueError('分页参数无效')
    return int(value)


@app.get('/api/sources')
def api_sources():
    try:
        owner, reveal = _context()
        sources, items = _snapshot(owner, reveal)
        _guard(owner, reveal)
        return _reply(items=sources, total=len(sources), unread=sum(item['state'] == 'unread' for item in items), storage_owner=owner)
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.get('/api/inbox')
def api_inbox():
    try:
        owner, reveal = _context()
        state = request.args.get('state', 'unread')
        if state not in ('unread', 'read', 'ignored', 'all'):
            raise ValueError('收件箱状态无效')
        offset, limit = _page('offset', 0, 1_000_000_000), _page('limit', 50, 100)
        if not limit:
            raise ValueError('分页数量至少为 1')
        _, items = _snapshot(owner, reveal)
        unread = sum(item['state'] == 'unread' for item in items)
        source = request.args.get('source')
        filtered = [item for item in items if (state == 'all' or item['state'] == state)
                    and (not source or any(entry['key'] == source for entry in item['sources']))]
        _guard(owner, reveal)
        return _reply(items=[_public(item) for item in filtered[offset:offset + limit]], total=len(filtered), unread=unread,
                       offset=offset, limit=limit, more=offset + limit < len(filtered), storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


def _selected(body, owner, reveal):
    ids = body.get('ids') if isinstance(body, dict) else None
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError('请选择 1–100 条内容')
    wanted = set(ids)
    _, items = _snapshot(owner, reveal)
    selected = [item for item in items if item['id'] in wanted]
    if len(selected) != len(wanted):
        raise ValueError('部分内容已移除或不可见，请刷新后重试')
    return selected


def _live_link(db, item, owner, reveal):
    if item['_privacy_stamp'] != _privacy_stamp(owner, db):
        return False
    for link in item['_links']:
        if link['_job_id']:
            job = db.execute('SELECT analysis FROM jobs WHERE id=? AND owner=?', (link['_job_id'], owner)).fetchone()
            if not job or job['analysis'] != link['_job_analysis']:
                return False
    for link in item['_links']:
        if link['source_kind'] == 'crawl':
            row = db.execute('SELECT c.source_classification,i.job_id FROM crawls c JOIN crawl_results r ON r.crawl_id=c.id '
                             'JOIN crawl_items i ON i.id=r.item_id WHERE c.id=? AND c.owner=? AND c.url_key=? '
                             'AND i.id=? AND i.owner=?', (link['source_id'], owner, link['source_key'], link['content_id'], owner)).fetchone()
            if row and row['job_id'] == link['_content_job_id'] and (reveal or row['source_classification'] == 'general'):
                return True
        else:
            row = db.execute('SELECT 1 FROM subs s JOIN jobs j ON j.id=? AND j.owner=s.owner '
                             'WHERE s.id=? AND s.owner=? AND s.key=?',
                             (link['content_id'], link['source_id'], owner, link['source_key'])).fetchone()
            if row:
                return True
    return False


@app.post('/api/inbox/action')
def api_inbox_action():
    try:
        owner, reveal = _context()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'ids', 'action'} or body['action'] not in ('read', 'unread', 'ignore'):
            raise ValueError('收件箱操作无效')
        items = _selected(body, owner, reveal)
        with transaction() as db:
            _guard(owner, reveal, db)
            if any(not _live_link(db, item, owner, reveal) for item in items):
                raise ValueError('来源已变化，请刷新后重试')
            ids = [item['id'] for item in items]
            changed = db.execute('UPDATE source_inbox SET state=?,updated=? WHERE owner=? '
                                 'AND id IN (SELECT value FROM json_each(?))',
                                 ('ignored' if body['action'] == 'ignore' else body['action'], time.time(), owner, json.dumps(ids))).rowcount
            _guard(owner, reveal, db)
        return _reply(ok=True, updated=changed, storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.post('/api/inbox/import')
def api_inbox_import():
    from . import crawl
    try:
        owner, reveal = _context()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'ids'}:
            raise ValueError('请求格式无效')
        items = _selected(body, owner, reveal)
        result = {'added': 0, 'duplicates': 0, 'ids': [], 'errors': []}
        with transaction() as db:
            _guard(owner, reveal, db)
            if any(not _live_link(db, item, owner, reveal) for item in items):
                raise ValueError('来源或隐私设置已变化，请刷新后重试')
        for item in items:
            _guard(owner, reveal)
            with transaction() as db:
                if not _live_link(db, item, owner, reveal):
                    result['errors'].append({'id': item['id'], 'error': '来源或隐私设置已变化，请刷新后重试'})
                    continue
            if item['job_id']:
                result['duplicates'] += 1
                result['ids'].append(item['job_id'])
                continue
            link = next((link for link in item['_links'] if link['content_kind'] == 'crawl'), None)
            if not link:
                continue
            try:
                def before_import():
                    with transaction() as db:
                        _guard(owner, reveal, db)
                        if (not _live_link(db, item, owner, reveal) or
                                not _live_link(db, {**item, '_links': [link]}, owner, reveal)):
                            raise ValueError('来源或隐私设置已变化，请刷新后重试')
                response = crawl._import_results(link['source_id'], {'ids': [link['content_id']]}, before_import=before_import)
                if isinstance(response, tuple):
                    raise ValueError('内容已不可见，请刷新后重试')
            except ValueError:
                result['errors'].append({'id': item['id'], 'error': '内容已不可见，请刷新后重试'})
                continue
            data = response.get_json()
            result['added'] += data['added'] + data['linked']
            result['duplicates'] += data['duplicates']
            result['ids'].extend(data['ids'])
        _guard(owner, reveal)
        return _reply(**result, storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403
