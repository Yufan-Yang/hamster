"""Concrete file/download diagnoses, explicit repairs and recoverable file moves."""
import json
import os
from pathlib import Path
import re
import time

from flask import g, request

from . import board, core, library, pipeline, steward
from .core import app, q

INTERVAL = 6 * 3600
ATTACHMENTS = core.SUB_EXT | {'.jpg', '.jpeg', '.png', '.webp', '.nfo'}


def _safe(path):
    path = Path(path)
    root = core.MEDIA.resolve(strict=True)
    if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError('路径不在受管媒体目录内')
    # Parent symlinks can change independently of a recorded file stamp.
    for parent in [path, *path.parents]:
        if parent in (core.MEDIA,root):
            break
        if parent.is_symlink():
            raise ValueError('归档路径包含符号链接')
    return path


def _snapshot(row):
    return {key: row[key] for key in ('id', 'owner', 'status', 'files', 'thumb', 'analysis', 'ref', 'updated', 'cancel', 'retry_at')}


def _layout_metadata(raw):
    analysis=steward._object(raw,{})
    return {k:analysis.get(k) for k in ('library','folder','title','year','show','season','episode','artist','album')}


def _referenced_elsewhere(paths, jid, db=None):
    wanted = set(paths)
    bases = {(Path(p).parent, Path(p).stem) for p in paths if Path(p).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT}
    def protected(value):
        return isinstance(value, str) and bool(value) and (value in wanted or any(Path(value) in Path(p).parents for p in wanted)
            or Path(value).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT and (Path(value).parent,Path(value).stem) in bases)
    def fetch(sql, args=()):
        return db.execute(sql,args).fetchall() if db is not None else q(sql,args)
    for r in fetch('SELECT files,thumb FROM jobs WHERE id!=?', (jid,)):
        for value in steward._object(r['files'], []) + [r['thumb']]:
            if protected(value):
                return True
    for r in fetch('SELECT snapshot FROM job_trash'):
        data = steward._object(r['snapshot'], {})
        for value in steward._object(data.get('files'), []) + [data.get('thumb')]:
            if protected(value):
                return True
    return False


def layout(row):
    """A complete source/destination preview. Never overwrite or guess a multi-part name."""
    files = steward._object(row['files'], [])
    media = [Path(p) for p in files if isinstance(p, str) and Path(p).suffix.lower() in core.VIDEO_EXT | core.AUDIO_EXT]
    if len(media) != 1 or row['ref']:
        return None
    main = _safe(media[0])
    analysis = steward._object(row['analysis'], {})
    directory, base = library.destination(analysis, main.suffix.lower(), main.stem)
    target = _safe(directory / (base + main.suffix))
    # Numeric collision suffixes were deliberately assigned by the original archiver.
    if main.parent == target.parent and (main == target or re.fullmatch(re.escape(base) + r' \(\d+\)', main.stem)):
        return None
    sources = [main] + sorted(p for p in main.parent.glob(steward.glob.escape(main.stem) + '.*')
                              if p != main and p.suffix.lower() in ATTACHMENTS)
    moves = []
    for source in sources:
        source = _safe(source)
        stamp = steward._stamp(source)
        dest = _safe(directory / (base + source.name[len(main.stem):]))
        if dest.exists() or dest.is_symlink():
            raise ValueError('目标已有同名文件，保留两份文件并等待人工处理')
        moves.append({'source': str(source), 'destination': str(dest), 'stamp': stamp})
    if _referenced_elsewhere([m['source'] for m in moves], row['id']):
        raise ValueError('文件仍被其他记录或回收站引用，暂停移动')
    return {'moves': moves, 'snapshot': _snapshot(row)}


def diagnose(row):
    row = dict(row)
    issues, actions, evidence = [], [], []
    if row['status'] == 'failed' and not row['cancel']:
        if row['retry_at']:
            issues.append('下载正在等待既定自动重试，无需重复安排')
        else:
            issues.append('下载已失败且没有后续重试')
            actions.append({'id': 'retry_download', 'label': '恢复下载', 'detail': '保留部分下载，从原链接重新排队'})
            source = q("SELECT * FROM jobs WHERE owner=? AND id!=? AND status='done' AND ref IS NULL AND url=? AND key=? LIMIT 1",
                       (row['owner'], row['id'], row['url'], row['key']), one=True)
            if source:
                try:
                    stamps = [steward._stamp(p) for p in steward._object(source['files'], [])]
                    if stamps:
                        evidence.append([source['id'], source['files'], stamps])
                        actions = [{'id': 'reuse_download', 'label': '复用已有下载', 'detail': '相同链接已有完整文件，直接关联，避免再次下载'}]
                except (OSError, ValueError, RuntimeError):
                    pass
    elif row['status'] in ('downloading', 'processing', 'linked', 'queued'):
        if time.time() - (row['updated'] or 0) > 3 * 3600:
            issues.append('下载或入库超过三小时没有进展，请检查任务与存储；不会强制重启正在运行的下载')
    elif row['status'] == 'done' and not row['ref']:
        files = steward._object(row['files'], [])
        if not files:
            issues.append('媒体已标记完成，但没有登记文件')
        for path in files:
            if not isinstance(path,str):
                issues.append('记录含无效文件路径，暂停整理')
                continue
            try:
                if Path(path).is_dir():
                    _safe(path)
                    continue
                evidence.append(steward._stamp(path))
            except (OSError, ValueError, RuntimeError):
                issues.append('已登记文件缺失或不可读取：' + Path(path).name)
        if not issues:
            try:
                proposal = layout(row)
                if proposal:
                    evidence.append(proposal['moves'])
                    issues.append('文件位置或名称与当前分类、标题不一致')
                    actions.append({'id': 'organize_files', 'label': '按当前分类归档', 'detail': '同时移动字幕和封面，更新媒体记录；完成后可撤销',
                                    'moves': [{'from': str(Path(m['source']).relative_to(core.MEDIA)),
                                               'to': str(Path(m['destination']).relative_to(core.MEDIA))} for m in proposal['moves']]})
            except (OSError, ValueError, RuntimeError) as error:
                issues.append(str(error) if isinstance(error, ValueError) else '文件归档预览暂不可用')
    identity = _snapshot(row)
    if row['status'] == 'done':
        identity.pop('updated')
        identity['layout']=_layout_metadata(identity.pop('analysis'))
    fingerprint = steward.digest([identity, row['url'], row['key'], row['error'], evidence])
    return {'job_id': row['id'], 'owner': row['owner'], 'revision': fingerprint,
            'issues': list(dict.fromkeys(issues)), 'actions': actions, 'checked': time.time()}


def observe(row):
    report = diagnose(row)
    with board.mutation() as db:
        current = db.execute('SELECT owner,updated,status FROM jobs WHERE id=?', (row['id'],)).fetchone()
        if not current or any(current[k] != row[k] for k in current.keys()):
            return
        db.execute('INSERT INTO media_care(job_id,owner,revision,report,checked) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(job_id) DO UPDATE SET owner=excluded.owner,revision=excluded.revision,report=excluded.report,checked=excluded.checked',
                   (row['id'], row['owner'], report['revision'], json.dumps(report, ensure_ascii=False), report['checked']))


@board.task('steward_diagnose', '媒体管家文件与下载检查', 'steward')
def scan(task, beat):
    owner = task['payload']['owner']
    rows = q("SELECT j.* FROM jobs j LEFT JOIN media_care c ON c.job_id=j.id WHERE j.owner=? AND j.status NOT IN ('deleted','cancelled') "
             'ORDER BY COALESCE(c.checked,0),j.id LIMIT ?', (owner, steward.BATCH))
    dispatched = 0
    for row in rows:
        beat({'checked': row['id']})
        observe(row)
        if steward.policy(owner)['automatic'] and dispatched < steward.DISPATCH_LIMIT:
            dispatched += bool(auto_apply(row))
    return {'checked': len(rows), 'dispatched': dispatched}


def review_action(row, report, action):
    if row['status'] == 'cancelled' or row['cancel'] or row['ref']:
        return False, '已取消或共享的媒体不自动改动'
    if action not in {a['id'] for a in report['actions']}:
        return False, '当前方案没有该动作'
    if action == 'retry_download':
        error = (row['error'] or '').lower()
        if not any(word in error for word in ('network', 'timeout', 'timed out', 'connection', '503', '502', '429', '网络', '超时', '进程意外退出')):
            return False, '非暂时性失败，保留给人工检查'
    if q("SELECT 1 FROM tasks WHERE json_extract(payload,'$.job')=? AND state IN ('queued','running') "
         "AND kind NOT IN ('steward_care_apply','steward_care_undo') LIMIT 1", (row['id'],), one=True):
        return False, '正在处理该媒体，等待任务结束'
    return True, '已核对媒体类型、文件版本、引用关系及任务状态'


def auto_apply(row):
    report = diagnose(row)
    saved = q('SELECT dismissed FROM media_care WHERE job_id=?', (row['id'],), one=True)
    if saved and saved['dismissed'] == report['revision']:
        return
    for action in report['actions'][:1]:
        approved, reason = review_action(row, report, action['id'])
        if not approved:
            return
        now = time.time()
        with board.mutation() as db:
            # Two attempts per job/day and a combined daily cap, including repairs that fail.
            attempts = db.execute("SELECT count(*) FROM tasks WHERE kind='steward_care_apply' AND json_extract(payload,'$.job')=? AND created>?", (row['id'],now-86400)).fetchone()[0]
            daily = steward.daily_count(row['owner'],db)
            policy_row = db.execute('SELECT v FROM kv WHERE k=?', (steward.POLICY_PREFIX + row['owner'],)).fetchone()
            if not policy_row or not json.loads(policy_row['v']).get('automatic') or attempts >= 2 or daily >= steward.DAILY_LIMIT:
                return
            target=f"care:{row['id']}:{action['id']}:{report['revision']}"
            if db.execute('SELECT 1 FROM tasks WHERE kind=? AND target=?', ('steward_care_apply',target)).fetchone():
                return
            db.execute('INSERT INTO tasks(kind,target,priority,payload,published_by,created,updated) VALUES(?,?,20,?,?,?,?)',
                       ('steward_care_apply',target,json.dumps({'job':row['id'],'owner':row['owner'],'action':action['id'],'revision':report['revision'],
                        'automatic':True,'review':reason,'title':row['title']}),'media-steward',now,now))
        board.ring('tasks')
        return True


def tick():
    # Inspection is automatic; file moves, downloads and model work require explicit intent.
    for row in q("SELECT DISTINCT j.owner FROM jobs j LEFT JOIN media_care c ON c.job_id=j.id "
                 "WHERE j.owner IS NOT NULL AND j.status NOT IN ('deleted','cancelled') AND (c.checked IS NULL OR c.checked<? OR j.updated>c.checked "
                 "OR EXISTS(SELECT 1 FROM kv WHERE k='media-steward:policy:' || j.owner AND json_extract(v,'$.automatic')=1))",
                 (time.time() - INTERVAL,)):
        board.publish('steward_diagnose', 'care:' + row['owner'], 5, force=True, payload={'owner': row['owner']})


def _transfer(source, destination):
    try:
        return _transfer_file(source,destination)
    except OSError as error:
        raise ValueError('存储暂不可用或不支持安全归档，已保留现有文件；请检查存储后重试或撤销') from error


def _transfer_file(source, destination):
    """An exclusive link/unlink move: no replacement, including after a crash between the two calls."""
    source, destination = _safe(source), _safe(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe(destination)
    parent = destination.parent
    while True:
        _sync_dir(parent)
        if parent.resolve() == core.MEDIA.resolve():
            break
        parent=parent.parent
    if destination.exists():
        if source.exists() and os.path.samefile(source, destination):
            _sync_dir(destination.parent)
            source.unlink()
            _sync_dir(source.parent)
            return
        raise ValueError('目标已有文件，未覆盖')
    os.link(source, destination, follow_symlinks=False)
    _sync_dir(destination.parent)
    source.unlink()
    _sync_dir(source.parent)


def _sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def journal_mutation():
    # The intent must survive a power loss before filesystem names change.
    return board.mutation(durable=True)


def _matches(path, stamp):
    try:
        return steward._stamp(path)[1:] == stamp[1:]
    except (OSError, ValueError, RuntimeError):
        return False


def _recover(op):
    """Resume one durable move or undo. Called at startup and before a subsequent operation."""
    with journal_mutation() as db:
        # The scheduler may have finished this journal between selection and lock acquisition.
        op = db.execute('SELECT * FROM media_file_ops WHERE id=?',(op['id'],)).fetchone()
        data = json.loads(op['payload'])
        if op['state'] in ('done','undone'):
            return {'operation_id':op['id'],'files':len(data['moves']),
                    'outcome':'已撤销归档' if op['state']=='undone' else '已归档文件、字幕和封面'}
        reverse = op['state'] in ('undoing', 'rolling_back')
        moves = list(reversed(data['moves'])) if reverse else data['moves']
        row = db.execute('SELECT * FROM jobs WHERE id=?', (op['job_id'],)).fetchone()
        expected = data['after'] if op['state'] == 'undoing' else data['before']
        if (not row or any(row[k] != expected[k] for k in expected if k != 'analysis')
                or _layout_metadata(row['analysis']) != _layout_metadata(expected['analysis'])):
            raise ValueError('媒体记录已变化，停止归档并保留文件')
        if _referenced_elsewhere([m[k] for m in data['moves'] for k in ('source','destination')], op['job_id'], db):
            raise ValueError('文件出现新的共享引用，停止移动')
        if db.execute("SELECT 1 FROM tasks WHERE json_extract(payload,'$.job')=? AND state IN ('queued','running') "
                      "AND kind NOT LIKE 'steward_care_%' LIMIT 1", (op['job_id'],)).fetchone():
            raise ValueError('媒体仍有处理任务，稍后再整理')
        for move in moves:
            source, destination = (move['destination'], move['source']) if reverse else (move['source'], move['destination'])
            if Path(source).exists():
                if not _matches(source, move['stamp']):
                    raise ValueError('源文件已变化，停止归档并保留文件')
                _transfer(source, destination)
            elif not _matches(destination, move['stamp']):
                raise ValueError('无法核实中断的文件移动，请人工检查')
        result = data['before'] if reverse else data['after']
        db.execute('UPDATE jobs SET files=?,thumb=?,updated=? WHERE id=?',
                   (result['files'], result['thumb'], time.time(), op['job_id']))
        db.execute("UPDATE media_file_ops SET state=?,updated=?,error='' WHERE id=?",
                   ('undone' if reverse else 'done', time.time(), op['id']))
    library._listings.clear()
    core.ring('jobs')
    return {'operation_id': op['id'], 'files': len(data['moves']), 'outcome': '已撤销归档' if reverse else '已归档文件、字幕和封面'}


def resume(op):
    try:
        return _recover(op)
    except ValueError as error:
        board.guarded_q('UPDATE media_file_ops SET error=?,updated=? WHERE id=?', (str(error)[:500],time.time(),op['id']))
        raise


def recover():
    for op in q("SELECT * FROM media_file_ops WHERE state IN ('moving','undoing','rolling_back') AND error='' ORDER BY id LIMIT 10"):
        try:
            resume(op)
        except (OSError, ValueError, RuntimeError):
            # Preserve the journal for explicit retry; do not guess how external changes should be merged.
            continue


@board.task('steward_care_apply', '媒体管家整理与恢复', 'steward')
def apply(task, beat):
    p = task['payload']
    row = q('SELECT * FROM jobs WHERE id=? AND owner=?', (p['job'], p['owner']), one=True)
    if not row:
        return {'skipped': '媒体已移除或归属变化'}
    if p.get('automatic') and not steward.policy(p['owner'])['automatic']:
        return {'skipped': '自主处理已暂停'}
    existing = q("SELECT * FROM media_file_ops WHERE job_id=? AND owner=? AND state='moving'", (p['job'], p['owner']), one=True)
    if existing:
        return resume(existing)
    report = diagnose(row)
    if report['revision'] != p['revision'] or p['action'] not in {a['id'] for a in report['actions']}:
        return {'skipped': '文件或下载状态已变化，请重新检查'}
    if p.get('automatic') and not review_action(row, report, p['action'])[0]:
        return {'skipped': '执行前审查未通过'}
    if p['action'] == 'organize_files':
        proposal = layout(row)
        before = _snapshot(row)
        mapping = {m['source']: m['destination'] for m in proposal['moves']}
        after = dict(before, files=json.dumps([mapping.get(f, f) for f in steward._object(row['files'], [])]),
                     thumb=mapping.get(row['thumb'], row['thumb']))
        # Updated time is not a stable identity once the journal commits its own operation.
        before.pop('updated'); after.pop('updated')
        with journal_mutation() as db:
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if not current or _snapshot(current) != _snapshot(row):
                raise ValueError('媒体记录已变化')
            now = time.time()
            opid = db.execute("INSERT INTO media_file_ops(owner,job_id,state,payload,created,updated) VALUES(?,?,'moving',?,?,?)",
                              (p['owner'], p['job'], json.dumps({'before': before, 'after': after, 'moves': proposal['moves']}), now, now)).lastrowid
        result = resume(q('SELECT * FROM media_file_ops WHERE id=?', (opid,), one=True))
    else:
        with board.mutation() as db:
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            if not current or _snapshot(current) != _snapshot(row):
                raise ValueError('下载状态已变化')
            if p['action'] == 'retry_download':
                db.execute("UPDATE jobs SET status='queued',error='',progress=0,stage='',ref=NULL,attempts=0,retry_at=NULL,cancel=0,updated=? WHERE id=?",
                           (time.time(), row['id']))
                result = {'outcome': '已恢复下载，保留部分文件继续处理'}
            else:
                source = db.execute("SELECT * FROM jobs WHERE owner=? AND id!=? AND status='done' AND ref IS NULL AND url=? AND key=? LIMIT 1",
                                    (row['owner'], row['id'], row['url'], row['key'])).fetchone()
                if not source or not steward._object(source['files'], []):
                    raise ValueError('原下载文件已变化')
                for path in steward._object(source['files'], []):
                    steward._stamp(path)
                fields = {k: source[k] for k in pipeline.SHARED_FIELDS}
                fields.update(ref=source['id'], retry_at=None, updated=time.time())
                db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), row['id']))
                result = {'outcome': '已复用已有文件，避免重复下载'}
        core.ring('jobs')
    observe(q('SELECT * FROM jobs WHERE id=?', (row['id'],), one=True))
    return result


@board.task('steward_care_undo', '媒体管家撤销归档', 'steward')
def undo(task, beat):
    p = task['payload']
    with journal_mutation() as db:
        op = db.execute("SELECT * FROM media_file_ops WHERE id=? AND owner=? AND state IN ('done','undoing','moving','rolling_back')", (p['operation'], p['owner'])).fetchone()
        if not op:
            return {'skipped': '归档已撤销或不存在'}
        db.execute("UPDATE media_file_ops SET state=?,updated=?,error='' WHERE id=?", ('undoing' if op['state'] in ('done','undoing') else 'rolling_back', time.time(), op['id']))
    result = resume(q('SELECT * FROM media_file_ops WHERE id=?', (op['id'],), one=True))
    observe(q('SELECT * FROM jobs WHERE id=?', (op['job_id'],), one=True))
    # Undo expresses a preference; a future automatic round must not immediately redo it.
    q('UPDATE media_care SET dismissed=revision WHERE job_id=? AND owner=?', (op['job_id'], p['owner']))
    return result


def overview(cond, args):
    rows = q('SELECT j.id,j.title,j.updated job_updated,c.* FROM jobs j JOIN media_care c ON c.job_id=j.id AND c.owner=j.owner WHERE ' + cond + ' ORDER BY c.checked DESC', args)
    items = []
    for row in rows:
        report = json.loads(row['report'])
        if row['job_updated'] > row['checked']:
            report['stale']=True
            report['actions']=[]
        if report['issues'] and row['dismissed'] != row['revision']:
            items.append({'title': row['title'], **report})
    ops = q('SELECT o.id,o.job_id,o.state,o.error,o.updated,j.title FROM media_file_ops o JOIN jobs j ON j.id=o.job_id AND j.owner=o.owner WHERE '
            + cond + ' ORDER BY o.updated DESC LIMIT 10', args)
    tasks = q("SELECT t.kind,t.state,t.result,t.error,t.updated,j.id job_id,j.title FROM tasks t JOIN jobs j ON j.id=json_extract(t.payload,'$.job') "
              "AND j.owner=json_extract(t.payload,'$.owner') WHERE " + cond + " AND t.kind IN ('steward_care_apply','steward_care_undo') ORDER BY t.updated DESC LIMIT 10", args)
    receipts = [{'job_id': t['job_id'], 'title': t['title'], 'state': t['state'],
                 'outcome': steward._object(t['result'], {}).get('outcome') or steward._object(t['result'], {}).get('skipped') or ('处理失败，请查看任务板' if t['state'] == 'failed' else '正在处理'),
                 'updated': t['updated']} for t in tasks]
    content = q('SELECT DISTINCT t.id,t.kind,t.state,t.result,t.updated,j.id job_id,j.title,s.state asset_state,s.checked FROM media_steward_actions a '
                'JOIN jobs j ON j.id=a.job_id AND j.owner=a.owner JOIN tasks t ON t.id=a.task_id '
                'LEFT JOIN media_steward_assets s ON s.job_id=j.id AND s.owner=j.owner WHERE ' + cond + ' ORDER BY t.updated DESC LIMIT 10',args)
    labels={'chapters':'补全章节','summarize':'更新总结','steward_index':'整理字幕','transcribe':'补全转写'}
    for task in content:
        outcome='正在'+labels.get(task['kind'],'补全内容')
        if task['state']=='failed':
            outcome='内容处理失败，请查看任务板'
        elif task['state']=='done':
            if steward._object(task['result'],{}).get('skipped'):
                outcome='状态或输入已经变化，本次未改写内容'
            elif task['asset_state']=='ready' and task['checked']>=task['updated']:
                outcome='已'+labels.get(task['kind'],'补全内容')+'并验收'
            else:
                outcome='处理完成，等待后续步骤或产物验收'
        receipts.append({'job_id':task['job_id'],'title':task['title'],'state':task['state'],'updated':task['updated'],'outcome':outcome})
    return {'items': items[:25], 'total': len(items), 'checked': len(rows),
            'operations': [dict(op) for op in ops],
            'receipts': sorted(receipts,key=lambda r:r['updated'],reverse=True)[:10]}


@app.post('/api/steward/care/action')
def request_action():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get('job_id')) is not int or not isinstance(body.get('revision'), str) or body.get('action') not in ('organize_files','retry_download','reuse_download','keep_layout'):
        return steward._reply(error='整理参数无效'), 400
    cond, args = steward._visible()
    row = q('SELECT j.* FROM jobs j WHERE ' + cond + ' AND j.id=?', (*args, body['job_id']), one=True)
    if not row:
        return steward._reply(error='媒体不存在或不可见'), 404
    report = diagnose(row)
    if report['revision'] != body['revision'] or not steward._visibility_unchanged(args, [row['id']]):
        return steward._reply(error='媒体或权限已变化，请刷新'), 409
    if body['action'] == 'keep_layout':
        if not any(a['id']=='organize_files' for a in report['actions']):
            return steward._reply(error='当前没有归档建议'), 409
        observe(row)
        q('UPDATE media_care SET dismissed=? WHERE job_id=? AND owner=?', (report['revision'], row['id'], g.owner))
        return steward._reply(saved=True)
    if body['action'] not in {a['id'] for a in report['actions']}:
        return steward._reply(error='处理方案已变化'), 409
    tid = board.publish('steward_care_apply', f"care:{row['id']}:{body['action']}:{report['revision']}", 20,
                        force=True,
                        payload={'job': row['id'], 'owner': g.owner, 'revision': report['revision'], 'action': body['action'], 'title': row['title']})
    return steward._reply(task_id=tid, queued=True), 202


@app.post('/api/steward/care/undo')
def request_undo():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get('operation_id')) is not int:
        return steward._reply(error='撤销参数无效'), 400
    cond, args = steward._visible()
    op = q('SELECT o.* FROM media_file_ops o JOIN jobs j ON j.id=o.job_id AND j.owner=o.owner WHERE ' + cond +
           " AND o.id=? AND o.state IN ('done','moving','rolling_back','undoing')", (*args, body['operation_id']), one=True)
    if not op or not steward._visibility_unchanged(args, [op['job_id']]):
        return steward._reply(error='归档不存在或不可见'), 404
    tid = board.publish('steward_care_undo', 'undo:' + str(op['id']), 20, force=True,
                        payload={'job': op['job_id'], 'owner': g.owner, 'operation': op['id']})
    return steward._reply(task_id=tid, queued=True), 202
