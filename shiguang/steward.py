"""Observe owned media, plan bounded repairs, dispatch them and verify their artifacts."""
import hashlib
import glob
import json
import math
from pathlib import Path
import time

from flask import g, jsonify, request

from . import ai_activity, board, core, library, library_search, llm, web
from .core import app, q

BATCH = 25
DISPATCH_LIMIT = 5
DAILY_LIMIT = 40
COOLDOWN = 3600
MAX_ATTEMPTS = 2
AI_DAILY_LIMIT = 12
SCAN_INTERVAL = 60
MANUAL_WINDOW = 86400
POLICY_PREFIX = 'media-steward:policy:'
KINDS = {'steward_index', 'transcribe', 'summarize', 'chapters'}
DEFAULT_POLICY = {'automatic': False, 'ai_planning': False, 'profile': 'standard'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text_digest(value):
    return hashlib.sha256((value if isinstance(value, str) else '').encode()).hexdigest()


def policy(owner):
    saved = core.kv_get(POLICY_PREFIX + owner, {})
    return {**DEFAULT_POLICY, **{k: v for k, v in saved.items() if k in DEFAULT_POLICY}}


def _object(raw, fallback):
    try:
        value = json.loads(raw or 'null')
        return value if isinstance(value, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _stamp(path):
    """No recursive scanning or media decoding. Never follow media outside the managed root."""
    root = core.MEDIA.resolve(strict=True)
    candidate = Path(path)
    resolved = candidate.resolve(strict=True)
    if candidate.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('unmanaged file')
    stat = resolved.stat()
    if not stat.st_size:
        raise ValueError('empty file')
    with resolved.open('rb') as stream:
        stream.read(1)
    return [str(candidate), stat.st_size, stat.st_mtime_ns]


def _action(kind, jid, part, evidence, reason):
    return {'id': f'{kind}:{part if part is not None else "job"}', 'kind': kind,
            'target': f'job:{jid}' + (f':{part}' if part is not None else ''),
            'part': part, 'input_version': digest(evidence), 'reason': reason, 'state': 'pending'}


def inspect(row, settings):
    """Build a plan from actual artifacts. A task's success flag is never proof of an artifact."""
    job = dict(row)
    jid = job['id']
    a = _object(job['analysis'], {})
    observed = {'files': 0, 'subtitles': 0, 'indexed_parts': 0, 'chapters': 0,
                'summary': isinstance(a.get('summary'), str) and bool(a['summary'].strip()), 'issues': [], 'parts': []}
    actions, evidence = [], []
    state = 'ready'
    if job['status'] != 'done':
        observed['issues'].append('下载失败，等待下载任务处理' if job['status'] == 'failed' else '下载或入库尚未完成')
        state = 'blocked' if job['status'] == 'failed' else 'waiting'
    elif job['ref']:
        observed['issues'].append('共享媒体沿用源记录，本轮不改动其处理任务')
        state = 'shared'
    elif not core.MEDIA.is_dir():
        observed['issues'].append('媒体存储暂不可用，暂停补全')
        state = 'blocked'
    else:
        files = _object(job['files'], [])
        media = [f for f in files if isinstance(f, str) and Path(f).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT]
        if not media:
            observed['issues'].append('记录没有已归档的音视频文件')
            state = 'blocked'
        # Missing parts must not shift indices used by board.task_media / remote workers.
        for path in media:
            try:
                evidence.append(_stamp(path))
                observed['files'] += 1
            except (OSError, ValueError, RuntimeError):
                observed['issues'].append('媒体文件缺失、不可读取或不在受管目录，暂停补全')
                state = 'blocked'
        if state != 'blocked':
            music = a.get('library') == 'Music' or a.get('folder') == 'Music Videos'
            semantic = not music and settings['profile'] != 'essential' and (
                settings['profile'] == 'deep' or a.get('needs_transcript') is True)
            chapter_map = a.get('chapters') if isinstance(a.get('chapters'), dict) else {}
            for part, path in enumerate(media):
                p = Path(path)
                subtitles = []
                try:
                    for sidecar in sorted(p.parent.glob(glob.escape(p.stem) + '.*')):
                        if sidecar.suffix.lower() in {'.srt', '.vtt'}:
                            subtitles.append(_stamp(sidecar))
                except (OSError, ValueError, RuntimeError):
                    observed['issues'].append('字幕附件不可读取或不在受管目录，暂停补全')
                    state = 'blocked'
                    break
                count = q("SELECT COUNT(*) n FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (jid, part), one=True)['n']
                stamps = [evidence[part], subtitles]
                observed['subtitles'] += bool(subtitles)
                observed['indexed_parts'] += bool(count)
                chapters = chapter_map.get(str(part))
                chapter_ok = isinstance(chapters, list) and bool(chapters) and all(
                    isinstance(c, dict) and type(c.get('t')) in (int, float) and math.isfinite(c['t'])
                    and c['t'] >= 0 and isinstance(c.get('title'), str) and bool(c['title'].strip()) for c in chapters)
                if chapter_ok:
                    chapter_ok = all(chapters[i]['t'] > chapters[i-1]['t'] for i in range(1, len(chapters)))
                observed['chapters'] += chapter_ok
                observed['parts'].append({'part': part, 'subtitle_lines': count, 'has_subtitles': bool(subtitles)})
                indexed = q("SELECT updated FROM tasks WHERE kind='steward_index' AND target=? AND state='done'",
                            (f'job:{jid}:{part}',), one=True)
                changed_subs = bool(indexed and subtitles and max(s[2] for s in subtitles) / 1e9 > indexed['updated'])
                if (subtitles and (not count or changed_subs)) or (part == 0 and semantic and not job['transcript'] and (subtitles or count)):
                    actions.append(_action('steward_index', jid, part, stamps, '先使用已有字幕，避免重复转写'))
                elif not subtitles and not count and semantic:
                    actions.append(_action('transcribe', jid, part, evidence[part], '该媒体需要内容分析，尚无字幕或转写'))
                elif semantic and count >= 20 and not chapter_ok:
                    actions.append(_action('chapters', jid, part, [stamps, count], '已有足够字幕，尚无章节'))
            if semantic and job['transcript']:
                source = a.get('_summary_source')
                managed = isinstance(source, dict) and source.get('output') == text_digest(a.get('summary'))
                stale = managed and source.get('transcript') != text_digest(job['transcript'])
                if not observed['summary'] or stale:
                    # First import any newer sidecar, then summarize the actual updated text.
                    if not any(x['kind'] == 'steward_index' and x['part'] == 0 for x in actions):
                        actions.insert(0, _action('summarize', jid, None, [evidence, text_digest(job['transcript'])],
                                                 '转写已更新，总结需要刷新' if stale else '已有转写，尚无正文总结'))
                        # Timed summarization also generates first-part chapters.
                        actions = [x for x in actions if not (x['kind'] == 'chapters' and x['part'] == 0)]
            if state == 'blocked':
                actions = []
    version = digest([job['status'], job['ref'], job['files'], job['analysis'],
                      text_digest(job['transcript']), evidence, observed, settings['profile']])
    daily = q('SELECT COUNT(*) n FROM media_steward_actions WHERE owner=? AND created>?',
              (job['owner'], time.time() - 86400), one=True)['n'] if actions else 0
    for action in actions:
        # Existing download follow-ups may still be saving the very artifact we lack.
        part = action['part'] if action['part'] is not None else 0
        upstream = q("SELECT id FROM tasks WHERE state IN ('queued','running') AND "
                     "((target=? AND kind IN ('transcribe','save_subs','index_subs','steward_index')) "
                     "OR (target=? AND kind='summarize' AND ?='chapters')) LIMIT 1",
                     (f'job:{jid}:{part}', f'job:{jid}', action['kind']), one=True)
        task = q('SELECT id,state,updated,not_before FROM tasks WHERE kind=? AND target=?',
                 (action['kind'], action['target']), one=True)
        attempts = q('SELECT COUNT(*) n,MAX(created) last FROM media_steward_actions '
                     'WHERE job_id=? AND kind=? AND target=? AND input_version=?',
                     (jid, action['kind'], action['target'], action['input_version']), one=True)
        if upstream:
            action.update(state='waiting', task_id=upstream['id'], reason='已有处理任务正在补齐所需产物，等待复查')
        elif task and task['state'] in ('queued', 'running'):
            action.update(state='waiting', task_id=task['id'])
        elif task and task['state'] == 'failed':
            action.update(state='blocked', reason='处理任务已失败，请在任务板查看并重试', task_id=task['id'])
        elif attempts['n'] >= MAX_ATTEMPTS:
            action.update(state='blocked', reason='相同输入补全后仍缺少产物，已停止自动重跑')
        elif attempts['last'] and time.time() - attempts['last'] < COOLDOWN:
            action.update(state='backoff', reason='等待产物保存或退避结束后复查')
        elif daily >= DAILY_LIMIT:
            action.update(state='backoff', reason='今日补全额度已用完，等待额度恢复后复查')
    if state == 'ready' and actions:
        state = ('blocked' if any(x['state'] == 'blocked' for x in actions) else
                 'needs_work' if any(x['state'] == 'pending' for x in actions) else 'waiting')
    return {'job_id': jid, 'owner': job['owner'], 'revision': version,
            'state': state, 'observation': observed, 'plan': actions}


def save_observation(item):
    with board.mutation() as conn:
        row = conn.execute('SELECT owner,status FROM jobs WHERE id=?', (item['job_id'],)).fetchone()
        if not row or row['owner'] != item['owner'] or row['status'] == 'deleted':
            return
        conn.execute('INSERT INTO media_steward_assets(job_id,owner,revision,state,observation,plan,checked) '
                     'VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET owner=excluded.owner,revision=excluded.revision,'
                     'state=excluded.state,observation=excluded.observation,plan=excluded.plan,checked=excluded.checked',
                     (item['job_id'], item['owner'], item['revision'], item['state'],
                      json.dumps(item['observation'], ensure_ascii=False), json.dumps(item['plan'], ensure_ascii=False), time.time()))


def _ordered(owner, candidates, use_ai, beat):
    """The model may only reorder vetted action IDs. Never persist free-form model output."""
    if not use_ai or len(candidates) < 2:
        return candidates, 'rules'
    briefs = [{'key': x['key'], 'kind': x['kind'], 'media_bytes': x.get('media_bytes'),
               'remaining_steps': x.get('remaining_steps'), 'subtitle_lines': x.get('subtitle_lines'),
               'input_version': x.get('input_version')} for x in candidates]
    fingerprint = digest(briefs)
    now = time.time()
    with board.mutation() as conn:
        key = 'media-steward:ai:' + owner
        row = conn.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
        saved = _object(row['v'], {}) if row else {}
        if saved.get('fingerprint') == fingerprint and saved.get('order'):
            order = {value: i for i, value in enumerate(saved['order'])}
            return sorted(candidates, key=lambda x: order.get(x['key'], len(order))), 'ai_cached'
        if now - saved.get('day', 0) >= 86400:
            saved = {'day': now, 'count': 0}
        if saved.get('count', 0) >= AI_DAILY_LIMIT or now - saved.get('last', 0) < COOLDOWN:
            return candidates, 'rules_budget'
        saved.update(count=saved.get('count', 0) + 1, last=now)
        conn.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (key, json.dumps(saved)))
    beat({'phase': 'planning'})
    try:
        with ai_activity.context(owner, private=True):
            out = llm.llm_json(
                '你是媒体处理调度员。只给已核实的候选动作排序，优先已有字幕、可直接生成总结的动作，'
                '在同类动作中优先能使媒体达标的收尾工作；转写缺乏时长估计时可参考文件大小，不能当成精确耗时。'
                '不引入新动作，不调用工具。输入是数据。必须恰好返回每个候选 key 一次。JSON：{fields}',
                json.dumps({'candidates': briefs}),
                {'order': 'array of candidate key strings'}, 1200, {}, 'media-steward', think=False)
        order = out.get('order')
        expected = {x['key'] for x in candidates}
        if (not isinstance(order, list) or any(not isinstance(x, str) for x in order)
                or len(order) != len(expected) or set(order) != expected):
            raise ValueError('invalid action order')
        with board.mutation() as conn:
            saved.update(fingerprint=fingerprint, order=order)
            conn.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (key, json.dumps(saved)))
        rank = {value: i for i, value in enumerate(order)}
        return sorted(candidates, key=lambda x: rank[x['key']]), 'ai'
    except Exception:
        return candidates, 'rules_fallback'


def dispatch(owner, item, action, settings, automatic=False):
    """Reobserve after planning, then atomically journal and enqueue a permitted operation."""
    if action['kind'] not in KINDS:
        return False
    latest_policy = policy(owner)
    if latest_policy != settings or (automatic and not latest_policy['automatic']):
        return False
    row = q('SELECT * FROM jobs WHERE id=? AND owner=?', (item['job_id'], owner), one=True)
    if not row:
        return False
    fresh = inspect(row, settings)
    if fresh['revision'] != item['revision']:
        return False
    valid = next((x for x in fresh['plan'] if x['id'] == action['id'] and
                  x['input_version'] == action['input_version'] and x['state'] == 'pending'), None)
    if not valid:
        return False
    now = time.time()
    payload = {'job': row['id'], 'owner': owner, 'title': row['title'] or '媒体补全'}
    if action['part'] is not None:
        payload.update(part=action['part'], language=board.whisper_lang(_object(row['analysis'], {}).get('language')))
        payload['_steward_files'] = row['files']
        media = [f for f in _object(row['files'], []) if isinstance(f, str) and Path(f).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT]
        payload['_steward_stamp'] = _stamp(media[action['part']])
    with board.mutation() as conn:
        saved_policy = conn.execute('SELECT v FROM kv WHERE k=?', (POLICY_PREFIX + owner,)).fetchone()
        current_policy = {**DEFAULT_POLICY, **(_object(saved_policy['v'], {}) if saved_policy else {})}
        if current_policy != settings or (automatic and not current_policy['automatic']):
            return False
        current = conn.execute('SELECT owner,status,files,analysis,transcript,ref FROM jobs WHERE id=?', (row['id'],)).fetchone()
        if not current or any(current[k] != row[k] for k in current.keys()):
            return False
        recent = conn.execute('SELECT COUNT(*) n FROM media_steward_actions WHERE owner=? AND created>?', (owner, now - 86400)).fetchone()['n']
        prior = conn.execute('SELECT COUNT(*) n,MAX(created) last FROM media_steward_actions '
                             'WHERE job_id=? AND kind=? AND target=? AND input_version=?',
                             (row['id'], action['kind'], action['target'], action['input_version'])).fetchone()
        if recent >= DAILY_LIMIT or prior['n'] >= MAX_ATTEMPTS or (prior['last'] and now - prior['last'] < COOLDOWN):
            return False
        task = conn.execute('SELECT id,state FROM tasks WHERE kind=? AND target=?', (action['kind'], action['target'])).fetchone()
        if task and task['state'] in ('queued', 'running', 'failed'):
            return False
        if task:
            tid = task['id']
            conn.execute("UPDATE tasks SET state='queued',priority=20,payload=?,result=NULL,progress=NULL,error='',"
                         "attempts=0,worker=NULL,lease_until=NULL,not_before=NULL,updated=? WHERE id=?",
                         (json.dumps(payload), now, tid))
        else:
            tid = conn.execute('INSERT INTO tasks(kind,target,priority,payload,published_by,created,updated) '
                               "VALUES(?,?,20,?,'media-steward',?,?)",
                               (action['kind'], action['target'], json.dumps(payload), now, now)).lastrowid
        conn.execute('INSERT INTO media_steward_actions(owner,job_id,kind,target,input_version,task_id,created) VALUES(?,?,?,?,?,?,?)',
                     (owner, row['id'], action['kind'], action['target'], action['input_version'], tid, now))
    board.ring('tasks')
    return True


def scan(owner, ids=None, apply=False, automatic=False, beat=lambda *_: None):
    settings = policy(owner)
    cond, args = 'j.owner=? AND j.status NOT IN (\'deleted\',\'cancelled\')', [owner]
    if ids is not None:
        cond += ' AND j.id IN (SELECT value FROM json_each(?))'
        args.append(json.dumps(ids[:BATCH]))
    rows = q('SELECT j.* FROM jobs j LEFT JOIN media_steward_assets s ON s.job_id=j.id WHERE ' + cond +
             ' ORDER BY CASE WHEN EXISTS(SELECT 1 FROM media_steward_actions a JOIN tasks t ON t.id=a.task_id '
             "WHERE a.job_id=j.id AND t.state IN ('done','failed') AND t.updated>COALESCE(s.checked,0)) THEN 0 ELSE 1 END,"
             'COALESCE(s.checked,0),j.id LIMIT ?', (*args, BATCH))
    items, candidates = [], []
    for row in rows:
        beat({'phase': 'observing', 'checked': len(items)})
        item = inspect(row, settings)
        save_observation(item)
        items.append(item)
        for action in item['plan']:
            if action['state'] == 'pending':
                media_files = [f for f in _object(row['files'], []) if isinstance(f, str) and Path(f).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT]
                try:
                    media_bytes = sum(Path(f).stat().st_size for f in media_files)
                except OSError:
                    media_bytes = None
                candidates.append({**action, 'key': f"{item['job_id']}:{action['id']}", 'item': item,
                                   'media_bytes': media_bytes, 'remaining_steps': len(item['plan']),
                                   'subtitle_lines': sum(p['subtitle_lines'] for p in item['observation']['parts'])})
    # Cheap, deterministic dependency preference also serves as the AI fallback.
    candidates.sort(key=lambda x: ({'steward_index': 0, 'summarize': 1, 'chapters': 2, 'transcribe': 3}[x['kind']], x['item']['job_id']))
    candidates, planner = _ordered(owner, candidates, apply and settings['ai_planning'], beat)
    sent = 0
    if apply:
        for action in candidates:
            if sent >= DISPATCH_LIMIT:
                break
            if dispatch(owner, action['item'], action, settings, automatic):
                sent += 1
    for item in items:
        row = q('SELECT * FROM jobs WHERE id=? AND owner=?', (item['job_id'], owner), one=True)
        if row:
            save_observation(inspect(row, settings))
    return {'checked': len(items), 'dispatched': sent, 'planner': planner}


@board.task('steward_scan', '媒体管家盘点', 'steward')
def run_scan(task, beat):
    p = task['payload']
    if p.get('automatic') and not policy(p['owner'])['automatic']:
        return {'skipped': 'automatic processing paused'}
    return scan(p['owner'], p.get('ids'), p.get('apply', False), p.get('automatic', False), beat)


@board.task('steward_index', '媒体管家整理字幕', 'light')
def import_subtitles(task, beat):
    p = task['payload']
    row = q('SELECT * FROM jobs WHERE id=? AND owner=? AND status=\'done\' AND ref IS NULL', (p['job'], p['owner']), one=True)
    if not row:
        return {'skipped': 'media unavailable'}
    item = inspect(row, policy(p['owner']))
    if item['state'] == 'blocked':
        return {'skipped': 'media unavailable'}
    path, subs = board.task_media(task)
    _stamp(path)
    stamp = _stamp(subs[0]) if subs else None
    cues = (library._cues(subs[0], Path(subs[0]).stat().st_mtime) if subs else
            [(r['t'], r['text']) for r in q("SELECT t,text FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕' ORDER BY t", (p['job'], p['part']))])
    if not cues:
        raise ValueError('字幕没有可读取的时间轴，保留已有转写')
    text = '\n'.join(line for _, line in cues)[:200_000]
    beat({'phase': 'saving', 'lines': len(cues)})
    if subs and _stamp(subs[0]) != stamp:
        raise ValueError('字幕文件已变化，稍后重试')
    with board.mutation() as conn:
        current = conn.execute('SELECT owner,status,files,transcript FROM jobs WHERE id=?', (p['job'],)).fetchone()
        if not current or any(current[k] != row[k] for k in current.keys()):
            return {'skipped': 'media changed'}
        conn.execute("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (p['job'], p['part']))
        conn.executemany("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,?,?,'字幕',?)",
                         [(p['job'], p['part'], t, line) for t, line in cues])
        if p['part'] == 0:
            conn.execute('UPDATE jobs SET transcript=?,updated=? WHERE id=?', (text, time.time(), p['job']))
    return {'lines': len(cues)}


def pending_manual_requests(owner=None):
    """A manual batch is durable intent, independent of the opt-in whole-library scheduler."""
    where, args = '', ()
    if owner is not None:
        where, args = " AND json_extract(payload,'$.owner')=?", (owner,)
    rows = q("SELECT payload,created FROM tasks WHERE kind='steward_scan' AND state='done' "
             "AND json_extract(payload,'$.apply')=1 AND json_extract(payload,'$.automatic')=0" + where, args)
    pending = []
    for row in rows:
        p = _object(row['payload'], {})
        ids = p.get('ids')
        requested = p.get('requested_at', row['created'])
        if not isinstance(ids, list) or not ids or time.time() - requested >= MANUAL_WINDOW:
            continue
        jobs = q("SELECT j.id FROM jobs j JOIN media_steward_assets s ON s.job_id=j.id AND s.owner=j.owner "
                 "WHERE j.owner=? AND j.id IN (SELECT value FROM json_each(?)) "
                 "AND j.status NOT IN ('deleted','cancelled') AND s.state IN ('needs_work','waiting')",
                 (p['owner'], json.dumps(ids)))
        if jobs:
            pending.append({'owner': p['owner'], 'ids': [job['id'] for job in jobs], 'requested_at': requested})
    return pending


def tick():
    for row in q('SELECT k FROM kv WHERE k LIKE ?', (POLICY_PREFIX + '%',)):
        owner = row['k'][len(POLICY_PREFIX):]
        if policy(owner)['automatic']:
            queue_scan(owner, automatic=True, apply=True)
    for pending in pending_manual_requests():
        queue_scan(pending['owner'], pending['ids'], apply=True, requested_at=pending['requested_at'])


def loop():
    while True:
        try:
            tick()
        except Exception:
            app.logger.exception('media steward scheduler failed')
        time.sleep(SCAN_INTERVAL)


def queue_scan(owner, ids=None, apply=False, automatic=False, requested_at=None):
    payload = {'owner': owner, 'ids': ids, 'apply': apply, 'automatic': automatic, 'title': '媒体管家盘点',
               'requested_at': time.time() if requested_at is None else requested_at}
    mode = 'automatic' if automatic else 'apply' if apply else 'inspect'
    target = 'steward:' + owner + ':' + mode
    if automatic:
        previous = q("SELECT id,updated FROM tasks WHERE kind='steward_scan' AND target=? AND state='failed'", (target,), one=True)
        if previous and time.time() - previous['updated'] < COOLDOWN:
            return previous['id']
    return board.publish('steward_scan', target, 15, force=True, payload=payload)


def _visible():
    # Personal settings/observations stay personal, including for administrators.
    hidden = sorted(web.hidden_ids())
    return "j.owner=? AND j.status NOT IN ('deleted','cancelled') AND j.id NOT IN (SELECT value FROM json_each(?))", (g.owner, json.dumps(hidden))


def _reply(**body):
    response = jsonify(storage_owner=g.owner, **body)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _visibility_unchanged(args, ids):
    # Classification/tags and ownership can change without changing the privacy preferences row.
    for key in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(key, None)
    cond, fresh_args = _visible()
    if fresh_args != args:
        return False
    current = q('SELECT j.id FROM jobs j WHERE ' + cond +
                ' AND j.id IN (SELECT value FROM json_each(?))', (*fresh_args, json.dumps(ids)))
    return {row['id'] for row in current} == set(ids)


@app.before_request
def steward_guard():
    if request.path.startswith('/api/steward'):
        request.max_content_length = 4096
        if request.headers.get('X-Privacy-Token') and not web.privacy_token():
            return _reply(error='隐私权限已失效，请重新解锁'), 403


@app.get('/api/steward', endpoint='steward_overview')
def overview():
    stamp, reveal = library_search.access_stamp(g.owner), web.revealed()
    cond, args = _visible()
    try:
        offset = int(request.args.get('offset', '0'))
        if not 0 <= offset <= 1000000:
            raise ValueError()
    except ValueError:
        return _reply(error='分页参数无效'), 400
    rows = q('SELECT j.id,j.title,s.state,s.observation,s.plan,s.checked FROM jobs j '
             'LEFT JOIN media_steward_assets s ON s.job_id=j.id AND s.owner=j.owner WHERE ' + cond +
             " ORDER BY CASE s.state WHEN 'blocked' THEN 0 WHEN 'needs_work' THEN 1 WHEN 'waiting' THEN 2 ELSE 3 END,j.id DESC LIMIT ? OFFSET ?",
             (*args, BATCH + 1, offset))
    counts = q('SELECT COALESCE(s.state,\'unchecked\') state,COUNT(*) n FROM jobs j LEFT JOIN media_steward_assets s '
               'ON s.job_id=j.id AND s.owner=j.owner WHERE ' + cond + ' GROUP BY COALESCE(s.state,\'unchecked\')', args)
    items = []
    for row in rows[:BATCH]:
        actions = _object(row['plan'], [])
        items.append({'job_id': row['id'], 'title': row['title'], 'state': row['state'] or 'unchecked',
                      'observation': _object(row['observation'], {}), 'checked': row['checked'],
                      'actions': [{k: action[k] for k in ('id', 'kind', 'state', 'reason')} for action in actions]})
    if (library_search.access_stamp(g.owner) != stamp or web.revealed() != reveal
            or (request.headers.get('X-Privacy-Token') and not web.privacy_token())
            or not _visibility_unchanged(args, [item['job_id'] for item in items])):
        return _reply(error='片库或隐私权限已变化，请刷新'), 409
    busy = bool(q("SELECT 1 FROM tasks WHERE kind='steward_scan' AND state IN ('queued','running') "
                  "AND json_extract(payload,'$.owner')=?", (g.owner,), one=True))
    if not busy:
        for pending in pending_manual_requests(g.owner):
            if q('SELECT 1 FROM jobs j WHERE ' + cond + ' AND j.id IN (SELECT value FROM json_each(?)) LIMIT 1',
                 (*args, json.dumps(pending['ids'])), one=True):
                busy = True
                break
    return _reply(items=items, counts={row['state']: row['n'] for row in counts}, policy=policy(g.owner),
                  offset=offset, more=len(rows) > BATCH, busy=busy)


@app.post('/api/steward/policy', endpoint='steward_policy')
def set_policy():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != set(DEFAULT_POLICY):
        return _reply(error='处理规则字段无效'), 400
    if (any(type(body[k]) is not bool for k in ('automatic', 'ai_planning'))
            or body['profile'] not in ('essential', 'standard', 'deep')):
        return _reply(error='处理规则无效'), 400
    core.kv_set(POLICY_PREFIX + g.owner, body)
    return _reply(policy=body)


@app.post('/api/steward/scan', endpoint='steward_request_scan')
def request_scan():
    stamp, reveal = library_search.access_stamp(g.owner), web.revealed()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {'apply'} or type(body.get('apply', False)) is not bool:
        return _reply(error='盘点参数无效'), 400
    cond, args = _visible()
    rows = q('SELECT j.id FROM jobs j LEFT JOIN media_steward_assets s ON s.job_id=j.id AND s.owner=j.owner WHERE ' + cond +
             ' ORDER BY COALESCE(s.checked,0),j.id LIMIT ?', (*args, BATCH))
    if (stamp != library_search.access_stamp(g.owner) or reveal != web.revealed()
            or (request.headers.get('X-Privacy-Token') and not web.privacy_token())
            or not _visibility_unchanged(args, [row['id'] for row in rows])):
        return _reply(error='片库或隐私权限已变化，请刷新'), 409
    tid = queue_scan(g.owner, [row['id'] for row in rows], apply=body.get('apply', False))
    # Never expose counts from a coalesced whole-library background scan.
    return _reply(task_id=tid, queued=True), 202


def move_owner(old, new):
    with board.mutation() as conn:
        conn.execute('UPDATE media_steward_assets SET owner=? WHERE owner=?', (new, old))
        conn.execute('UPDATE media_steward_actions SET owner=? WHERE owner=?', (new, old))
        conn.execute('INSERT OR IGNORE INTO kv(k,v) SELECT ?,v FROM kv WHERE k=?', (POLICY_PREFIX + new, POLICY_PREFIX + old))
        conn.execute('DELETE FROM kv WHERE k=?', (POLICY_PREFIX + old,))
