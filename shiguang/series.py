"""Series names and pagination: a complete series occupies one place in the video list."""
import re
import unicodedata
import json
import hashlib
from pathlib import Path

from flask import g, jsonify, request, send_file
from .core import app, q, job_dict, VIDEO_EXT, AUDIO_EXT


def clean_name(name):
    name = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s*(?:第[一二三四五六七八九十百\d]+[季部期]|season\s*\d+|s\d+)\s*$",
                  "", name, flags=re.I).strip()


def numbered_name(title):
    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    match = re.match(r"^(.{2,}?)\s*(?:第[一二三四五六七八九十百零〇\d]+[集话話期]|[Ss]\d+\s*[Ee]\d+|"
                     r"([Rr][Ee])\s*\d+|[Ee][Pp]?\s*\d+)(?:\b|\s|[:·.\-]|$|[^\x00-\x7F])",
                     text, flags=re.ASCII)
    if match:
        return clean_name(match[1] + ("re" if match[2] else ""))
    match = re.match(r"^(.+?[^\d\s])\s*\d{1,4}\s*(?::|$)", text, flags=re.ASCII)
    return clean_name(match[1]) if match else ""


def series_name(job, subscriptions):
    if isinstance(job.get('series'), str):
        return job['series']
    analysis = job.get("analysis") or {}
    if analysis.get("library") == "TV" and analysis.get("show"):
        return clean_name(analysis["show"])
    source = job.get("source") or ""
    sub = subscriptions.get(source)
    if sub and sub["platform"] == "bangumi":
        return clean_name(sub["name"]) or source
    if analysis.get("library") and analysis["library"] not in ("TV", "Videos"):
        return ""
    return numbered_name(analysis.get("title") or job.get("title")) or numbered_name(job.get("title"))


def series_key(name):
    return re.sub(r"[\s·:：\-_]", "", name.lower())


def series_identity(job, subscriptions):
    return job.get('series_key') or series_key(series_name(job, subscriptions))


def page_ids(candidates, limit, subscriptions):
    """Candidates are visible videos in the requested sort order; include every member of selected series."""
    groups = {}
    for job in candidates:
        name = series_name(job, subscriptions)
        key = ("series", series_identity(job, subscriptions)) if name else ("job", job["id"])
        groups.setdefault(key, []).append(job["id"])
    selected = list(groups.values())[:limit]
    return [jid for group in selected for jid in group], len(groups) > limit


@app.after_request
def series_response(response):
    if request.path.startswith('/api/series/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/api/series/<int:jid>/cover')
def series_cover(jid):
    """Player covers require the same live owner and privacy permission as the list."""
    from . import web, library_search
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        return '', 403
    owner, reveal = g.owner, web.revealed()
    stamp = library_search.access_stamp(owner)
    row = q('SELECT thumb,files FROM jobs WHERE id=? AND owner=? AND status!=\'cancelled\'', (jid, owner), one=True)
    if not row or jid in web.hidden_ids():
        return '', 404
    candidates = [row['thumb']] + json.loads(row['files'] or '[]')
    path = next((Path(value) for value in candidates if isinstance(value, str) and value
                 and Path(value).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') and Path(value).is_file()), None)
    if path is None:
        return '', 404
    response = send_file(path, max_age=0)
    for field in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(field, None)
    if (web.revealed() != reveal or jid in web.hidden_ids() or library_search.access_stamp(owner) != stamp
            or not q('SELECT 1 FROM jobs WHERE id=? AND owner=? AND status!=\'cancelled\'', (jid, owner), one=True)
            or request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        response.close()
        return '', 403
    return response


@app.get('/api/series/<int:jid>')
def api_series(jid):
    """Light owned siblings for the player; visibility precedes grouping and counts."""
    from . import web, library_search
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        return jsonify(error='隐私权限已失效'), 403
    owner, reveal = g.owner, web.revealed()
    access = library_search.access_stamp(owner)
    hidden = web.hidden_ids()
    try:
        offset, limit = request.args.get('offset', '0'), request.args.get('limit', '100')
        if not offset.isascii() or not offset.isdecimal() or int(offset) > 1_000_000:
            raise ValueError()
        if not limit.isascii() or not limit.isdecimal() or not 1 <= int(limit) <= 100:
            raise ValueError()
        offset, limit = int(offset), int(limit)
        term = request.args.get('q', '').strip()
        if len(term) > 128:
            raise ValueError()
        order = {'added': 'created DESC,id DESC',
                 'published': "json_extract(analysis,'$.published') DESC,id DESC",
                 'title': "COALESCE(json_extract(analysis,'$.title'),title,'') COLLATE NOCASE,id DESC",
                 'author': "(COALESCE(json_extract(analysis,'$.creator'),'')=''),json_extract(analysis,'$.creator') COLLATE NOCASE,"
                           "COALESCE(json_extract(analysis,'$.title'),title,'') COLLATE NOCASE,id DESC"}[request.args.get('sort', 'added')]
    except (ValueError, KeyError):
        return jsonify(error='分集筛选参数无效'), 400
    fields = ('title', 'library', 'show', 'season', 'episode', 'episode_title', 'creator', 'published')
    analysis = 'json_object(' + ','.join("'" + field + "',json_extract(analysis,'$." + field + "')" for field in fields) + ')'
    rows = [job_dict(row) for row in q('SELECT id,title,source,status,created,files,thumb,' + analysis + ' analysis FROM jobs '
                                      "WHERE owner=? AND status!='cancelled' ORDER BY " + order, (owner,))]
    from . import native_series
    native_series.attach(rows)
    current = next((job for job in rows if job['id'] == jid), None)
    if current is None or jid in hidden:
        return jsonify(error='找不到这个视频'), 404
    subscriptions = {f"sub:{row['id']}": dict(row) for row in q('SELECT id,platform,name FROM subs WHERE owner=?', (owner,))}
    name = series_name(current, subscriptions)
    key = series_identity(current, subscriptions)
    members = [job for job in rows if key and job['id'] not in hidden and series_identity(job, subscriptions) == key]
    if term:
        needle = term.casefold()
        members = [job for job in members if needle in (str(job['analysis'].get('title') or job['title'] or '') + ' ' +
                   str(job['analysis'].get('episode_title') or '') + ' ' + str(job['analysis'].get('episode') or '')).casefold()]
    total = len(members)
    watched = {row['job_id']: bool(row['done']) for row in q('SELECT job_id,done FROM watch WHERE owner=?', (owner,))}
    items = [{'id': job['id'], 'title': job['analysis'].get('title') or job['title'] or '未命名视频',
              'analysis': job['analysis'], 'created': job['created'], 'status': job['status'],
              'watched': watched.get(job['id'], False),
              'thumb': bool(job.get('thumb') or any(isinstance(path, str) and Path(path).suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') for path in job['files'])),
              'playable': job['status'] == 'done' and any(isinstance(path, str) and Path(path).suffix.lower() in VIDEO_EXT | AUDIO_EXT for path in job['files'])}
             for job in members[offset:offset + limit]]
    for field in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(field, None)
    fresh = q("SELECT id,title,analysis,source FROM jobs WHERE id=? AND owner=? AND status!='cancelled'", (jid, owner), one=True)
    fresh = {**dict(fresh), 'analysis': json.loads(fresh['analysis'] or '{}')} if fresh else None
    if fresh:
        native_series.attach([fresh])
    remaining = q("SELECT COUNT(*) n FROM jobs WHERE owner=? AND status!='cancelled' AND id IN (SELECT value FROM json_each(?))",
                  (owner, json.dumps([job['id'] for job in members])), one=True)['n'] if members else 0
    if (fresh is None or series_identity(fresh, subscriptions) != key
            or remaining != total or web.revealed() != reveal or web.hidden_ids() != hidden
            or library_search.access_stamp(owner) != access or request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        return jsonify(error='合集或隐私权限已变化，请重新打开'), 403
    revision = [(job['id'], job['status'], job['created'], job['analysis'], job['files'], job.get('thumb'), watched.get(job['id'], False)) for job in members]
    return jsonify(items=items, total=total, offset=offset, limit=limit, more=offset + len(items) < total,
                   series=name, storage_owner=owner,
                   membership=hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()), 200, {'Cache-Control': 'no-store'}
