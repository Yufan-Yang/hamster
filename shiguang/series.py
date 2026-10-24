"""Series names and pagination: a complete series occupies one place in the video list."""
import re
import unicodedata
import json
import hashlib
from pathlib import Path

from flask import g, jsonify, request
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


def page_ids(candidates, limit, subscriptions):
    """Candidates are visible videos in the requested sort order; include every member of selected series."""
    groups = {}
    for job in candidates:
        name = series_name(job, subscriptions)
        key = ("series", series_key(name)) if name else ("job", job["id"])
        groups.setdefault(key, []).append(job["id"])
    selected = list(groups.values())[:limit]
    return [jid for group in selected for jid in group], len(groups) > limit


@app.after_request
def series_response(response):
    if request.path.startswith('/api/series/'):
        response.headers['Cache-Control'] = 'no-store'
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
    rows = [job_dict(row) for row in q('SELECT id,title,source,status,created,files,' + analysis + ' analysis FROM jobs '
                                      "WHERE owner=? AND status!='cancelled' ORDER BY " + order, (owner,))]
    current = next((job for job in rows if job['id'] == jid), None)
    if current is None or jid in hidden:
        return jsonify(error='找不到这个视频'), 404
    subscriptions = {f"sub:{row['id']}": dict(row) for row in q('SELECT id,platform,name FROM subs WHERE owner=?', (owner,))}
    name = series_name(current, subscriptions)
    key = series_key(name)
    members = [job for job in rows if key and job['id'] not in hidden and series_key(series_name(job, subscriptions)) == key]
    if term:
        needle = term.casefold()
        members = [job for job in members if needle in (str(job['analysis'].get('title') or job['title'] or '') + ' ' +
                   str(job['analysis'].get('episode_title') or '') + ' ' + str(job['analysis'].get('episode') or '')).casefold()]
    total = len(members)
    watched = {row['job_id']: bool(row['done']) for row in q('SELECT job_id,done FROM watch WHERE owner=?', (owner,))}
    items = [{'id': job['id'], 'title': job['analysis'].get('title') or job['title'] or '未命名视频',
              'analysis': job['analysis'], 'created': job['created'], 'status': job['status'],
              'watched': watched.get(job['id'], False),
              'playable': job['status'] == 'done' and any(isinstance(path, str) and Path(path).suffix.lower() in VIDEO_EXT | AUDIO_EXT for path in job['files'])}
             for job in members[offset:offset + limit]]
    for field in ('hidden_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(field, None)
    fresh = q("SELECT title,analysis,source FROM jobs WHERE id=? AND owner=? AND status!='cancelled'", (jid, owner), one=True)
    remaining = q("SELECT COUNT(*) n FROM jobs WHERE owner=? AND status!='cancelled' AND id IN (SELECT value FROM json_each(?))",
                  (owner, json.dumps([job['id'] for job in members])), one=True)['n'] if members else 0
    if (fresh is None or series_key(series_name({**dict(fresh), 'analysis': json.loads(fresh['analysis'] or '{}')}, subscriptions)) != key
            or remaining != total or web.revealed() != reveal or web.hidden_ids() != hidden
            or library_search.access_stamp(owner) != access or request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        return jsonify(error='合集或隐私权限已变化，请重新打开'), 403
    revision = [(job['id'], job['status'], job['created'], job['analysis'], job['files'], watched.get(job['id'], False)) for job in members]
    return jsonify(items=items, total=total, offset=offset, limit=limit, more=offset + len(items) < total,
                   series=name, storage_owner=owner,
                   membership=hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()), 200, {'Cache-Control': 'no-store'}
