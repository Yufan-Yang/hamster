"""Persistent directory collection: bounded fetching, incremental scans and video identities."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import re
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from flask import Response, g, has_request_context, jsonify, request, stream_with_context

from . import core, download, pipeline, web
from .core import app, q, ring, UA
from .crawl_extract import extract_page, parse_duration
from .crawl_net import PublicAdapter
from .crawl_search import search_items
from .crawl_source import classify_source, known_adult

MAX_PAGE_LINKS = 5000
MAX_IMPORT_ITEMS = 5000
MAX_BYTES = 4 << 20
RETRY_DELAYS = (2, 10)  # three total attempts, with short waits after failures one and two
MEDIA_EXT = re.compile(r'\.(?:mp4|webm|mkv|mov|m4v|flv|m3u8|mpd)(?:$|[?#])', re.I)
TRACKING = re.compile(r'^(?:utm_.*|spm(?:_id_from)?|vd_source|fbclid|gclid|si|share_.*)$', re.I)
SIGNATURE = re.compile(r'^(?:token|expires?|signature|sign|auth_key|wsSecret|wsTime|Policy|Key-Pair-Id|X-Amz-.*)$', re.I)
_import_lock = threading.Lock()
_run_context = threading.local()


class Stopped(Exception):
    pass


def http_url(value, base=''):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError('请输入有效的 HTTP 或 HTTPS 网址')
    value = urljoin(base, value.strip())
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError()
        _ = p.port
    except ValueError:
        raise ValueError('请输入有效的 HTTP 或 HTTPS 网址') from None
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', p.query, ''))


def identity(url, media=False):
    """Never erase content IDs in queries; strip short-lived credentials only on media files."""
    p = urlsplit(http_url(url))
    host = p.hostname.lower().removeprefix('www.').removeprefix('m.')
    if host in ('youtube.com', 'music.youtube.com', 'youtu.be', 'bilibili.com'):
        return core.link_key(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not TRACKING.match(k) and not (media and MEDIA_EXT.search(p.path) and SIGNATURE.match(k))]
    netloc = p.netloc.lower()
    if (p.scheme == 'https' and p.port == 443) or (p.scheme == 'http' and p.port == 80):
        netloc = p.hostname.lower()
    # Unknown hosts and http/https remain distinct: they need not serve the same content.
    return urlunsplit((p.scheme, netloc, p.path or '/', urlencode(sorted(query)), ''))


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


def source_visible(row, reveal=False):
    """Unknown sources stay private until this request explicitly reveals them."""
    return bool(reveal) or dict(row).get('source_classification') == 'general'


def _request_access():
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        raise PermissionError('隐私权限已失效，请重新解锁')
    return g.owner, web.revealed()


def _access_guard(row=None):
    expected = g.get('crawl_request_access', _request_access())
    if _request_access() != expected:
        raise PermissionError('隐私权限已变化，请重新加载')
    if row is not None:
        original = g.get('crawl_request_root')
        if (row['owner'] != expected[0] or not source_visible(row, expected[1])
                or original and original != (row['id'], row['url_key'])):
            raise PermissionError('请解锁隐私并显示隐藏内容后操作')


def _remember_root(row):
    g.setdefault('crawl_response_roots', {})[row['id']] = (row['owner'], row['url_key'], row['source_classification'])


@app.before_request
def _crawl_privacy_entry():
    if not (request.path == '/api/crawls' or request.path.startswith('/api/crawls/')):
        return
    try:
        g.crawl_request_access = _request_access()
        cid = (request.view_args or {}).get('cid')
        if cid is not None:
            row = _visible(cid)
            if row:
                g.crawl_request_root = (row['id'], row['url_key'])
                if request.method != 'GET' or request.path.endswith('/export'):
                    _access_guard(row)
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403


@app.after_request
def _crawl_privacy_response(response):
    if not (request.path == '/api/crawls' or request.path.startswith('/api/crawls/')):
        return response
    # Authentication may reject the request before the crawl hook runs.
    if 'crawl_request_access' not in g:
        response.headers['Cache-Control'] = 'no-store'
        return response
    try:
        _access_guard()
        for cid, expected in g.get('crawl_response_roots', {}).items():
            fresh = _visible(cid)
            if (not fresh or (fresh['owner'], fresh['url_key'], fresh['source_classification']) != expected
                    or not source_visible(fresh, web.revealed())):
                raise PermissionError('来源或隐私权限已变化，请重新加载')
        body = response.get_json(silent=True) if response.is_json else None
        if isinstance(body, dict):
            body.update(revealed=web.revealed(), storage_owner=g.owner)
            response.set_data(app.json.dumps(body))
        _access_guard()
    except PermissionError as exc:
        response = jsonify(error=str(exc), revealed=False)
        response.status_code = 403
    response.headers['Cache-Control'] = 'no-store'
    return response


def public_provenance(items, owner, reveal=None):
    """Only current visible root URLs are provable without a per-root history ledger."""
    reveal = web.revealed() if reveal is None else reveal
    if reveal or not items:
        return items
    roots = {}
    for row in q('SELECT r.item_id,c.id,c.url FROM crawl_results r JOIN crawls c ON c.id=r.crawl_id '
                 "WHERE c.owner=? AND c.source_classification='general' AND r.item_id IN (SELECT value FROM json_each(?)) "
                 'ORDER BY c.id DESC', (owner, json.dumps([item['id'] for item in items]))):
        roots.setdefault(row['item_id'], []).append((row['id'], row['url']))
    for item in items:
        allowed = roots.get(item['id'], [])
        item['sources'] = list(dict.fromkeys(url for _, url in allowed))
        item['page_url'] = next((url for cid, url in allowed if cid == item.get('crawl_id')),
                                allowed[0][1] if allowed else '')
    return items


def visible_source_ids(owner, reveal=False):
    return {row['id'] for row in q('SELECT id,source_classification FROM crawls WHERE owner=?', (owner,))
            if source_visible(row, reveal)}


def hidden_source_job_ids(owner, reveal=False):
    """Persisted source membership survives task removal and archived jobs.

    A merged video with an independently general source is allowed, matching
    collected search's filtering of sources before merging duplicate items.
    """
    if reveal:
        return set()
    inherited = {row['job_id'] for row in q('SELECT job_id FROM crawl_source_jobs WHERE owner=?', (owner,))}
    inherited |= {row['id'] for row in q("SELECT id FROM jobs WHERE owner=? AND source LIKE 'crawl:%'", (owner,))}
    inherited |= {row['job_id'] for row in q("SELECT job_id FROM job_trash WHERE owner=? AND job_id IS NOT NULL "
                    "AND json_extract(snapshot,'$.source') LIKE 'crawl:%'", (owner,))}
    general = {row['job_id'] for row in q('SELECT j.job_id FROM crawl_source_jobs j JOIN crawl_sources s '
                "ON s.owner=j.owner AND s.url_key=j.url_key WHERE j.owner=? AND s.classification='general'", (owner,))}
    # Old rows may not yet have a ledger entry. Never use a task's new URL to
    # override an existing entry for the URL from which a video was imported.
    general |= {row['id'] for row in q("SELECT j.id FROM jobs j JOIN crawls c ON j.source='crawl:'||c.id "
                "AND j.owner=c.owner WHERE j.owner=? AND c.source_classification='general' AND NOT EXISTS "
                '(SELECT 1 FROM crawl_source_jobs s WHERE s.owner=j.owner AND s.job_id=j.id)', (owner,))}
    return inherited - general


def _source_state(db, owner, url, key):
    db.execute('INSERT OR IGNORE INTO crawl_sources(owner,url_key) VALUES(?,?)', (owner, key))
    source = db.execute('SELECT * FROM crawl_sources WHERE owner=? AND url_key=?', (owner, key)).fetchone()
    if known_adult(url) and source['classification'] != 'adult':
        db.execute("UPDATE crawl_sources SET classification='adult',reason='来源网址属于成人内容站点',checked=? "
                   'WHERE owner=? AND url_key=?', (time.time(), owner, key))
        source = db.execute('SELECT * FROM crawl_sources WHERE owner=? AND url_key=?', (owner, key)).fetchone()
    db.execute('UPDATE crawls SET source_classification=?,source_reason=?,source_checked=? WHERE owner=? AND url_key=?',
               (source['classification'], source['reason'], source['checked'], owner, key))
    return source


def _sync_known_sources(db):
    """Upgrade old tasks without fetching pages or repeating a model decision."""
    for row in db.execute("SELECT DISTINCT owner,url,url_key FROM crawls WHERE "
                          "source_classification!='adult' OR source_checked IS NULL").fetchall():
        if known_adult(row['url']):
            _source_state(db, row['owner'], row['url'], row['url_key'])


def _classify_root(cid, metadata, generation=None):
    """Claim one persistent classification per owner's root, never per video."""
    with transaction() as db:
        row = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        if not row or (generation is not None and (row['generation'] != generation or row['status'] != 'running')):
            raise Stopped()
        owner, key, url = row['owner'], row['url_key'], row['url']
        source = _source_state(db, owner, url, key)
        if source['checked'] is not None:
            return
        claimed = time.time()
        # Persist the attempt before making the model call. An interrupted or
        # failed attempt remains unknown instead of billing once per refresh.
        db.execute("UPDATE crawl_sources SET checked=?,reason='来源判别中' WHERE owner=? AND url_key=?", (claimed, owner, key))
        db.execute("UPDATE crawls SET source_checked=?,source_reason='来源判别中' WHERE owner=? AND url_key=?", (claimed, owner, key))
    try:
        from . import ai_activity
        with ai_activity.context(owner, private=True):
            result = classify_source(url, str(metadata.get('title') or '')[:500], str(metadata.get('description') or '')[:2000])
        classification, reason = result['classification'], result['reason']
        if classification not in ('adult', 'general', 'unknown') or not isinstance(reason, str):
            raise ValueError('invalid source classification')
    except Exception:
        classification, reason = 'unknown', '来源判别暂不可用，解锁隐私菜单并显示隐藏后可查看'
    with transaction() as db:
        db.execute('UPDATE crawl_sources SET classification=?,reason=? WHERE owner=? AND url_key=? AND checked=?',
                   (classification, reason[:300], owner, key, claimed))
        source = db.execute('SELECT * FROM crawl_sources WHERE owner=? AND url_key=?', (owner, key)).fetchone()
        if source:
            db.execute('UPDATE crawls SET source_classification=?,source_reason=?,source_checked=? WHERE owner=? AND url_key=?',
                       (source['classification'], source['reason'], source['checked'], owner, key))
    ring('crawls')


def _unique(values, limit=500):
    return list(dict.fromkeys(x for x in values if isinstance(x, str) and x))[:limit]


def _media_unique(values):
    latest = {}
    for url in values:
        latest.setdefault(identity(url, True), url)
    return list(latest.values())[:30]


def _merge(db, keep, other):
    if keep == other:
        return
    a = db.execute('SELECT * FROM crawl_items WHERE id=?', (keep,)).fetchone()
    b = db.execute('SELECT * FROM crawl_items WHERE id=?', (other,)).fetchone()
    if not a or not b:
        return
    from . import sources as source_center
    source_center.merge_crawl(db, keep, other)
    media = _media_unique(json.loads(b['media_urls']) + json.loads(a['media_urls']))
    sources = _unique(json.loads(a['sources']) + json.loads(b['sources']))
    db.execute('UPDATE crawl_items SET media_urls=?,sources=?,description=?,job_id=COALESCE(job_id,?),'
               'duration=COALESCE(duration,?),updated=? WHERE id=?',
               (json.dumps(media), json.dumps(sources), max((a['description'], b['description']), key=len),
                b['job_id'], b['duration'], time.time(), keep))
    db.execute('UPDATE crawl_aliases SET item_id=? WHERE item_id=?', (keep, other))
    db.execute('INSERT OR IGNORE INTO crawl_results SELECT crawl_id,? FROM crawl_results WHERE item_id=?', (keep, other))
    db.execute('DELETE FROM crawl_results WHERE item_id=?', (other,))
    db.execute('DELETE FROM crawl_items WHERE id=?', (other,))


def move_owner(previous, owner):
    """Carry anonymous collections into the signed-in account, merging its existing aliases."""
    if previous == owner:
        return
    with transaction() as db:
        for alias in db.execute('SELECT * FROM crawl_aliases WHERE owner=?', (previous,)).fetchall():
            old = db.execute('SELECT item_id FROM crawl_aliases WHERE owner=? AND url_key=?',
                             (previous, alias['url_key'])).fetchone()
            existing = db.execute('SELECT item_id FROM crawl_aliases WHERE owner=? AND url_key=?',
                                  (owner, alias['url_key'])).fetchone()
            if old and existing:
                _merge(db, existing['item_id'], old['item_id'])
                db.execute('DELETE FROM crawl_aliases WHERE owner=? AND url_key=?', (previous, alias['url_key']))
        db.execute('UPDATE crawl_aliases SET owner=? WHERE owner=?', (owner, previous))
        db.execute('UPDATE crawl_items SET owner=? WHERE owner=?', (owner, previous))
        db.execute('UPDATE crawls SET owner=? WHERE owner=?', (owner, previous))
        db.execute('INSERT OR IGNORE INTO crawl_sources SELECT ?,url_key,classification,reason,checked FROM crawl_sources WHERE owner=?',
                   (owner, previous))
        db.execute('INSERT OR IGNORE INTO crawl_source_jobs SELECT ?,job_id,url_key FROM crawl_source_jobs WHERE owner=?', (owner, previous))
        db.execute('DELETE FROM crawl_sources WHERE owner=?', (previous,))
        db.execute('DELETE FROM crawl_source_jobs WHERE owner=?', (previous,))
        db.execute('UPDATE crawls SET source_classification=(SELECT classification FROM crawl_sources s WHERE s.owner=crawls.owner AND s.url_key=crawls.url_key),'
                   'source_reason=(SELECT reason FROM crawl_sources s WHERE s.owner=crawls.owner AND s.url_key=crawls.url_key),'
                   'source_checked=(SELECT checked FROM crawl_sources s WHERE s.owner=crawls.owner AND s.url_key=crawls.url_key) '
                   'WHERE owner=? AND EXISTS(SELECT 1 FROM crawl_sources s WHERE s.owner=crawls.owner AND s.url_key=crawls.url_key)', (owner,))


def _running(cid):
    row = q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
    expected = getattr(_run_context, 'claim', None)
    if not row or row['status'] != 'running' or (expected and expected[0] == cid and row['generation'] != expected[1]):
        raise Stopped()
    return row


class Fetcher:
    """Check every redirect and browser request; never follow a page into the home network."""
    def __init__(self, cid=None):
        self.cid = cid
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA
        self.session.trust_env = False
        self.session.mount('http://', PublicAdapter())
        self.session.mount('https://', PublicAdapter())

    def close(self):
        self.session.close()

    def check(self):
        if self.cid is not None:
            _running(self.cid)

    def validate(self, url):
        url = http_url(url)
        reason = web.internal_link(url)
        if reason:
            raise ValueError(reason)
        return url

    def get(self, url, referer=''):
        deadline = time.monotonic() + 60
        for _ in range(6):
            self.check()
            url = self.validate(url)
            with self.session.get(url, headers={'Referer': referer} if referer else {},
                                  timeout=(8, 30), allow_redirects=False, stream=True) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    url = http_url(response.headers.get('Location', ''), url)
                    continue
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '').lower()
                if not any(t in content_type for t in ('text/', 'json', 'xml', 'javascript')) and content_type:
                    raise ValueError('这个地址不是网页或公开播放器配置')
                data = bytearray()
                for chunk in response.iter_content(65536):
                    self.check()
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError('页面超过 4 MB，已跳过')
                    if time.monotonic() > deadline:
                        raise ValueError('读取页面超时')
                charset = re.search(r'charset=["\x27]?([\w-]+)', content_type)
                if not charset:
                    charset = re.search(r'charset\s*=\s*["\x27]?([\w-]+)', bytes(data[:4096]).decode('ascii', 'ignore'), re.I)
                try:
                    text = bytes(data).decode(charset[1] if charset else 'utf-8', 'replace')
                except LookupError:
                    text = bytes(data).decode('utf-8', 'replace')
                return text, url
        raise ValueError('网页重定向次数过多')

    def render(self, url):
        from playwright.sync_api import sync_playwright
        locks = core.STATE / 'locks'
        locks.mkdir(parents=True, exist_ok=True)
        with open(locks / 'browser-0.lock', 'a') as handle:
            deadline = time.monotonic() + 30
            while True:
                self.check()
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise ValueError('浏览器繁忙，可以稍后继续采集')
                    time.sleep(.25)
            with sync_playwright() as p:
                opts = {'headless': True, 'args': ['--no-sandbox', '--mute-audio']}
                if Path(core.CHROMIUM).exists():
                    opts['executable_path'] = core.CHROMIUM
                browser = p.chromium.launch(**opts)
                try:
                    ctx = browser.new_context(user_agent=UA, service_workers='block')
                    navigation_errors = []
                    def route(r):
                        main_document = r.request.resource_type == 'document' and r.request.frame == page.main_frame
                        try:
                            self.check()
                            address = self.validate(r.request.url)
                            if r.request.resource_type in ('image', 'media', 'font'):
                                r.abort()
                            else:
                                # Chromium must not resolve the validated hostname
                                # again. Fulfill all page/script/XHR traffic through
                                # the same pinned public socket transport as HTML.
                                headers = {key: value for key, value in r.request.all_headers().items()
                                           if key.lower() not in ('host', 'content-length', 'connection')}
                                with self.session.request(r.request.method, address, headers=headers,
                                                          data=r.request.post_data_buffer, timeout=(8, 30),
                                                          allow_redirects=False, stream=True) as response:
                                    if main_document:
                                        response.raise_for_status()
                                    body = bytearray()
                                    for chunk in response.iter_content(65536):
                                        self.check()
                                        body.extend(chunk)
                                        if len(body) > MAX_BYTES:
                                            raise ValueError('浏览器资源超过 4 MB')
                                    if response.is_redirect or response.is_permanent_redirect:
                                        self.validate(http_url(response.headers.get('Location', ''), address))
                                        # Browser routing can skip later hops in a
                                        # redirect chain. Follow these with our
                                        # checked transport instead of Chromium.
                                        body, _ = self.get(http_url(response.headers.get('Location', ''), address), address)
                                        content_type = {'script': 'application/javascript', 'stylesheet': 'text/css',
                                                        'document': 'text/html'}.get(r.request.resource_type, 'text/plain')
                                        r.fulfill(status=200, content_type=content_type, body=body)
                                        return
                                    response_headers = {key: value for key, value in response.headers.items()
                                                        if key.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')}
                                    r.fulfill(status=response.status_code, headers=response_headers, body=bytes(body))
                        except (Stopped, ValueError, requests.RequestException) as exc:
                            if main_document or isinstance(exc, Stopped):
                                navigation_errors.append(exc)
                            r.abort()
                    ctx.route('**/*', route)
                    if hasattr(ctx, 'route_web_socket'):
                        ctx.route_web_socket('**/*', lambda ws: ws.close())
                    page = ctx.new_page()
                    page.on('dialog', lambda dialog: dialog.dismiss())
                    try:
                        page.goto(self.validate(url), wait_until='domcontentloaded', timeout=30000)
                    except Exception:
                        # Playwright reports a generic aborted navigation when
                        # our checked transport rejects the main document. Keep
                        # its actual HTTP/timeout error for bounded page retries.
                        if navigation_errors:
                            raise navigation_errors[-1]
                        raise
                    if navigation_errors:
                        raise navigation_errors[-1]
                    for _ in range(3):
                        self.check()
                        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                        page.wait_for_timeout(600)
                    final = self.validate(page.url)
                    rendered = page.content()
                    if len(rendered.encode()) > MAX_BYTES:
                        raise ValueError('渲染后的页面过大，已跳过')
                    for c in ctx.cookies():
                        self.session.cookies.set(c['name'], c['value'], domain=c['domain'], path=c['path'])
                    return rendered, final
                finally:
                    browser.close()


def _extract(fetcher, url, listing, source=''):
    text, final = fetcher.get(url, source)
    result = extract_page(text, final, listing=listing)
    if not result['videos'] and not result['links'] and not result['lazy_players'] and '<script' in text.lower():
        text, final = fetcher.render(final)
        result = extract_page(text, final, listing=listing)
    for endpoint in result['lazy_players'][:30]:
        try:
            raw, resolved = fetcher.get(endpoint, final)
            data = json.loads(raw)
            data = data.get('data', data) if isinstance(data, dict) else {}
            address = data.get('url') if isinstance(data, dict) else None
            if isinstance(address, str) and address:
                media = fetcher.validate(http_url(address, resolved))
                if download.usable_media_url(media):
                    result['videos'].append({'url': media, 'media_urls': [media], 'aliases': [],
                                             'page_url': final, 'title': result['title'], 'description': result['description'],
                                             'duration': parse_duration(data.get('duration'))})
        except Stopped:
            raise
        except (ValueError, requests.RequestException):
            continue
    if result['lazy_players'] and not result['videos']:
        raise ValueError('公开播放器配置暂时不可用，可以继续重试此页面')
    if not listing and len(result['videos']) == 1:
        video = result['videos'][0]
        video['aliases'] = _unique(video.get('aliases', []) + [final, result.get('canonical') or final])
    return result, final


def _queue(db, crawl, url, kind, source='', title='', duration=None):
    url = http_url(url)
    key = identity(url)
    if type(duration) not in (int, float) or not 0 < duration < float('inf'):
        duration = None
    row = db.execute('SELECT * FROM crawl_pages WHERE crawl_id=? AND url_key=?', (crawl['id'], key)).fetchone()
    if duration is not None and kind == 'detail':
        # A listing card can enrich an already completed detail without another
        # fetch. A page with several distinct videos has no unambiguous duration.
        existing = db.execute('SELECT i.id FROM crawl_items i JOIN crawl_results r ON r.item_id=i.id '
                              'WHERE r.crawl_id=? AND i.owner=? AND (i.page_url=? OR i.url=? OR EXISTS '
                              '(SELECT 1 FROM crawl_aliases a WHERE a.owner=i.owner AND a.item_id=i.id AND a.url_key=?))',
                              (crawl['id'], crawl['owner'], url, url, key)).fetchall()
        if len(existing) == 1:
            db.execute('UPDATE crawl_items SET duration=? WHERE id=?', (duration, existing[0]['id']))
    if row:
        if duration is not None:
            db.execute('UPDATE crawl_pages SET duration=? WHERE id=?', (duration, row['id']))
        revisit = kind == 'list' and row['kind'] == 'list'
        unfinished_detail = row['kind'] == 'detail' and row['state'] in ('queued', 'running', 'failed')
        if row['generation'] < crawl['generation'] and (revisit or unfinished_detail):
            db.execute("UPDATE crawl_pages SET state='queued',error='',failures=0,retry_at=NULL,generation=? WHERE id=?", (crawl['generation'], row['id']))
        return False
    db.execute('INSERT INTO crawl_pages(crawl_id,url_key,url,kind,source,title,generation,duration) VALUES(?,?,?,?,?,?,?,?)',
               (crawl['id'], key, url, kind, source, title[:300], crawl['generation'], duration))
    return True


def record_video(cid, video, source, fetcher=None, generation=None):
    url = http_url(video['url'])
    page_url = http_url(video.get('page_url') or source)
    media = _unique([http_url(u, page_url) for u in video.get('media_urls', [])], 30)
    urls = _unique([url] + video.get('aliases', []) + media, 50)
    if fetcher:
        for address in urls + [page_url]:
            fetcher.validate(address)
    keys = _unique([identity(u, media=u in media or bool(MEDIA_EXT.search(u))) for u in urls], 50)
    now = time.time()
    duration = video.get('duration')
    if type(duration) not in (int, float) or not 0 < duration < float('inf'):
        duration = None
    with transaction() as db:
        crawl = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        if not crawl or crawl['status'] != 'running' or (generation is not None and crawl['generation'] != generation):
            raise Stopped()
        matches = db.execute('SELECT DISTINCT item_id FROM crawl_aliases WHERE owner=? AND url_key IN '
                             '(SELECT value FROM json_each(?)) ORDER BY item_id', (crawl['owner'], json.dumps(keys))).fetchall()
        sources = _unique([source, page_url])
        if matches:
            iid = matches[0]['item_id']
            for item in matches[1:]:
                _merge(db, iid, item['item_id'])
            old = db.execute('SELECT * FROM crawl_items WHERE id=?', (iid,)).fetchone()
            sources = _unique(json.loads(old['sources']) + sources)
            # Fresh signed URLs precede older addresses; keep equivalent alternatives as fallbacks.
            media = _media_unique(media + json.loads(old['media_urls']))
            description = max((old['description'], str(video.get('description') or '')[:1600]), key=len)
            db.execute("UPDATE crawl_items SET url=?,media_urls=?,sources=?,description=?,duration=COALESCE(?,duration),"
                       "title=CASE WHEN COALESCE(trim(title),'')='' THEN ? ELSE title END,updated=? WHERE id=?",
                       (url, json.dumps(media), json.dumps(sources), description, duration,
                        str(video.get('title') or '')[:300], now, iid))
            db.execute('UPDATE crawls SET merged=merged+1 WHERE id=?', (cid,))
        else:
            iid = db.execute('INSERT INTO crawl_items(owner,url,page_url,title,description,media_urls,sources,created,updated,duration) '
                             'VALUES(?,?,?,?,?,?,?,?,?,?)', (crawl['owner'], url, page_url, str(video.get('title') or '')[:300],
                             str(video.get('description') or '')[:1600], json.dumps(media), json.dumps(sources), now, now, duration)).lastrowid
        for key in keys:
            db.execute('INSERT OR IGNORE INTO crawl_aliases(owner,url_key,item_id) VALUES(?,?,?)', (crawl['owner'], key, iid))
        db.execute('INSERT OR IGNORE INTO crawl_results(crawl_id,item_id) VALUES(?,?)', (cid, iid))
        db.execute('UPDATE crawls SET updated=? WHERE id=?', (now, cid))
        from . import sources as source_center
        source_center.record_crawl(db, cid, iid)
    return iid


def crawl_view(row):
    if has_request_context() and (request.path == '/api/crawls' or request.path.startswith('/api/crawls/')):
        if not source_visible(row, web.revealed()):
            return {'id': row['id'], 'privacy_locked': True}
        _remember_root(row)
    value = dict(row)
    counts = q('SELECT kind,state,COUNT(*) n FROM crawl_pages WHERE crawl_id=? AND generation=? GROUP BY kind,state',
               (row['id'], row['generation']))
    value.update(pages_done=sum(r['n'] for r in counts if r['kind'] == 'list' and r['state'] in ('done', 'failed')),
                 pages_pending=sum(r['n'] for r in counts if r['state'] in ('queued', 'running')),
                 details_done=sum(r['n'] for r in counts if r['kind'] == 'detail' and r['state'] == 'done'),
                 errors=sum(r['n'] for r in counts if r['state'] == 'failed'),
                 found=q('SELECT COUNT(*) n FROM crawl_results WHERE crawl_id=?', (row['id'],), one=True)['n'])
    value['limit_reason'] = ('pages' if value['pages_done'] >= row['max_pages'] else 'previous_results') if row['status'] == 'limited' else ''
    value.pop('owner', None)
    value.pop('url_key', None)
    return value


def _transient_failure(exc):
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status = response.status_code if response is not None else None
        return f'HTTP {status}' if status == 429 or status is not None and 500 <= status <= 599 else ''
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return type(exc).__name__
    if isinstance(exc, ValueError) and str(exc).startswith('公开播放器配置暂时不可用'):
        return 'PlayerUnavailable'
    return ''


def _page_failed(cid, generation, page, exc):
    """Persist retries before waiting, and report one terminal incident per page."""
    error, transient = download.failure_detail(exc), _transient_failure(exc)
    trace = traceback.format_exc()
    with transaction() as db:
        current = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        saved = db.execute('SELECT * FROM crawl_pages WHERE id=? AND crawl_id=? AND generation=?',
                           (page['id'], cid, generation)).fetchone()
        if not current or current['status'] != 'running' or current['generation'] != generation or not saved:
            raise Stopped()
        failures = saved['failures'] + 1
        retry = bool(transient) and failures <= len(RETRY_DELAYS)
        due = time.time() + RETRY_DELAYS[failures - 1] if retry else None
        db.execute('UPDATE crawl_pages SET state=?,failures=?,retry_at=?,error=? WHERE id=? AND generation=?',
                   ('queued' if retry else 'failed', failures, due, error, page['id'], generation))
        db.execute('UPDATE crawls SET error=?,updated=? WHERE id=? AND generation=?', (error, time.time(), cid, generation))
        report = bool(transient) and not retry and not saved['heal_reported']
        context = {'owner': current['owner'], 'url_key': current['url_key'], 'root_url': current['url'],
                   'crawl_id': cid, 'page_id': page['id'], 'url': page['url'], 'attempts': failures,
                   'generation': generation}
    if report:
        from . import heal
        # Resource summaries are visible in the UI; keep private URLs and titles
        # in the protected diagnostic context/trace rather than the incident label.
        summary = f'{transient}: 连续失败 {failures} 次，短期自动重试已用尽'
        heal.report('crawl', '目录采集', summary, trace=trace, context=context)
        incident = q('SELECT id FROM incidents WHERE sig=?', (heal.signature('crawl', summary, trace, context),), one=True)
        if incident:
            q('UPDATE crawl_pages SET heal_reported=? WHERE id=? AND crawl_id=? AND generation=? AND heal_reported=0',
              (incident['id'], page['id'], cid, generation))
    ring('crawls')


def _wait_retry(cid, due):
    mark = core.bell_mark('crawls')
    _running(cid)
    # A stop wakes this immediately; the short cap also checks edits/deletion
    # even when a cross-process notification is unavailable.
    core.bell_wait('crawls', mark, min(.5, max(.01, due - time.time())))


def run_crawl(cid):
    from .directory_retired import enabled
    if not enabled():
        return
    # Claim once under the same DB lock as stop/edit/delete. A second scheduler
    # cannot start the same task while the first request is still in flight.
    with transaction() as db:
        claimed = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        if not claimed or claimed['status'] != 'queued':
            return
        generation = claimed['generation']
        # A pre-upgrade task may have finished its root before source metadata
        # existed. Its remaining detail pages must not delay this known decision.
        if known_adult(claimed['url']):
            _source_state(db, claimed['owner'], claimed['url'], claimed['url_key'])
        db.execute("UPDATE crawls SET status='running',error='',updated=? WHERE id=?", (time.time(), cid))
    _run_context.claim = (cid, generation)
    fetcher = None
    try:
        fetcher = Fetcher(cid)
        while True:
            crawl = _running(cid)
            view = crawl_view(crawl)
            listing_allowed = view['pages_done'] < crawl['max_pages']
            ready_at = time.time()
            page = q("SELECT * FROM crawl_pages WHERE crawl_id=? AND state='queued' AND generation=? "
                     "AND (retry_at IS NULL OR retry_at<=?) AND (kind='detail' OR ?) ORDER BY kind='detail' DESC,id LIMIT 1",
                     (cid, crawl['generation'], ready_at, listing_allowed), one=True)
            if not page:
                delayed = q("SELECT MIN(retry_at) due FROM crawl_pages WHERE crawl_id=? AND generation=? "
                            "AND state='queued' AND (kind='detail' OR ?) AND retry_at>?",
                            (cid, generation, listing_allowed, ready_at), one=True)
                if delayed and delayed['due'] is not None:
                    _wait_retry(cid, delayed['due'])
                    continue
                finish = 'limited' if view['pages_pending'] else ('failed' if view['errors'] and not view['found'] else 'done')
                break
            q("UPDATE crawl_pages SET state='running' WHERE id=? AND generation=?", (page['id'], generation))
            q("UPDATE crawls SET current_url=?,updated=? WHERE id=? AND generation=? AND status='running'",
              (page['url'], time.time(), cid, generation))
            try:
                result, final = _extract(fetcher, page['url'], page['kind'] == 'list', page['source'])
                fetcher.check()
                if page['url_key'] == crawl['url_key']:
                    _classify_root(cid, result, generation)
                    fetcher.check()
                if page['kind'] == 'list':
                    signature = sorted(identity(v['url'], bool(v.get('media_urls'))) for v in result['videos'])
                    signature += sorted(identity(v['url']) for v in result['links'])
                    fingerprint = hashlib.sha256(json.dumps(signature).encode()).hexdigest() if signature else ''
                    repeated = fingerprint and q('SELECT 1 FROM crawl_pages WHERE crawl_id=? AND generation=? AND '
                                                 "kind='list' AND state='done' AND fingerprint=? LIMIT 1",
                                                 (cid, crawl['generation'], fingerprint), one=True)
                else:
                    fingerprint, repeated = '', False
                for video in result['videos']:
                    if not video.get('title'):
                        video['title'] = page['title'] or result['title']
                    if not video.get('duration') and page['kind'] == 'detail' and len(result['videos']) == 1:
                        video['duration'] = page['duration']
                    try:
                        record_video(cid, video, final, fetcher, generation)
                    except ValueError:
                        continue
                if not repeated and page['kind'] == 'list':
                    with transaction() as db:
                        current = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
                        if not current or current['status'] != 'running' or current['generation'] != generation:
                            raise Stopped()
                        for candidate in result['links'][:MAX_PAGE_LINKS]:
                            _queue(db, current, candidate['url'], 'detail', final, candidate.get('title', ''), candidate.get('duration'))
                        for address in result['pages'][:500]:
                            _queue(db, current, address, 'list', final)
                q("UPDATE crawl_pages SET state='done',fingerprint=?,error='',failures=0,retry_at=NULL,heal_reported=0 WHERE id=? AND generation=?", (fingerprint, page['id'], generation))
                if not crawl['title'] and result['title']:
                    q('UPDATE crawls SET title=? WHERE id=? AND generation=?', (result['title'][:300], cid, generation))
            except Stopped:
                q("UPDATE crawl_pages SET state='queued' WHERE id=? AND generation=?", (page['id'], generation))
                raise
            except Exception as exc:
                # Retry eligible failures without blocking other ready pages.
                _page_failed(cid, generation, page, exc)
            time.sleep(.3)
        now = time.time()
        notice = ('达到本次目录页数上限，可提高页数后继续' if finish == 'limited' else
                  '部分页面未成功，可点击继续重试' if view['errors'] else
                  '没有识别到视频或可访问的详情页' if not view['found'] else '')
        with transaction() as db:
            current = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
            if not current or current['status'] != 'running' or current['generation'] != generation:
                raise Stopped()
            # A verified repair can queue a failed page after the worker's last
            # empty-queue read. Commit completion under the callback's lock so
            # that work is retained without reviving stopped or edited tasks.
            listed = db.execute("SELECT COUNT(*) n FROM crawl_pages WHERE crawl_id=? AND generation=? "
                                "AND kind='list' AND state IN ('done','failed')", (cid, generation)).fetchone()['n']
            pending = db.execute("SELECT 1 FROM crawl_pages WHERE crawl_id=? AND generation=? AND state='queued' "
                                 "AND (kind='detail' OR ?) LIMIT 1", (cid, generation, listed < current['max_pages'])).fetchone()
            if pending:
                db.execute("UPDATE crawls SET status='queued',current_url='',error='',next_refresh=NULL,updated=? WHERE id=?",
                           (now, cid))
            else:
                db.execute("UPDATE crawls SET status=?,current_url='',error=?,updated=?,last_refresh=?,"
                           "next_refresh=CASE WHEN refresh_hours>0 AND ?!='limited' THEN ?+refresh_hours*3600 ELSE NULL END "
                           'WHERE id=?', (finish, notice, now, now, finish, now, cid))
    except Stopped:
        pass
    except Exception as exc:
        q("UPDATE crawls SET status='failed',error=?,updated=?,current_url='' WHERE id=? AND generation=? AND status='running'",
          (download.failure_detail(exc), time.time(), cid, generation))
        q("UPDATE crawl_pages SET state='queued' WHERE crawl_id=? AND generation=? AND state='running'", (cid, generation))
        traceback.print_exc()
    finally:
        if fetcher is not None:
            fetcher.close()
        _run_context.claim = None


def refresh_crawl(cid, owner=None):
    with transaction() as db:
        row = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        if not row or (owner and row['owner'] != owner):
            raise ValueError('找不到这个采集任务')
        if has_request_context() and owner is not None:
            _access_guard(row)
        if row['status'] in ('queued', 'running'):
            return
        from . import sources as source_center
        source_center.crawl_baseline(db, row)
        now = time.time()
        db.execute("UPDATE crawls SET status='queued',generation=generation+1,merged=0,error='',next_refresh=NULL,updated=? WHERE id=?", (now, cid))
        row = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
        _queue(db, row, row['url'], 'list')
        # Keep failed directories even if a changing root no longer links them.
        # They remain subject to the same page budget; limited tasks retain the
        # pending pages for an explicit continuation. Successful details stay done.
        db.execute("UPDATE crawl_pages SET state='queued',failures=0,retry_at=NULL,generation=? WHERE crawl_id=? AND state='failed'", (row['generation'], cid))
        if has_request_context() and owner is not None:
            _access_guard()
    ring('crawls')


def worker_loop(stop_event=None):
    from .directory_retired import enabled
    if not enabled():
        return
    locks = core.STATE / 'locks'
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / 'crawl-worker.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as db:
            _sync_known_sources(db)
            db.execute("UPDATE crawls SET status='queued' WHERE status='running'")
            db.execute("UPDATE crawl_pages SET state='queued' WHERE state='running'")
        # Keep the process lock until all requests have exited, including during
        # shutdown. Futures also prevent a stopped/resumed task from overlapping
        # its old request; generation checks reject any late result.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='crawl') as pool:
            active = {}
            while stop_event is None or not stop_event.is_set():
                mark = core.bell_mark('crawls')
                try:
                    for cid, future in list(active.items()):
                        if future.done():
                            del active[cid]
                            future.result()
                    for row in q("SELECT id FROM crawls WHERE refresh_hours>0 AND next_refresh<=? AND status IN ('done','failed')", (time.time(),)):
                        refresh_crawl(row['id'])
                    for row in q("SELECT id FROM crawls WHERE status='queued' ORDER BY updated,id"):
                        if len(active) >= 2:
                            break
                        if row['id'] not in active:
                            future = pool.submit(run_crawl, row['id'])
                            active[row['id']] = future
                            future.add_done_callback(lambda _: ring('crawls'))
                    # Existing collected libraries need their one root decision
                    # without downloading or reclassifying hundreds of videos.
                    for row in q("SELECT id,title FROM crawls WHERE source_checked IS NULL AND status NOT IN ('queued','running') ORDER BY id"):
                        if len(active) >= 2:
                            break
                        if row['id'] not in active:
                            future = pool.submit(_classify_root, row['id'], {'title': row['title']})
                            active[row['id']] = future
                            future.add_done_callback(lambda _: ring('crawls'))
                except Exception:
                    traceback.print_exc()
                core.bell_wait('crawls', mark, .1 if stop_event is not None else 30)


def _visible(cid):
    return q('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner), one=True)


def _integer(value, default, low, high):
    result = core.number(value, default, low=low, high=high)
    if int(result) != result:
        raise ValueError('请填写整数')
    return int(result)


def _hidden_collected_job_ids(owner):
    """Apply the library's privacy rules to active and archived imports."""
    if web.revealed():
        return set()
    prefs = web.privacy_get(owner)
    hidden = web.hidden_ids() | set(prefs['ids'])
    for row in q('SELECT job_id,snapshot FROM job_trash WHERE owner=? AND job_id IS NOT NULL', (owner,)):
        snapshot = json.loads(row['snapshot'])
        snapshot['id'] = row['job_id']
        if isinstance(snapshot.get('analysis'), str):
            snapshot['analysis'] = json.loads(snapshot['analysis'] or '{}')
        if snapshot.get('_trash_private') is True or web.is_hidden(snapshot, prefs):
            hidden.add(row['job_id'])
    return hidden


def _item_scope(cid):
    source = _visible(cid)
    if not source or not source_visible(source, web.revealed()):
        return None
    hidden = _hidden_collected_job_ids(g.owner)
    return ('r.crawl_id=? AND i.owner=? AND (i.job_id IS NULL OR i.job_id NOT IN '
            '(SELECT value FROM json_each(?)))', (cid, g.owner, json.dumps(sorted(hidden))))


def _items(cid, *, offset=None, limit=None, ids=None, after_id=None):
    from .library_search import collected_durations
    scope = _item_scope(cid)
    if not scope:
        return []
    where, args = scope
    if ids is not None:
        where += ' AND i.id IN (SELECT value FROM json_each(?))'
        args += (json.dumps(list(ids)),)
    if after_id is not None:
        where += ' AND i.id>?'
        args += (after_id,)
    sql = 'SELECT i.* FROM crawl_items i JOIN crawl_results r ON r.item_id=i.id WHERE ' + where + ' ORDER BY i.id'
    if limit is not None:
        sql += ' LIMIT ? OFFSET ?'
        args += (limit, offset or 0)
    items = []
    for row in q(sql, args):
        item = dict(row)
        item.pop('owner', None)
        item['sources'] = json.loads(item['sources'])
        item['media_urls'] = json.loads(item['media_urls'])
        items.append(item)
    return collected_durations(items, g.owner)


def _item_count(cid):
    scope = _item_scope(cid)
    if not scope:
        return 0
    where, args = scope
    return q('SELECT COUNT(*) n FROM crawl_items i JOIN crawl_results r ON r.item_id=i.id WHERE ' + where,
             args, one=True)['n']


def _search(items, query='', mode='exact'):
    # Browsing must remain available even when a very large provenance list
    # exceeds the bounded search input. Search itself still enforces its budget.
    if not query and mode in ('exact', 'wildcard', 'regex', 'ai'):
        return {'items': items, 'interpretation': ''}
    return search_items(items, query, mode, g.owner)


def _home_crawl_items(owner):
    """One result per owned item, with an owned collection that can import it."""
    visible = visible_source_ids(owner, reveal=web.revealed())
    if not visible:
        return []
    hidden = _hidden_collected_job_ids(owner)
    rows = q('SELECT i.*,MAX(c.id) AS crawl_id FROM crawl_items i '
             'JOIN crawl_results r ON r.item_id=i.id JOIN crawls c ON c.id=r.crawl_id '
             'WHERE i.owner=? AND c.owner=? AND c.id IN (SELECT value FROM json_each(?)) '
             'GROUP BY i.id ORDER BY i.updated DESC,i.id DESC', (owner, owner, json.dumps(sorted(visible))))
    items = []
    for row in rows:
        if row['job_id'] in hidden:
            continue
        item = dict(row)
        item.pop('owner', None)
        item['sources'] = json.loads(item['sources'])
        item['media_urls'] = json.loads(item['media_urls'])
        items.append(item)
    from .library_search import collected_durations
    return public_provenance(collected_durations(items, owner), owner)


@app.get('/api/crawls/search')
def api_crawl_home_search():
    from . import library_search
    try:
        if request.headers.get('X-Privacy-Token') and not web.privacy_token():
            return jsonify(error='隐私权限已失效，请重新解锁'), 403
        reveal = web.revealed()
        access = library_search.access_stamp(g.owner)
        visible = visible_source_ids(g.owner, reveal=reveal)
        offset = _integer(request.args.get('offset'), 0, 0, 1_000_000)
        limit = _integer(request.args.get('limit'), 20, 1, 500)
        query, mode = request.args.get('q', ''), request.args.get('mode', 'exact')
        hidden = _hidden_collected_job_ids(g.owner) if visible else set()
        if not visible:
            result, total = _search([], query, mode), 0
        elif mode in ('exact', 'wildcard'):
            result, total = library_search.collected_page(query, mode, g.owner, visible, hidden, limit, offset, reveal=reveal)
        else:
            result = _search(_home_crawl_items(g.owner), query, mode)
            total = len(result['items'])
            result['items'] = result['items'][offset:offset + limit]
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    response = jsonify(**result, total=total, offset=offset, limit=limit, storage_owner=g.owner)
    if (web.revealed() != reveal or request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        return jsonify(error='隐私权限已变化，请重新搜索'), 403
    for key in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(key, None)
    if (visible_source_ids(g.owner, reveal=reveal) != visible or (visible and _hidden_collected_job_ids(g.owner) != hidden) or
            library_search.access_stamp(g.owner) != access or web.revealed() != reveal or
            request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        return jsonify(error='来源或隐藏内容设置已变化，请重新搜索'), 403
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/crawls', methods=['GET', 'POST'])
def api_crawls():
    if request.method == 'GET':
        visibility = '' if web.revealed() else " AND source_classification='general'"
        return jsonify(crawls=[crawl_view(r) for r in q('SELECT * FROM crawls WHERE owner=?' + visibility + ' ORDER BY id DESC LIMIT 30', (g.owner,))])
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error='请输入目录网址'), 400
    bulk = 'urls' in body
    urls = body.get('urls') if bulk else [body.get('url')]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
        return jsonify(error='一次请输入 1–20 个目录网址'), 400
    try:
        limit = _integer(body.get('max_pages'), 50, 1, 500)
        interval = core.number(body.get('refresh_hours'), 6, low=0, high=720)
        if 0 < interval < 1:
            raise ValueError('刷新间隔至少为 1 小时')
    except (ValueError, TypeError):
        return jsonify(error='请填写有效的公网网址、1–500 页和至少 1 小时的刷新间隔（0 为关闭）'), 400
    created, errors = [], []
    for value in urls:
        try:
            url = http_url(value.strip() if isinstance(value, str) else value)
            if bad := web.internal_link(url):
                raise ValueError(bad)
            with transaction() as db:
                _access_guard()
                row = db.execute("SELECT * FROM crawls WHERE owner=? AND url_key=? AND status IN ('queued','running')", (g.owner, identity(url))).fetchone()
                if not row:
                    if db.execute("SELECT COUNT(*) FROM crawls WHERE owner=? AND status IN ('queued','running')", (g.owner,)).fetchone()[0] >= 50:
                        raise ValueError('已有 50 个采集任务在执行，请等待完成或停止后再添加')
                    now = time.time()
                    cid = db.execute('INSERT INTO crawls(owner,url,url_key,max_pages,refresh_hours,created,updated) VALUES(?,?,?,?,?,?,?)',
                                     (g.owner, url, identity(url), limit, interval, now, now)).lastrowid
                    row = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
                    _queue(db, row, url, 'list')
                _source_state(db, g.owner, url, identity(url))
                row = db.execute('SELECT * FROM crawls WHERE id=?', (row['id'],)).fetchone()
                _access_guard()
            if not any(task['id'] == row['id'] for task in created):
                created.append(crawl_view(row))
        except PermissionError as exc:
            return jsonify(error=str(exc)), 403
        except (ValueError, TypeError) as exc:
            if not bulk:
                return jsonify(error=str(exc)), 400
            errors.append({'url': value if isinstance(value, str) else '', 'error': str(exc)})
    ring('crawls')
    return jsonify(crawls=created, errors=errors) if bulk else jsonify(crawl=created[0])


@app.get('/api/crawls/<int:cid>')
def api_crawl(cid):
    from . import library_search
    row = _visible(cid)
    if not row:
        return jsonify(error='找不到这个采集任务'), 404
    if not source_visible(row, web.revealed()):
        return jsonify(crawl={'id': cid, 'privacy_locked': True}, privacy_locked=True, items=[], total=0, interpretation='')
    try:
        if request.headers.get('X-Privacy-Token') and not web.privacy_token():
            return jsonify(error='隐私权限已失效，请重新解锁'), 403
        reveal = web.revealed()
        access = library_search.access_stamp(g.owner)
        visible = {cid} if source_visible(row, reveal) else set()
        hidden = _hidden_collected_job_ids(g.owner) if visible else set()
        offset = _integer(request.args.get('offset'), 0, 0, 1_000_000_000)
        limit = _integer(request.args.get('limit'), 100, 1, 500)
        query, mode = request.args.get('q', ''), request.args.get('mode', 'exact')
        if mode in ('exact', 'wildcard'):
            result, total = library_search.collected_page(query, mode, g.owner, visible, hidden, limit, offset, directory=True, reveal=reveal)
            result['total'] = total
            if not query:
                result['interpretation'] = ''
        elif not query and mode in ('regex', 'ai'):
            result, total = library_search.collected_page('', 'exact', g.owner, visible, hidden, limit, offset, directory=True, reveal=reveal)
            result.update(total=total, interpretation='')
        else:
            result = _search(public_provenance(_items(cid), g.owner), query, mode)
            result['total'] = len(result['items'])
            result['items'] = result['items'][offset:offset + limit]
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    response = jsonify(crawl=crawl_view(row), **result)
    for key in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(key, None)
    fresh = _visible(cid)
    if (not fresh or fresh['url_key'] != row['url_key'] or source_visible(fresh, web.revealed()) != bool(visible)
            or web.revealed() != reveal or library_search.access_stamp(g.owner) != access
            or (visible and _hidden_collected_job_ids(g.owner) != hidden)
            or request.headers.get('X-Privacy-Token') and not web.privacy_token()):
        return jsonify(error='来源或隐私权限已变化，请重新加载'), 403
    response.headers['Cache-Control'] = 'no-store'
    return response


def _issue_scope(row):
    """Scope diagnostic pages before counting, listing or retrying them."""
    hidden = _hidden_collected_job_ids(g.owner)
    keys, item_ids = set(), []
    if hidden:
        for item in q('SELECT i.id,i.url,i.page_url,i.sources FROM crawl_items i '
                      'JOIN crawl_results r ON r.item_id=i.id WHERE r.crawl_id=? AND i.owner=? '
                      'AND i.job_id IN (SELECT value FROM json_each(?))',
                      (row['id'], g.owner, json.dumps(sorted(hidden)))):
            item_ids.append(item['id'])
            addresses = [item['url'], item['page_url'], *json.loads(item['sources'])]
            for address in addresses:
                if isinstance(address, str) and address:
                    keys.add(address)
                    try:
                        keys.add(identity(address))
                    except ValueError:
                        pass
        if item_ids:
            keys.update(alias['url_key'] for alias in q('SELECT url_key FROM crawl_aliases WHERE owner=? '
                        'AND item_id IN (SELECT value FROM json_each(?))', (g.owner, json.dumps(item_ids))))
    encoded = json.dumps(sorted(keys))
    return ('p.crawl_id=? AND p.generation=? AND p.url NOT IN (SELECT value FROM json_each(?)) '
            'AND p.url_key NOT IN (SELECT value FROM json_each(?))',
            (row['id'], row['generation'], encoded, encoded))


@app.get('/api/crawls/<int:cid>/issues')
def api_crawl_issues(cid):
    row = _visible(cid)
    if not row:
        return jsonify(error='找不到这个采集任务'), 404
    try:
        offset = _integer(request.args.get('offset'), 0, 0, 1_000_000_000)
        limit = _integer(request.args.get('limit'), 20, 1, 100)
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    health = {'completed': 0, 'failed': 0, 'retrying': 0, 'pending': 0, 'running': 0, 'failure_rate': None}
    revealed = web.revealed()
    if not source_visible(row, revealed):
        return jsonify(generation=None, items=[], total=0, failed=0, retrying=0,
                       health=None, can_retry=False, privacy_locked=True,
                       notice='解锁隐私菜单并显示隐藏内容后，可查看问题页面')
    _remember_root(row)
    scope, args = _issue_scope(row)
    for count in q('SELECT p.state,p.failures>0 retried,COUNT(*) n FROM crawl_pages p WHERE ' + scope +
                   ' GROUP BY p.state,p.failures>0', args):
        state, n = count['state'], count['n']
        if state == 'done':
            health['completed'] += n
        elif state == 'failed':
            health['failed'] += n
        elif state in ('queued', 'running'):
            if count['retried']:
                health['retrying'] += n
            if state == 'running':
                health['running'] += n
            elif not count['retried']:
                health['pending'] += n
    ended = health['completed'] + health['failed']
    health['failure_rate'] = health['failed'] / ended if ended else None
    issues = q('SELECT p.id,p.url,p.kind,p.state,p.failures,p.retry_at,p.error,'
               "CASE WHEN i.state='healing' AND t.state='failed' THEN 'error' ELSE i.state END heal_state "
               'FROM crawl_pages p LEFT JOIN incidents i ON i.id=p.heal_reported AND i.kind=\'crawl\' '
               'AND CASE WHEN json_valid(i.context) THEN json_extract(i.context,\'$.owner\') END=? '
               'AND CASE WHEN json_valid(i.context) THEN json_extract(i.context,\'$.crawl_id\') END=? '
               'AND CASE WHEN json_valid(i.context) THEN json_extract(i.context,\'$.url_key\') END=? '
               "LEFT JOIN tasks t ON t.kind='heal' AND t.target='incident:'||i.id "
               'WHERE ' + scope + " AND (p.state='failed' OR (p.state IN ('queued','running') AND p.failures>0)) "
               "ORDER BY p.state!='failed',p.id LIMIT ? OFFSET ?",
               (g.owner, cid, row['url_key'], *args, limit, offset))
    items = []
    for issue in issues:
        item = dict(issue)
        state = item.pop('heal_state')
        item['heal'] = {'state': state} if state else None
        items.append(item)
    if revealed and not web.revealed():
        return jsonify(error='隐私权限已变化，请刷新问题页面'), 403
    return jsonify(generation=row['generation'], items=items, total=health['failed'] + health['retrying'],
                   failed=health['failed'], retrying=health['retrying'], health=health,
                   can_retry=bool(health['failed'] and row['status'] != 'stopped'), privacy_locked=False,
                   notice='任务已停止；先继续采集，再单独重试失败页面' if row['status'] == 'stopped' else '')


@app.post('/api/crawls/<int:cid>/retry-failed')
def api_crawl_retry_failed(cid):
    row = _visible(cid)
    if not row:
        return jsonify(error='找不到这个采集任务'), 404
    revealed = web.revealed()
    if not source_visible(row, revealed):
        return jsonify(error='请先解锁隐私菜单并显示隐藏来源'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get('generation')) is not int:
        return jsonify(error='请刷新问题页面后重试'), 400
    ids = body.get('ids')
    if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 1000 or
                            any(type(ident) is not int or ident < 1 for ident in ids)):
        return jsonify(error='请选择 1–1000 个失败页面'), 400
    # Privacy helpers query the shared connection; resolve them before opening
    # the mutation transaction, then fence owner/generation/state atomically.
    scope, args = _issue_scope(row)
    if ids is not None:
        scope += ' AND p.id IN (SELECT value FROM json_each(?))'
        args += (json.dumps(sorted(set(ids))),)
    with transaction() as db:
        current = db.execute('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner)).fetchone()
        if not current:
            return jsonify(error='找不到这个采集任务'), 404
        if current['generation'] != body['generation'] or current['generation'] != row['generation']:
            return jsonify(error='采集任务已变化，请刷新问题页面后重试'), 409
        if revealed and not web.revealed():
            return jsonify(error='隐私权限已变化，请刷新问题页面'), 403
        if not source_visible(current, web.revealed()):
            return jsonify(error='请先解锁隐私菜单并显示隐藏来源'), 403
        if current['status'] == 'stopped':
            return jsonify(error='任务已停止，请先继续采集'), 409
        failed = db.execute("SELECT p.id FROM crawl_pages p WHERE " + scope + " AND p.state='failed'", args).fetchall()
        if ids is not None and len(failed) != len(set(ids)):
            return jsonify(error='部分页面已恢复或不可见，请刷新后重试'), 400
        requeued = len(failed)
        if requeued:
            db.execute("UPDATE crawl_pages SET state='queued',error='',failures=0,retry_at=NULL,heal_reported=0 "
                       'WHERE id IN (SELECT value FROM json_each(?))', (json.dumps([page['id'] for page in failed]),))
            if current['status'] not in ('queued', 'running'):
                db.execute("UPDATE crawls SET status='queued',error='',current_url='',next_refresh=NULL,updated=? WHERE id=?",
                           (time.time(), cid))
    if requeued:
        ring('crawls')
    current = _visible(cid)
    return jsonify(crawl=crawl_view(current), requeued=requeued, generation=current['generation']) if current else (
        jsonify(error='找不到这个采集任务'), 404)


@app.post('/api/crawls/<int:cid>/<action>')
def api_crawl_action(cid, action):
    row = _visible(cid)
    if not row:
        return jsonify(error='找不到这个采集任务'), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error='请求格式无效'), 400
    try:
        if action == 'stop':
            with transaction() as db:
                current = db.execute('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner)).fetchone()
                if not current:
                    return jsonify(error='找不到这个采集任务'), 404
                _access_guard(current)
                from . import sources as source_center
                source_center.crawl_baseline(db, current, stopping=True)
                db.execute("UPDATE crawls SET status='stopped',generation=generation+1,refresh_hours=0,"
                           "next_refresh=NULL,current_url='',updated=? WHERE id=?", (time.time(), cid))
                db.execute("UPDATE crawl_pages SET generation=generation+1,state=CASE WHEN state='running' THEN 'queued' ELSE state END "
                           'WHERE crawl_id=? AND generation=?', (cid, current['generation']))
                _access_guard()
        elif action == 'delete':
            with transaction() as db:
                current = db.execute('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner)).fetchone()
                if not current:
                    return jsonify(error='找不到这个采集任务'), 404
                _access_guard(current)
                db.execute('DELETE FROM crawl_results WHERE crawl_id=?', (cid,))
                db.execute('DELETE FROM crawl_pages WHERE crawl_id=?', (cid,))
                db.execute('DELETE FROM crawls WHERE id=?', (cid,))
                _access_guard()
            ring('crawls')
            return jsonify(ok=True, id=cid)
        elif action == 'settings':
            interval = core.number(body.get('refresh_hours'), row['refresh_hours'], low=0, high=720)
            if 0 < interval < 1:
                raise ValueError('刷新间隔至少为 1 小时')
            full_edit = 'url' in body or 'max_pages' in body
            url = http_url(body['url'].strip() if isinstance(body.get('url'), str) else body['url']) if 'url' in body else row['url']
            if 'url' in body and (bad := web.internal_link(url)):
                raise ValueError(bad)
            limit = _integer(body.get('max_pages'), row['max_pages'], 1, 500)
            with transaction() as db:
                current = db.execute('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner)).fetchone()
                if not current:
                    return jsonify(error='找不到这个采集任务'), 404
                _access_guard(current)
                if full_edit and current['status'] in ('queued', 'running'):
                    return jsonify(error='请先停止采集任务，再编辑网址或页数'), 409
                # A frequency-only save must not revert a URL/page edit that
                # committed between the initial read and this transaction.
                if 'url' not in body:
                    url = current['url']
                if 'max_pages' not in body:
                    limit = current['max_pages']
                if 'refresh_hours' not in body:
                    interval = current['refresh_hours']
                now = time.time()
                changed = url != current['url']
                db.execute('UPDATE crawls SET url=?,url_key=?,max_pages=?,refresh_hours=?,next_refresh=?,updated=? WHERE id=?',
                           (url, identity(url), limit, interval,
                            now + interval * 3600 if interval and current['status'] in ('done', 'failed') else None, now, cid))
                if changed:
                    db.execute('DELETE FROM crawl_results WHERE crawl_id=?', (cid,))
                    db.execute('DELETE FROM crawl_pages WHERE crawl_id=?', (cid,))
                    db.execute("UPDATE crawls SET status='queued',generation=generation+1,title='',merged=0,error='',"
                               "current_url='',last_refresh=NULL,next_refresh=NULL WHERE id=?", (cid,))
                    current = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
                    _queue(db, current, url, 'list')
                    _source_state(db, g.owner, url, identity(url))
                _access_guard()
        elif action == 'refresh':
            refresh_crawl(cid, g.owner)
        elif action == 'resume':
            limit = _integer(body.get('max_pages'), row['max_pages'], 1, 500)
            if row['status'] in ('queued', 'running'):
                return jsonify(crawl=crawl_view(row))
            if row['status'] == 'limited' and limit <= crawl_view(row)['pages_done']:
                raise ValueError('请提高页数上限后继续，最多 500 页')
            with transaction() as db:
                current = db.execute('SELECT * FROM crawls WHERE id=? AND owner=?', (cid, g.owner)).fetchone()
                if not current:
                    return jsonify(error='找不到这个采集任务'), 404
                _access_guard(current)
                if current['status'] not in ('queued', 'running'):
                    db.execute("UPDATE crawls SET status='queued',max_pages=?,error='',updated=? WHERE id=?", (limit, time.time(), cid))
                    db.execute("UPDATE crawl_pages SET state='queued',failures=0,retry_at=NULL WHERE crawl_id=? AND generation=? AND state='failed'", (cid, current['generation']))
                _access_guard()
        elif action == 'import':
            return _import_results(cid, body)
        else:
            return jsonify(error='未知操作'), 404
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    ring('crawls')
    row = _visible(cid)
    return jsonify(crawl=crawl_view(row)) if row else (jsonify(error='找不到这个采集任务'), 404)


def _import_results(cid, body, *, before_import=None):
    source = _visible(cid)
    if not source or not source_visible(source, web.revealed()):
        return jsonify(error='请先解锁隐私菜单并显示隐藏来源'), 403
    access_context = _request_access()
    def verify():
        if _request_access() != access_context:
            raise PermissionError('隐私权限已变化，请重新加载')
        fresh = _visible(cid)
        if not fresh or fresh['url_key'] != source['url_key'] or not source_visible(fresh, access_context[1]):
            raise PermissionError('来源或隐私权限已变化，请重新加载')
        if before_import is not None:
            before_import()
    if body.get('all') is True:
        if not body.get('q') and _item_count(cid) > MAX_IMPORT_ITEMS:
            raise ValueError(f'每次最多添加 {MAX_IMPORT_ITEMS} 个下载，请先搜索或分批勾选')
        result = _search(_items(cid), str(body.get('q') or ''), str(body.get('mode') or 'exact'))
        items = result['items']
        if len(items) > MAX_IMPORT_ITEMS:
            raise ValueError(f'每次最多添加 {MAX_IMPORT_ITEMS} 个下载，请先缩小搜索范围或分批勾选')
    else:
        ids = body.get('ids')
        if not isinstance(ids, list) or len(ids) > MAX_IMPORT_ITEMS or any(type(i) is not int for i in ids):
            raise ValueError('请选择要添加的视频')
        wanted = set(ids)
        items = _items(cid, ids=wanted)
        if len(items) != len(wanted):
            raise ValueError('部分采集结果已合并或不可见，请刷新后重试')
    counts = {'added': 0, 'duplicates': 0, 'linked': 0, 'ids': []}
    with _import_lock:
        for item in items:
            verify()
            existing = q("SELECT id FROM jobs WHERE id=? AND owner=? AND status NOT IN ('failed','cancelled')",
                         (item['job_id'], g.owner), one=True) if item['job_id'] else None
            if existing:
                verify()
                q('INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)',
                  (g.owner, existing['id'], source['url_key']))
                counts['duplicates'] += 1
                counts['ids'].append(existing['id'])
                continue
            keys = [r['url_key'] for r in q('SELECT url_key FROM crawl_aliases WHERE owner=? AND item_id=?', (g.owner, item['id']))]
            aliases = list(keys)
            aliases.extend(core.link_key(u) for u in [item['url'], *item['media_urls'], *keys]
                           if u.startswith(('https://', 'http://')))
            context = {'media_urls': item['media_urls'], 'referer': item['page_url'], 'title': item['title']}
            verify()
            jid, how = pipeline.add_job_ex(item['url'], source=f"crawl:{cid}", owner=g.owner, device=g.device_label,
                                           key_aliases=aliases, download_context=context if item['media_urls'] else None)
            q('UPDATE crawl_items SET job_id=? WHERE id=? AND owner=?', (jid, item['id'], g.owner))
            q('INSERT OR IGNORE INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)', (g.owner, jid, source['url_key']))
            counts[{'new': 'added', 'duplicate': 'duplicates', 'linked': 'linked'}[how]] += 1
            counts['ids'].append(jid)
    return jsonify(**counts)


@app.get('/api/crawls/<int:cid>/export')
def api_crawl_export(cid):
    source = _visible(cid)
    if not source:
        return jsonify(error='找不到这个采集任务'), 404
    _remember_root(source)
    query, mode = request.args.get('q', ''), request.args.get('mode', 'exact')
    try:
        result = None if not query and mode in ('exact', 'wildcard', 'regex', 'ai') else _search(public_provenance(_items(cid), g.owner), query, mode)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    @stream_with_context
    def addresses():
        seen = set()
        after_id = 0
        while True:
            try:
                fresh = _visible(cid)
                if fresh is None:
                    return
                _access_guard(fresh)
            except PermissionError:
                return
            items = result['items'] if result is not None else _items(cid, after_id=after_id, limit=500)
            for item in items:
                for url in item['media_urls'] or [item['url'] or item['page_url']]:
                    if url not in seen:
                        seen.add(url)
                        yield url + '\n'
            if result is not None or len(items) < 500:
                break
            after_id = items[-1]['id']
    return Response(addresses(), mimetype='text/plain',
                    headers={'Content-Disposition': f'attachment; filename="videos-{cid}.txt"', 'Cache-Control': 'no-store'})


def download_context(job_id):
    row = q('SELECT context FROM crawl_downloads WHERE job_id=?', (job_id,), one=True)
    if not row:
        return None
    context = json.loads(row['context'])
    fetcher = Fetcher()
    try:
        original = context.get('media_urls') or []
        keys = {identity(u, True) for u in original}
        for address in original + [context['referer']]:
            fetcher.validate(address)
        if context['referer'] not in original:
            try:
                result, final = _extract(fetcher, context['referer'], False)
                fresh = [u for video in result['videos'] for u in video.get('media_urls', []) if identity(u, True) in keys]
                if fresh:
                    context['media_urls'] = _unique(fresh + original, 30)
                    context['referer'] = final
            except (requests.RequestException, ValueError, ImportError):
                pass
        if fetcher.session.cookies:
            path = core.STATE / f'crawl-cookies-{job_id}.txt'
            download.write_cookie_file(path, [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
                                               'secure': c.secure, 'expires': c.expires} for c in fetcher.session.cookies])
            context['cookiefile'] = str(path)
        return context
    finally:
        fetcher.close()
