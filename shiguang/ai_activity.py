"""Bounded routing receipts; never persist model inputs, outputs or raw failures."""
import contextlib
import contextvars
import json
import math
import re
import time

from flask import g, has_request_context, jsonify, request

from . import core
from .core import app, q

RETENTION_DAYS = 30
MAX_RECORDS = 20000
PURPOSES = {
    'classify': '内容分类', 'summarize': '内容摘要', 'tags': '标签整理', 'chapters': '章节',
    'translate': '字幕翻译', 'search': 'AI 搜索', 'ask': '问拾光', 'notes': '随记整理',
    'digest': '追更周报', 'crawl-source': '目录来源判别', 'learning-plan': '学习清单',
    'live-curator': '直播选题', 'live-boundary': '话题边界', 'live-copy': '直播文案',
    'live-calibrate': '直播校准', 'live-score': '直播评分', 'live-verify': '来源核验',
    'live-style': '主播风格', 'live-refine': '评分校准', 'live-blind-eval': '盲评',
    'failure': '失败说明', 'other': '其他 AI 调用',
}
FALLBACKS = {
    'disabled': 'Codex 路由未启用', 'worker_unavailable': '没有可用的 Codex 工人（离线或暂停）',
    'queue_timeout': 'Codex 排队结束，未及时领取', 'run_timeout': 'Codex 执行超过等待上限',
    'task_missing': 'Codex 任务记录已不存在', 'invalid_result': 'Codex 返回格式无效',
    'quota_exhausted': 'Codex 工人明确报告额度已用完',
    'transient_failure': 'Codex 网络或短期限流重试后仍失败',
    'execution_failed': 'Codex 执行失败，未报告具体分类',
    'routing_error': 'Codex 调度异常', 'no_answer': 'Codex 未返回可用结果',
}
_context = contextvars.ContextVar('ai_activity_context', default=None)


@contextlib.contextmanager
def context(owner, private=True):
    """Explicit provenance for workers outside the task board; never inspect prompts."""
    token = _context.set({'owner': str(owner or ''), 'private': bool(private)})
    try:
        yield
    finally:
        _context.reset(token)


def _provenance(job_id):
    from . import board
    explicit = _context.get()
    if explicit is not None:
        return {**explicit, 'job_id': job_id}
    if has_request_context():
        return {'owner': g.owner or '', 'private': bool(request.headers.get('X-Privacy-Token')), 'job_id': job_id}
    if job_id is not None:
        row = q('SELECT owner FROM jobs WHERE id=?', (job_id,), one=True)
        if row and row['owner']:
            return {'owner': row['owner'], 'private': False, 'job_id': job_id}
    claim = getattr(board._execution, 'claim', None)
    if claim:
        row = q('SELECT payload FROM tasks WHERE id=?', (claim[0],), one=True)
        payload = json.loads(row['payload'] or '{}') if row else {}
        if payload.get('owner'):
            # Aggregates can contain several private objects without a single job ID.
            return {'owner': payload['owner'], 'private': True, 'job_id': None}
        for key, table, column in (('job', 'jobs', 'id'), ('session', 'live_sessions', 'key'),
                                   ('source', 'live_sources', 'id'), ('reference', 'live_references', 'id'),
                                   ('recording', 'live_recordings', 'id'), ('clip', 'live_clips', 'id'),
                                   ('note', 'notes', 'id'), ('book', 'books', 'id')):
            if payload.get(key) is not None:
                row = q(f'SELECT owner FROM {table} WHERE {column}=?', (payload[key],), one=True)
                if row and row['owner']:
                    return {'owner': row['owner'], 'private': key in ('note', 'book'),
                            'job_id': payload[key] if key == 'job' else None}
    # Unattributable work is never assigned to whoever happens to open the page.
    return {'owner': '', 'private': True, 'job_id': None}


def begin(purpose, job_id=None):
    receipt = {'ts': time.time(), '_started': time.monotonic(), 'status': 'failed',
               'purpose': purpose if purpose in PURPOSES else 'other',
               'engine': None, 'model': None, 'fallback_code': None,
               'queue_seconds': None, 'run_seconds': None, 'retries': None}
    try:
        receipt.update(_provenance(job_id))
    except Exception:
        receipt.update(owner='', private=True, job_id=None)
    return receipt


def _seconds(value):
    return round(float(value), 3) if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def finish(receipt):
    """One receipt per route, including terminal failures. Failure here cannot affect AI."""
    if not receipt:
        return
    try:
        model = receipt.get('model')
        model = model if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', model) else None
        retry = receipt.get('retries')
        retry = retry if type(retry) is int and 0 <= retry <= 100 else None
        code = receipt.get('fallback_code')
        with core.db_lock:
            db = core.DB
            if db.in_transaction:
                return  # Never commit or roll back a caller's transaction for telemetry.
            try:
                db.execute('INSERT INTO ai_activity(ts,owner,private,job_id,purpose,engine,model,status,fallback_code,'
                           'queue_seconds,run_seconds,total_seconds,retries) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (receipt['ts'], receipt['owner'], int(receipt['private']), receipt['job_id'], receipt['purpose'],
                            receipt.get('engine') if receipt.get('engine') in ('codex', 'deepseek', 'api') else None,
                            model, 'done' if receipt.get('status') == 'done' else 'failed',
                            code if code in FALLBACKS else None, _seconds(receipt.get('queue_seconds')),
                            _seconds(receipt.get('run_seconds')), _seconds(time.monotonic() - receipt['_started']), retry))
                # Indexed and bounded; no filesystem scan or prompt text involved.
                db.execute('DELETE FROM ai_activity WHERE ts<?', (time.time() - RETENTION_DAYS * 86400,))
                db.execute('DELETE FROM ai_activity WHERE id <= COALESCE('
                           '(SELECT id FROM ai_activity ORDER BY id DESC LIMIT 1 OFFSET ?),0)', (MAX_RECORDS,))
                db.commit()
            except Exception:
                db.rollback()
                raise
    except Exception:
        pass


def move_owner(old, new):
    q('UPDATE ai_activity SET owner=? WHERE owner=?', (new, old))


def _scope():
    from . import web
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        raise PermissionError('隐私权限已失效，请重新解锁')
    owner, reveal = g.owner, web.revealed()
    scope = request.args.get('scope', 'mine')
    if scope not in ('mine', 'system'):
        raise ValueError('范围无效')
    if scope == 'system' and not g.admin:
        raise PermissionError('系统记录仅管理员可见')
    where = 'a.owner=? AND a.ts>=?'
    args = ['' if scope == 'system' else owner, time.time() - _number('days', 7, 1, RETENTION_DAYS) * 86400]
    if not reveal:
        where += ' AND a.private=0'
        hidden = web.hidden_ids()
        if hidden:
            # IDs are server-derived integers; avoid SQLite's bind-parameter limit.
            where += ' AND (a.job_id IS NULL OR a.job_id NOT IN (' + ','.join(str(int(i)) for i in hidden) + '))'
    # A removed/reassigned video cannot become a visible orphaned activity receipt.
    where += ' AND (a.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j WHERE j.id=a.job_id AND j.owner=a.owner))'
    return where, args, owner, reveal, scope


def _number(name, default, low, high):
    value = request.args.get(name, str(default))
    if not value.isascii() or not value.isdecimal() or not low <= int(value) <= high:
        raise ValueError('分页或时间范围无效')
    return int(value)


def _reply(**body):
    response = jsonify(**body)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/ai/activity')
def activity_list():
    from . import web
    try:
        offset, limit = _number('offset', 0, 0, MAX_RECORDS), _number('limit', 30, 1, 100)
        where, args, owner, reveal, scope = _scope()
        purpose = request.args.get('purpose', '')
        if purpose and purpose not in PURPOSES:
            raise ValueError('用途无效')
        if purpose:
            where += ' AND a.purpose=?'
            args.append(purpose)
        groups = q('SELECT purpose,COUNT(*) total,SUM(status=\'failed\') failed,'
                   'SUM(fallback_code IS NOT NULL) fallbacks FROM ai_activity a WHERE ' + where + ' GROUP BY purpose', tuple(args))
        summary = [{'purpose': r['purpose'], 'label': PURPOSES.get(r['purpose'], PURPOSES['other']),
                    'total': r['total'], 'failed': r['failed'], 'fallbacks': r['fallbacks'],
                    'fallback_rate': r['fallbacks'] / r['total']} for r in groups]
        total = sum(r['total'] for r in summary)
        rows = q('SELECT id,ts,purpose,engine,model,status,fallback_code,queue_seconds,run_seconds,total_seconds,retries '
                 'FROM ai_activity a WHERE ' + where + ' ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, offset))
        items = [{**dict(r), 'label': PURPOSES.get(r['purpose'], PURPOSES['other']),
                  'fallback': FALLBACKS.get(r['fallback_code'])} for r in rows]
        # Recheck the unlock after SQL; a revoked token must not release private rows or counts.
        if g.owner != owner or web.revealed() != reveal or (request.headers.get('X-Privacy-Token') and not web.privacy_token()):
            raise PermissionError('账号或隐私权限已变化，请刷新')
        return _reply(items=items, summary=summary, total=total, more=offset + len(items) < total,
                      offset=offset, limit=limit, storage_owner=owner, scope=scope,
                      can_system=bool(g.admin), retention_days=RETENTION_DAYS, max_records=MAX_RECORDS,
                      purposes=[{'value': key, 'label': label} for key, label in PURPOSES.items()])
    except ValueError as exc:
        return _reply(error=str(exc)), 400
    except PermissionError as exc:
        return _reply(error=str(exc)), 403
