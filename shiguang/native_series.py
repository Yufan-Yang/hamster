"""Original creator collections, refreshed independently of video downloads.

Only platform catalogue/playlist metadata is fetched. Request handlers read the
owned local snapshot, and never wait for the network or modify job analysis.
"""
import hashlib
import http.cookiejar
import json
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .core import COOKIES, UA, kv_get, kv_set, q

PREFIX = 'native-series:v1:'
REFRESH = 86400
RETRY = 3600
PAGE_SIZE = 50
BILI_CATALOGUE_SIZE = 20  # The space catalogue rejects larger page sizes (-400).
BILI_MEMBER_SIZE = 30


class _QuietLogger:
    def debug(self, message):
        pass

    warning = debug
    error = debug


def cache_key(owner, sub_key):
    return PREFIX + hashlib.sha256((owner + '\0' + sub_key).encode()).hexdigest()


def _valid_scope(owner, sub_key):
    return (isinstance(owner, str) and bool(owner) and isinstance(sub_key, str)
            and bool(re.fullmatch(r'(?:bilibili:\d+|youtube:.+)', sub_key)))


def video_key(value):
    """Normalize known platform keys/URLs without resolving arbitrary URLs."""
    value = str(value or '')
    if value.startswith('bilibili:'):
        return re.sub(r':p\d+$', ':p', value)
    if value.startswith('youtube:'):
        return value
    u = urlsplit(value)
    host = (u.hostname or '').lower().removeprefix('www.').removeprefix('m.')
    if host == 'bilibili.com' and (m := re.match(r'/video/(BV\w+|av\d+)', u.path)):
        return f'bilibili:{m[1]}:p'
    if host == 'youtu.be' and re.fullmatch(r'[\w-]{11}', u.path.strip('/')):
        return 'youtube:' + u.path.strip('/')
    if host == 'youtube.com':
        ident = (parse_qs(u.query).get('v') or [''])[0]
        m = re.match(r'/(?:shorts|live|embed)/([\w-]{11})(?:/|$)', u.path)
        ident = ident or (m[1] if m else '')
        if re.fullmatch(r'[\w-]{11}', ident):
            return 'youtube:' + ident
    return ''


def attach(jobs):
    """Decorate selected jobs with stable native names/identities, in place.

    IDs are looked up in one light query; the database's owner/subscription
    identity is authoritative even when a caller supplies incomplete job rows.
    The caller remains responsible for its normal per-video visibility filter.
    """
    if not jobs:
        return jobs
    ids = [job['id'] for job in jobs]
    rows = q('SELECT j.id,j.key,j.owner,s.key sub_key FROM jobs j JOIN subs s '
             "ON j.source='sub:'||s.id AND j.owner=s.owner WHERE j.id IN (SELECT value FROM json_each(?)) "
             "AND s.platform IN ('bilibili','youtube')", (json.dumps(ids),))
    maps, matches = {}, {}
    for row in rows:
        scope = (row['owner'], row['sub_key'])
        if not _valid_scope(*scope):
            continue
        if scope not in maps:
            data = kv_get(cache_key(*scope), {})
            mapping = {}
            if data.get('owner') == scope[0] and data.get('sub_key') == scope[1]:
                # A video can belong to several native playlists. The first
                # collection in the platform catalogue is its stable primary.
                order = [entry['id'] for entry in data.get('catalogue', [])]
                groups = data.get('groups', {})
                for ident in order:
                    group = groups.get(ident, {})
                    if not group.get('title'):
                        continue
                    identity = 'native:' + hashlib.sha256((scope[0] + '\0' + scope[1] + '\0' + ident).encode()).hexdigest()[:24]
                    for key in group.get('members', []):
                        mapping.setdefault(key, (group['title'], identity))
            maps[scope] = mapping
        native = maps[scope].get(video_key(row['key']))
        if native:
            matches[row['id']] = native
    for job in jobs:
        if job['id'] in matches:
            job['series'], job['series_key'] = matches[job['id']]
    return jobs


def _session():
    session = requests.Session()
    session.headers.update({'User-Agent': UA, 'Referer': 'https://space.bilibili.com/', 'Accept': 'application/json'})
    if COOKIES.exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES))
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                if cookie.domain.lstrip('.').endswith('bilibili.com'):
                    session.cookies.set_cookie(cookie)
        except (OSError, http.cookiejar.LoadError):
            pass
    return session


def _bili_get(session, endpoint, params):
    response = session.get('https://api.bilibili.com/' + endpoint, params=params, timeout=(8, 15))
    response.raise_for_status()
    body = response.json()
    if body.get('code') != 0 or not isinstance(body.get('data'), dict):
        raise ValueError('B站合集元数据暂不可用')
    return body['data']


def _bili_catalogue(sub, page):
    mid = sub['key'].split(':', 1)[1]
    if not mid.isdecimal():
        raise ValueError('B站账号无效')
    with _session() as session:
        data = _bili_get(session, 'x/polymer/web-space/seasons_series_list',
                         {'mid': mid, 'page_num': page, 'page_size': BILI_CATALOGUE_SIZE})['items_lists']
    out = []
    for kind, field in (('season', 'seasons_list'), ('series', 'series_list')):
        for entry in data.get(field) or []:
            meta = entry.get('meta') or {}
            ident = str(meta.get('season_id' if kind == 'season' else 'series_id') or '')
            title = str(meta.get('name') or meta.get('title') or '').strip()
            if not ident.isdecimal() or not title:
                continue
            members = [f"bilibili:{v['bvid']}:p" for v in entry.get('archives') or [] if re.fullmatch(r'BV\w+', str(v.get('bvid') or ''))]
            total = meta.get('total')
            out.append({'id': kind + ':' + ident, 'title': title[:500],
                        'members': members, 'complete': type(total) is int and total == len(members)})
    total = int(data.get('page', {}).get('total') or 0)
    return out, bool(out) and page * BILI_CATALOGUE_SIZE < total


def _bili_members(sub, group, page):
    mid = sub['key'].split(':', 1)[1]
    kind, ident = group['id'].split(':', 1)
    if not mid.isdecimal() or not ident.isdecimal() or kind not in ('season', 'series'):
        raise ValueError('B站合集无效')
    endpoint = 'x/polymer/web-space/seasons_archives_list' if kind == 'season' else 'x/series/archives'
    params = {'mid': mid, 'season_id': ident, 'page_num': page, 'page_size': BILI_MEMBER_SIZE} if kind == 'season' else {
        'mid': mid, 'series_id': ident, 'pn': page, 'ps': BILI_MEMBER_SIZE}
    with _session() as session:
        data = _bili_get(session, endpoint, params)
    if not isinstance(data.get('archives'), list) or not isinstance(data.get('page'), dict):
        raise ValueError('B站合集分页暂不可用')
    members = [f"bilibili:{v['bvid']}:p" for v in data.get('archives') or [] if re.fullmatch(r'BV\w+', str(v.get('bvid') or ''))]
    total = int(data.get('page', {}).get('total') or 0)
    return members, bool(members) and page * BILI_MEMBER_SIZE < total


def _youtube(url, page):
    import yt_dlp
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True,
            'logger': _QuietLogger(),
            'socket_timeout': 12, 'retries': 1, 'extractor_retries': 1,
            'playliststart': (page - 1) * PAGE_SIZE + 1, 'playlistend': page * PAGE_SIZE}
    # yt-dlp writes its cookie jar on exit; metadata refresh uses a private copy.
    with tempfile.TemporaryDirectory(prefix='native-series-') as directory:
        if COOKIES.exists():
            cookie = Path(directory) / 'cookies.txt'
            cookie.write_bytes(COOKIES.read_bytes()); cookie.chmod(0o600)
            opts['cookiefile'] = str(cookie)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict) or 'entries' not in info:
            raise ValueError('YouTube 播放列表暂不可用')
        raw = list(info.get('entries') or [])
        entries = [entry for entry in raw if entry]
    return entries, len(raw) == PAGE_SIZE


def _youtube_catalogue(sub, page):
    u = urlsplit(sub['url'])
    channel = re.match(r'^/(@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)(?:/|$)', u.path)
    if (u.hostname or '').lower() not in ('youtube.com', 'www.youtube.com') or not channel:
        raise ValueError('YouTube 频道地址无效')
    entries, more = _youtube('https://www.youtube.com/' + channel[1] + '/playlists', page)
    out = []
    for entry in entries:
        ident = str(entry.get('id') or '')
        title = str(entry.get('title') or '').strip()
        if re.fullmatch(r'[\w-]{12,100}', ident) and title:
            out.append({'id': ident, 'title': title[:500], 'members': [], 'complete': False})
    return out, more


def _youtube_members(sub, group, page):
    ident = group['id']
    if not re.fullmatch(r'[\w-]{12,100}', ident):
        raise ValueError('YouTube 播放列表无效')
    entries, more = _youtube('https://www.youtube.com/playlist?list=' + ident, page)
    members = ['youtube:' + entry['id'] for entry in entries if re.fullmatch(r'[\w-]{11}', str(entry.get('id') or ''))]
    return members, more


def _same_sub(sub):
    row = q('SELECT owner,key,platform,url FROM subs WHERE id=?', (sub['id'],), one=True)
    return row is not None and all(row[key] == sub[key] for key in ('owner', 'key', 'platform', 'url'))


def sync_sub(sub):
    """One catalogue page and two playlist pages per turn, with durable cursors.

    Large channels continue next turn. A failed refresh retains every last
    successful membership; only a completed replacement removes old members.
    """
    if sub['platform'] not in ('bilibili', 'youtube') or not _valid_scope(sub['owner'], sub['key']):
        return
    key = cache_key(sub['owner'], sub['key'])
    data = kv_get(key, {})
    if data.get('owner') != sub['owner'] or data.get('sub_key') != sub['key']:
        data = {'owner': sub['owner'], 'sub_key': sub['key'], 'groups': {}, 'catalogue': [], 'catalog_due': 0}
    now = time.time()
    catalogue = _bili_catalogue if sub['platform'] == 'bilibili' else _youtube_catalogue
    members = _bili_members if sub['platform'] == 'bilibili' else _youtube_members
    try:
        if data.get('catalog_due', 0) <= now:
            page = data.get('catalog_page', 1)
            entries, more = catalogue(sub, page)
            work = {group['id']: group for group in data.get('catalog_work', [])}
            for entry in entries:
                work[entry['id']] = {'id': entry['id'], 'title': entry['title']}
                old = data['groups'].get(entry['id'], {})
                data['groups'][entry['id']] = {**old, 'title': entry['title'], 'members': old.get('members', [])}
                if entry['complete']:
                    data['groups'][entry['id']].update(members=entry['members'], updated=now)
                    data['groups'][entry['id']].pop('pending', None)
            data['catalog_work'] = list(work.values())
            if more:
                data['catalog_page'] = page + 1
                # Initial pages can already be used while the rest are fetched.
                known = {group['id']: group for group in data.get('catalogue', [])}
                data['catalogue'] = list({**known, **work}.values())
            else:
                data['catalogue'] = data.pop('catalog_work')
                data['catalog_page'] = 1
                data['catalog_due'] = now + REFRESH
                data['groups'] = {group['id']: data['groups'][group['id']] for group in data['catalogue']}
        for _ in range(2):
            due = sorted((group for group in data['catalogue'] if data['groups'][group['id']].get('updated', 0) + REFRESH <= now
                          and data['groups'][group['id']].get('retry_at', 0) <= now),
                         key=lambda group: (not bool(data['groups'][group['id']].get('pending')), data['groups'][group['id']].get('updated', 0), group['id']))
            if not due:
                break
            group = due[0]; stored = data['groups'][group['id']]
            pending = stored.get('pending') or {'page': 1, 'members': []}
            try:
                fresh, more = members(sub, group, pending['page'])
            except Exception as exc:
                stored.update(retry_at=now + RETRY, error=type(exc).__name__)
                continue
            stored.pop('error', None); stored.pop('retry_at', None)
            accumulated = list(dict.fromkeys(pending['members'] + fresh))
            if more:
                if not fresh or not set(fresh) - set(pending['members']):
                    raise ValueError('平台重复返回同一页，稍后重试')
                stored['pending'] = {'page': pending['page'] + 1, 'members': accumulated}
                stored['members'] = list(dict.fromkeys(stored.get('members', []) + accumulated))
            else:
                stored.update(members=accumulated, updated=now)
                stored.pop('pending', None)
        data.pop('error', None)
        unfinished = data.get('catalog_due', 0) <= now or any(group.get('updated', 0) + REFRESH <= now for group in data['groups'].values())
        data['next'] = now + (30 if unfinished else 3600)
    except Exception as exc:
        # Keep only the exception class: extractor messages can contain cookies,
        # signed URLs, or private platform metadata.
        data['error'] = type(exc).__name__
        data['next'] = now + RETRY
    if _same_sub(sub):
        kv_set(key, data)


def move_owner(old, new):
    """Called during account migration, before old subscriptions are removed."""
    if old == new:
        return
    for sub in q('SELECT key FROM subs WHERE owner=?', (old,)):
        if not _valid_scope(old, sub['key']) or not isinstance(new, str) or not new:
            continue
        previous = cache_key(old, sub['key']); data = kv_get(previous, {})
        if data.get('owner') != old:
            continue
        target = cache_key(new, sub['key']); current = kv_get(target, {})
        if current.get('owner') == new and current.get('sub_key') == sub['key']:
            # A merged account may already follow this creator. Preserve the
            # most recently completed group snapshots from either account.
            groups = current.setdefault('groups', {})
            for ident, group in data.get('groups', {}).items():
                if ident not in groups or group.get('updated', 0) > groups[ident].get('updated', 0):
                    groups[ident] = group
            catalogue = {group['id']: group for group in current.get('catalogue', [])}
            for group in data.get('catalogue', []):
                catalogue.setdefault(group['id'], group)
            current['catalogue'] = list(catalogue.values())
            kv_set(target, current)
        else:
            data['owner'] = new; kv_set(target, data)
        q('DELETE FROM kv WHERE k=?', (previous,))


def loop():
    while True:
        try:
            for row in q("SELECT id,owner,key,platform,url FROM subs WHERE platform IN ('bilibili','youtube') ORDER BY id"):
                sub = dict(row)
                if not _valid_scope(sub['owner'], sub['key']):
                    continue
                data = kv_get(cache_key(sub['owner'], sub['key']), {})
                if data.get('next', 0) <= time.time():
                    sync_sub(sub)
        except Exception:
            # Database contention/restart should not terminate future refreshes.
            pass
        time.sleep(30)
