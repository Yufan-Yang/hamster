"""Page owned library matches before loading cards and timed subtitle evidence."""
from functools import lru_cache
from contextlib import contextmanager, nullcontext
import json
import math
import re
import sqlite3
import time
from pathlib import Path

from . import core, crawl_search, search
from .core import job_dict, q


def access_stamp(owner):
    prefs = q('SELECT tags,ids FROM privacy WHERE owner=?', (owner,), one=True)
    aliases = q("SELECT v FROM kv WHERE k='tag_aliases'", one=True)
    return (tuple(prefs) if prefs else None, aliases['v'] if aliases else None)


@lru_cache(maxsize=128)
def _chunks(query):
    return crawl_search._wildcard_chunks(query)


def _wild(query, text):
    return bool(text is not None and crawl_search._wildcard_match(_chunks(query), str(text)))


def _metadata(query, title, url, raw):
    analysis = json.loads(raw or '{}')
    description = []
    for field in ('description', 'summary', 'tags', 'key_points', 'creator', 'show', 'episode_title'):
        value = analysis.get(field)
        if isinstance(value, str):
            description.append(value)
        elif isinstance(value, list):
            description.extend(part for part in value if isinstance(part, str))
    return any(_wild(query, value) for value in
               (' '.join(str(value) for value in (title, analysis.get('title')) if value), '\n'.join(description), url))


def _phone(device, source):
    value = device or (source[9:] if source and source.startswith('shortcut:') else '')
    if value.startswith('📱'):
        value = value[1:].lstrip('\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005'
                                 '\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff')
    return value or None


def _collected(query, mode, title, description, url, page_url, media, sources):
    fields = crawl_search._parts({'title': title, 'description': description, 'url': url, 'page_url': page_url,
                                  'media_urls': json.loads(media or '[]'), 'sources': json.loads(sources or '[]')})
    if sum(len(text) for _, text in fields) > crawl_search.MAX_ITEM_TEXT:
        raise ValueError('search item exceeds text budget')
    if mode == 'exact':
        needle = query.casefold()
        return any(needle in text.casefold() for _, text in fields)
    return any(_wild(query, text) for _, text in fields)


def register(db=None):
    # Functions are query-independent: concurrent requests cannot overwrite
    # another request's compiled pattern or owner while SQLite is executing.
    with core.db_lock if db is None else nullcontext():
        target = core.DB if db is None else db
        target.create_function('library_wildcard', 2, _wild, deterministic=True)
        target.create_function('library_wild_metadata', 4, _metadata, deterministic=True)
        target.create_function('library_phone', 2, _phone, deterministic=True)
        target.create_function('collected_search_match', 8, _collected, deterministic=True)


@contextmanager
def _reader():
    # Long subtitle scans must not hold core.q's process-wide connection lock.
    # Each request sees one read snapshot; the final permission guard still uses
    # the current live state before returning records or counts.
    db = sqlite3.connect(Path(core.DB_PATH).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    deadline = time.monotonic() + 8
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA cache_size=-2048')
        register(db)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN')
        yield db
    except sqlite3.OperationalError as exc:
        if 'interrupted' in str(exc):
            raise ValueError('搜索耗时过长，请缩小关键词或通配范围') from None
        raise
    finally:
        db.rollback()
        db.close()


def _read(db, sql, args=(), one=False):
    cursor = db.execute(sql, args)
    try:
        return cursor.fetchone() if one else cursor.fetchall()
    finally:
        cursor.close()


def _index(query, kind, db):
    from . import search_index
    condition = getattr(search_index, kind + '_condition')(query, db=db)
    return condition if condition else ('1', ())


def _literal(query):
    return '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _safe_wild_literal(query):
    # Python re.I additionally equates I/İ/ı, S/ſ and K/K. Do not require
    # these as literal SQLite matches. Other ASCII letters/digits and CJK
    # runs provide a conservative mandatory substring for the segment index.
    pieces = re.findall(r'[a-hjl-rt-zA-HJL-RT-Z0-9\u3400-\u9fff]+', query)
    return max(pieces, key=len, default='')


def _segment_predicate(query, mode, db):
    if mode == 'exact':
        sql, args = _index(query, 'segment', db)
        return sql + " AND text LIKE ? ESCAPE '\\'", (*args, _literal(query))
    literal = _safe_wild_literal(query)
    sql, args = _index(literal, 'segment', db) if literal else ('1', ())
    if literal:
        return sql + " AND CASE WHEN text LIKE ? ESCAPE '\\' THEN library_wildcard(?,text) ELSE 0 END", (*args, _literal(literal), query)
    return sql + ' AND library_wildcard(?,text)', (*args, query)


def _wild_excerpt(query, text):
    patterns = _chunks(query)
    match = patterns[0].search(text) if patterns else None
    start = max(0, match.start() - 30) if match else 0
    return ('…' if start else '') + text[start:start + 240] + ('…' if start + 240 < len(text) else '')


def _valid_hit(row, query, mode):
    part, at = row['part'], row['t']
    if type(part) is not int or part < 0:
        return None
    if at is not None and (not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0):
        return None
    text = row['text'] or ''
    if mode == 'exact':
        text = search.snippet(text, query, 10, 60) if len(text) > 80 else text
    else:
        text = _wild_excerpt(query, text)
    return {'part': part, 't': at, 'src': row['src'] or '字幕', 'text': text}


def find(query, mode, scope, args, order, columns, limit=100, offset=0):
    """Return a bounded page and total; callers enforce access again before replying."""
    with _reader() as db:
        return _find(db, query, mode, scope, args, order, columns, limit, offset)


def _find(db, query, mode, scope, args, order, columns, limit, offset):
    def q(sql, args=(), one=False):
        return _read(db, sql, args, one)
    visible = q(f"SELECT id,ref,status FROM jobs WHERE {scope}", args)
    universal = mode == 'wildcard' and not query.strip('*')
    by_source = {}
    for row in visible:
        if row['status'] in ('done', 'linked'):
            by_source.setdefault(row['ref'] or row['id'], []).append(row['id'])
    if mode == 'exact':
        prefilter, index_args = _index(query, 'job', db)
        match = ' OR '.join(column + " LIKE ? ESCAPE '\\'" for column in
                            ('title', 'url', 'analysis', 'files', 'transcript'))
        metadata = q(f'SELECT id FROM jobs WHERE {scope} AND ({prefilter}) AND ({match})',
                     (*args, *index_args, *(_literal(query),) * 5))
    elif universal:
        metadata = visible
    else:
        anchor = _safe_wild_literal(query)
        transcript_match = ("CASE WHEN transcript LIKE ? ESCAPE '\\' THEN library_wildcard(?,transcript) ELSE 0 END"
                            if anchor else 'library_wildcard(?,transcript)')
        transcript_args = (_literal(anchor), query) if anchor else (query,)
        metadata = q(f'SELECT id FROM jobs WHERE {scope} AND '
                     '(library_wild_metadata(?,title,url,analysis) OR ' + transcript_match + ')',
                     (*args, query, *transcript_args))
    ids = {row['id'] for row in metadata}
    predicate, segment_args = _segment_predicate(query, mode, db)
    sources = json.dumps(list(by_source))
    if by_source and not universal and not {row['id'] for row in visible}.issubset(ids):
        matched = q("SELECT DISTINCT ref FROM seg WHERE kind='job' AND ref IN (SELECT value FROM json_each(?)) "
                    'AND ' + predicate, (sources, *segment_args))
        for row in matched:
            ids.update(by_source.get(row['ref'], ()))
    total = len(ids)
    rows = q(f'SELECT {columns} FROM jobs WHERE {scope} AND id IN (SELECT value FROM json_each(?)) '
             f'ORDER BY {order} LIMIT ? OFFSET ?', (*args, json.dumps(list(ids)), limit, offset)) if ids else []
    page_sources = {row['ref'] or row['id'] for row in rows if row['status'] in ('done', 'linked')}
    said = {}
    if page_sources and not universal:
        hits = q("WITH hits AS (SELECT ref,part,t,src,text,ROW_NUMBER() OVER(PARTITION BY ref ORDER BY part,t,id) n "
                 "FROM seg WHERE kind='job' AND ref IN (SELECT value FROM json_each(?)) AND " + predicate +
                 ') SELECT ref,part,t,src,text FROM hits WHERE n<=30 ORDER BY ref,part,t',
                 (json.dumps(list(page_sources)), *segment_args))
        for row in hits:
            hit = _valid_hit(row, query, mode)
            if hit:
                said.setdefault(row['ref'], []).append(hit)
    out = []
    for row in rows:
        item = job_dict(row)
        hits = said.get(row['ref'] or row['id'], [])
        if not hits and row['status'] == 'done' and not universal:
            # Old/unindexed downloads retain their existing file/timeline
            # fallback, but only for the requested page and transcript matches.
            predicate = "transcript LIKE ? ESCAPE '\\'" if mode == 'exact' else 'library_wildcard(?,transcript)'
            transcript_arg = _literal(query) if mode == 'exact' else query
            spoken = q('SELECT transcript,segments FROM jobs WHERE id=? AND (' + predicate + ')',
                       (row['id'], transcript_arg), one=True)
            if spoken:
                if mode == 'exact':
                    hits = [{**hit, 'src': '字幕'} for hit in search.subtitle_hits(item, spoken, query)]
                else:
                    segments = json.loads(spoken['segments'] or '[]')
                    for segment in segments if isinstance(segments, list) else []:
                        if not isinstance(segment, (list, tuple)) or len(segment) != 2 or not isinstance(segment[1], str):
                            continue
                        at, text = segment
                        if _wild(query, text):
                            hit = _valid_hit({'part': 0, 't': at, 'src': '字幕', 'text': text}, query, mode)
                            if hit:
                                hits.append(hit)
                                if len(hits) == 30:
                                    break
                if not hits:
                    text = search.snippet(spoken['transcript'], query, 30, 100) if mode == 'exact' else _wild_excerpt(query, spoken['transcript'])
                    hits = [{'part': 0, 't': None, 'src': '字幕', 'text': text}]
        item['hits'] = hits
        if hits and (mode != 'exact' or query.lower() not in search.titleish(item).lower()):
            item['match_where'], item['match'] = hits[0]['src'], hits[0]['text']
        elif mode == 'exact' and query.lower() not in search.titleish(item).lower():
            item['match_where'], item['match'] = next(((label, search.snippet(text, query)) for label, text in
                search.match_fields(item, {'transcript': ''}) if query.lower() in text.lower()), ('', ''))
        out.append(item)
    return out, {'search_total': total, 'search_offset': offset, 'search_limit': limit}, offset + len(out) < total


def collected_page(query, mode, owner, visible, hidden, limit, offset, *, directory=False, reveal=False):
    """Filter/count within SQL, decoding only the current page into response objects."""
    with _reader() as db:
        return _collected_page(db, query, mode, owner, visible, hidden, limit, offset, directory, reveal)


def _collected_page(db, query, mode, owner, visible, hidden, limit, offset, directory, reveal):
    def q(sql, args=(), one=False):
        return _read(db, sql, args, one)
    interpretation = crawl_search.search_items([], query, mode, owner)['interpretation']
    if not visible:
        return {'items': [], 'interpretation': interpretation}, 0
    provenance = '' if reveal else ',json_group_array(DISTINCT c.url) _public_sources,MAX(c.url) _public_page'
    eligible = ('WITH eligible AS MATERIALIZED (SELECT i.id,MAX(c.id) crawl_id,i.updated' + provenance + ' '
                'FROM crawl_items i JOIN crawl_results r ON r.item_id=i.id JOIN crawls c ON c.id=r.crawl_id '
                'WHERE i.owner=? AND c.owner=? AND c.id IN (SELECT value FROM json_each(?)) '
                'AND (i.job_id IS NULL OR i.job_id NOT IN (SELECT value FROM json_each(?))) GROUP BY i.id) ')
    args = (owner, owner, json.dumps(sorted(visible)), json.dumps(sorted(hidden)))
    match_query = bool(query) and not (mode == 'wildcard' and not query.strip('*'))
    page_url, sources = ('i.page_url', 'i.sources') if reveal else ('e._public_page', 'e._public_sources')
    predicate = ('collected_search_match(?,?,i.title,i.description,i.url,' + page_url + ',i.media_urls,' + sources + ')'
                 if match_query else '1')
    if match_query:
        args += (query, mode)
    match = 'FROM eligible e JOIN crawl_items i ON i.id=e.id WHERE ' + predicate
    order = 'e.id' if directory else 'e.updated DESC,e.id DESC'
    try:
        # Keep the count/sort window narrow. Selecting i.* here would spool all
        # matching descriptions and media JSON before LIMIT, even for browsing.
        public_columns = '' if reveal else ',e._public_sources,e._public_page'
        rows = q(eligible + ', page AS MATERIALIZED (SELECT e.id,e.crawl_id,e.updated' + public_columns + ','
                 'COUNT(*) OVER() AS _search_total ' + match +
                 ' ORDER BY ' + order + ' LIMIT ? OFFSET ?) '
                 'SELECT i.*,e.crawl_id,e._search_total' + public_columns + ' FROM page e JOIN crawl_items i ON i.id=e.id '
                 'ORDER BY ' + order, (*args, limit, offset))
        total = rows[0]['_search_total'] if rows else (
            q(eligible + 'SELECT COUNT(*) n ' + match, args, one=True)['n'] if offset else 0)
    except sqlite3.OperationalError as exc:
        if 'user-defined function raised' in str(exc):
            raise ValueError('部分采集记录的搜索字段无效，请检查来源任务') from None
        raise
    items = []
    for row in rows:
        item = dict(row)
        item.pop('owner', None)
        item.pop('_search_total', None)
        item['sources'] = json.loads(item['sources'])
        item['media_urls'] = json.loads(item['media_urls'])
        if not reveal:
            item['sources'] = json.loads(item.pop('_public_sources'))
            item['page_url'] = item.pop('_public_page') or ''
        items.append(item)
    return {'items': collected_durations(items, owner), 'interpretation': interpretation}, total


def collected_durations(items, owner):
    """Reuse already measured owned downloads; no filesystem or media requests."""
    pending = set()
    for item in items:
        value = item.get('duration')
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            item['duration'] = None
            if item.get('job_id'):
                pending.add(item['job_id'])
    durations, videos = {}, {}
    if pending:
        rows = q('SELECT j.id,f.value path,p.duration FROM jobs j JOIN json_each(j.files) f '
                 'LEFT JOIN probes p ON p.path=f.value WHERE j.owner=? AND j.id IN (SELECT value FROM json_each(?))',
                 (owner, json.dumps(list(pending))))
        for row in rows:
            if not isinstance(row['path'], str) or Path(row['path']).suffix.lower() not in core.VIDEO_EXT:
                continue
            videos.setdefault(row['id'], set()).add(row['path'])
            value = row['duration']
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                durations.setdefault(row['id'], {})[row['path']] = value
    for item in items:
        measured = durations.get(item.get('job_id'), {})
        if item['duration'] is None and len(videos.get(item.get('job_id'), ())) == 1 and len(measured) == 1:
            item['duration'] = next(iter(measured.values()))
    return items
