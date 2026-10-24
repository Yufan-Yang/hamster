"""Saved query shortcuts, scoped to the account and current privacy unlock."""
import contextlib
import re
import time

from flask import g, jsonify, request

from . import core, web
from .core import app, q

MODES = {'exact', 'wildcard', 'regex', 'ai'}
OWNER_LIMIT = 100


@contextlib.contextmanager
def _transaction():
    with core.db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _body(keys, optional=()):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not set(keys) <= set(body) or not set(body) <= set(keys) | set(optional):
        raise ValueError('请求格式无效')
    return body


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60 or re.search(r'[\x00-\x1f\x7f]', value):
        raise ValueError('名称请填写 1–60 个字符')
    return value.strip()


def _query(value, mode):
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('请选择有效的搜索方式')
    limit = 128 if mode == 'regex' else 256
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or '\x00' in value:
        raise ValueError(f'搜索内容请填写 1–{limit} 个字符')
    value = value.strip()
    if mode == 'regex':
        try:
            re.compile(value, re.I)
        except (re.error, OverflowError, RecursionError):
            raise ValueError('正则表达式无效，请修改后保存') from None
    return value


def _page_number(name, default, low, high):
    value = request.args.get(name)
    if value is None:
        return default
    if not value.isascii() or not value.isdecimal() or not low <= int(value) <= high:
        raise ValueError('分页参数无效')
    return int(value)


def _context():
    # A stale unlock header expresses a private-page request. Never silently
    # reinterpret it as an ordinary save just because its token has expired.
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        raise PermissionError('隐私权限已失效，请重新解锁后重试')
    return g.owner, web.revealed()


def _guard(owner, revealed):
    if _context() != (owner, revealed):
        raise PermissionError('账号或隐私权限已变化，请刷新后重试')


def _item(row):
    return {'id': row['id'], 'name': row['name'], 'q': row['query'], 'mode': row['mode'],
            'private': bool(row['private']), 'created': row['created'], 'updated': row['updated']}


def _reply(**values):
    response = jsonify(**values)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _owned(db, ident, owner, revealed):
    row = db.execute('SELECT * FROM saved_searches WHERE id=? AND owner=? AND (private=0 OR ?)',
                     (ident, owner, revealed)).fetchone()
    if row is None:
        raise LookupError('找不到这个常用搜索')
    return row


def move_owner(old, new):
    """Merge browser shortcuts on login, retaining the account's duplicate name."""
    if old == new:
        return
    with _transaction() as db:
        db.execute('DELETE FROM saved_searches WHERE owner=? AND EXISTS '
                   '(SELECT 1 FROM saved_searches target WHERE target.owner=? '
                   'AND target.query=saved_searches.query AND target.mode=saved_searches.mode '
                   'AND target.private=saved_searches.private)', (old, new))
        db.execute('UPDATE saved_searches SET owner=? WHERE owner=?', (new, old))


@app.get('/api/searches')
def api_saved_searches():
    try:
        offset = _page_number('offset', 0, 0, 1_000_000_000)
        limit = _page_number('limit', 50, 1, 100)
        owner, revealed = _context()
        total = q('SELECT COUNT(*) n FROM saved_searches WHERE owner=? AND (private=0 OR ?)',
                  (owner, revealed), one=True)['n']
        rows = q('SELECT * FROM saved_searches WHERE owner=? AND (private=0 OR ?) '
                 'ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?', (owner, revealed, limit, offset))
        _guard(owner, revealed)
        return _reply(items=[_item(row) for row in rows], total=total, offset=offset, limit=limit,
                       more=offset + len(rows) < total, storage_owner=owner, limit_per_owner=OWNER_LIMIT)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.post('/api/searches')
def api_saved_search_create():
    try:
        owner, revealed = _context()
        body = _body({'name', 'q', 'mode'}, {'expected_private'})
        if 'expected_private' in body and type(body['expected_private']) is not bool:
            raise ValueError('隐私保存标记无效')
        if body.get('expected_private') and not revealed:
            raise PermissionError('隐藏内容已关闭，请重新显示后保存此私密搜索')
        name, query = _name(body['name']), _query(body['q'], body['mode'])
        with _transaction() as db:
            _guard(owner, revealed)
            row = db.execute('SELECT * FROM saved_searches WHERE owner=? AND query=? AND mode=? AND private=?',
                             (owner, query, body['mode'], revealed)).fetchone()
            created = row is None
            if created:
                if db.execute('SELECT COUNT(*) n FROM saved_searches WHERE owner=?', (owner,)).fetchone()['n'] >= OWNER_LIMIT:
                    return _reply(error='常用搜索已达容量限制，请删除不再需要的搜索后重试'), 409
                now = time.time()
                ident = db.execute('INSERT INTO saved_searches(owner,name,query,mode,private,created,updated) '
                                   'VALUES(?,?,?,?,?,?,?)', (owner, name, query, body['mode'], revealed, now, now)).lastrowid
                row = db.execute('SELECT * FROM saved_searches WHERE id=?', (ident,)).fetchone()
            _guard(owner, revealed)
        _guard(owner, revealed)
        return _reply(item=_item(row), created=created, storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.get('/api/searches/<int:ident>')
def api_saved_search_get(ident):
    try:
        owner, revealed = _context()
        row = q('SELECT * FROM saved_searches WHERE id=? AND owner=? AND (private=0 OR ?)',
                (ident, owner, revealed), one=True)
        if row is None:
            raise LookupError('找不到这个常用搜索')
        _guard(owner, revealed)
        return _reply(item=_item(row), storage_owner=owner)
    except LookupError as exc:
        return _reply(error=str(exc)), 404
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.post('/api/searches/<int:ident>/rename')
def api_saved_search_rename(ident):
    try:
        owner, revealed = _context()
        name = _name(_body({'name'})['name'])
        with _transaction() as db:
            _guard(owner, revealed)
            _owned(db, ident, owner, revealed)
            db.execute('UPDATE saved_searches SET name=?,updated=? WHERE id=? AND owner=?',
                       (name, time.time(), ident, owner))
            row = db.execute('SELECT * FROM saved_searches WHERE id=? AND owner=?', (ident, owner)).fetchone()
            _guard(owner, revealed)
        _guard(owner, revealed)
        return _reply(item=_item(row), storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except LookupError as exc:
        return _reply(error=str(exc)), 404
    except PermissionError as exc:
        return _reply(error=str(exc)), 403


@app.post('/api/searches/<int:ident>/delete')
def api_saved_search_delete(ident):
    try:
        owner, revealed = _context()
        _body(set())
        with _transaction() as db:
            _guard(owner, revealed)
            _owned(db, ident, owner, revealed)
            db.execute('DELETE FROM saved_searches WHERE id=? AND owner=?', (ident, owner))
            _guard(owner, revealed)
        _guard(owner, revealed)
        return _reply(ok=True, id=ident, storage_owner=owner)
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except LookupError as exc:
        return _reply(error=str(exc)), 404
    except PermissionError as exc:
        return _reply(error=str(exc)), 403
