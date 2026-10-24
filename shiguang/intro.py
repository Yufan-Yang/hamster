"""Owned TV opening markers from explicit site marks or repeated local audio."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import array
import ctypes
import ctypes.util
import hashlib
import http.cookiejar
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

import requests

from flask import g, jsonify, request

from . import core, library, series, web
from .core import app, q

SCAN_SECONDS = 300
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='intro')
_lock = threading.Lock()
_pending = set()
_LABEL = re.compile(r'(?:intro|opening(?: credits)?|op(?:\s*\d+)?|片头(?:曲)?|开场曲|オープニング)', re.I)


def group_of(job):
    a = job['analysis']
    if a.get('library') != 'TV' or not isinstance(a.get('show'), str) or not a['show'].strip():
        return None
    season = a.get('season', 1)
    if season is None:
        season = 1
    if type(season) not in (int, float) or not math.isfinite(season) or not 0 <= season <= 99:
        return None
    return (unicodedata.normalize('NFKC', series.clean_name(a['show'])).casefold(), int(season))


def episode_of(job):
    value = job['analysis'].get('episode')
    return int(value) if type(value) in (int, float) and math.isfinite(value) and value == int(value) and 1 <= value <= 10000 else None


def file_key(path):
    p = Path(path).resolve(strict=True)
    stat = p.stat()
    if not p.is_file() or p.suffix.lower() not in core.VIDEO_EXT:
        raise ValueError('unsupported media')
    raw = json.dumps([str(p), stat.st_size, stat.st_mtime_ns, stat.st_ino])
    return hashlib.sha256(raw.encode()).hexdigest()


def interval(start, end, duration, minimum=10):
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end, duration)):
        return None
    if not (0 <= start <= 180 and minimum <= end - start <= 180 and end <= min(300, duration - 30)):
        return None
    return {'start': round(start, 3), 'end': round(end, 3)}


def chapter_intro(analysis, part, duration):
    # These are site marks, not generated summary chapter titles.
    marks = (analysis.get('markers') or {}).get(str(part), [])
    if not isinstance(marks, list):
        return None
    ordered = sorted((mark for mark in marks if isinstance(mark, dict)), key=lambda mark: mark.get('kind') != 'intro')
    for i, mark in enumerate(ordered):
        if not isinstance(mark, dict) or not _LABEL.fullmatch(str(mark.get('title', '')).strip()):
            continue
        end = mark.get('end')
        if end is None and i + 1 < len(ordered):
            end = ordered[i + 1].get('t')
        found = interval(mark.get('t'), end, duration)
        if found:
            return {**found, 'method': 'chapter', 'automatic': True}
    return None


@lru_cache(maxsize=1)
def fingerprint_timing():
    # Obtain timing from the same installed algorithm, rather than assuming
    # that a fingerprint item is a second. Public API: acoustid/chromaprint.h.
    name = ctypes.util.find_library('chromaprint')
    if not name:
        raise RuntimeError('fingerprinting unavailable')
    lib = ctypes.CDLL(name)
    lib.chromaprint_new.argtypes = [ctypes.c_int]
    lib.chromaprint_new.restype = ctypes.c_void_p
    lib.chromaprint_free.argtypes = [ctypes.c_void_p]
    lib.chromaprint_free.restype = None
    names = ('chromaprint_get_sample_rate', 'chromaprint_get_item_duration', 'chromaprint_get_delay')
    for key in names:
        fn = getattr(lib, key)
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_int
    ctx = lib.chromaprint_new(1)
    if not ctx:
        raise RuntimeError('fingerprinting unavailable')
    try:
        rate, step, delay = (getattr(lib, key)(ctx) for key in names)
        if rate <= 0 or not 0 < step / rate < 1 or not 0 < delay / rate < 10:
            raise RuntimeError('invalid fingerprint timing')
        return step / rate, delay / rate
    finally:
        lib.chromaprint_free(ctx)


@lru_cache(maxsize=8)
def fingerprint(path, signature):
    fingerprint_timing()
    if file_key(path) != signature:
        raise ValueError('media changed')
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                             '-protocol_whitelist', 'file,pipe', '-i', str(path), '-t', str(SCAN_SECONDS),
                             '-map', '0:a:0', '-ac', '1', '-ar', '11025', '-f', 'chromaprint',
                             '-algorithm', '1', '-fp_format', 'raw', 'pipe:1'],
                            capture_output=True, timeout=90)
    if result.returncode or len(result.stdout) > 20000 or len(result.stdout) % 4:
        raise RuntimeError('fingerprinting unavailable')
    words = array.array('I')
    if words.itemsize != 4:
        raise RuntimeError('fingerprinting unavailable')
    words.frombytes(result.stdout)
    if file_key(path) != signature:
        raise ValueError('media changed')
    return tuple(words)


def match_audio(first, second, step, delay, duration):
    """Find a varied, long repeated passage; constant audio and duplicates fail closed."""
    positions = defaultdict(list)
    for j, value in enumerate(second):
        positions[value >> 16].append(j)
    offsets = Counter()
    for i, value in enumerate(first):
        bucket = positions.get(value >> 16, ())
        if len(bucket) <= 24:
            offsets.update(i - j for j in bucket)
    candidates = []
    gap_limit = max(1, round(1 / step))
    for offset, votes in offsets.most_common(12):
        if votes < 12:
            break
        begin, last, gap = None, None, 0
        distances = []
        runs = []
        for i in range(max(0, offset), min(len(first), len(second) + offset)):
            distance = (first[i] ^ second[i - offset]).bit_count()
            if distance <= 5:
                if begin is None:
                    begin, distances = i, []
                distances.extend([32] * gap)
                distances.append(distance)
                last, gap = i, 0
            elif begin is not None:
                gap += 1
                if gap > gap_limit:
                    runs.append((begin, last, distances))
                    begin, last, gap = None, None, 0
        if begin is not None:
            runs.append((begin, last, distances))
        for begin, last, distances in runs:
            # A hash covers a window, not an instant. Use the interior of the
            # repeated passage so a fuzzy boundary cannot discard adjacent plot.
            found = interval(begin * step + delay, last * step, duration, minimum=25)
            if (found and len(set(first[begin:last + 1])) >= 30 and distances
                    and sum(d <= 5 for d in distances) / len(distances) >= .90
                    and sum(distances) / len(distances) <= 3.5):
                candidates.append((last - begin, found))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def _duration(path):
    stamp = Path(path).stat().st_mtime
    row = q('SELECT duration FROM probes WHERE path=? AND mtime=?', (path, stamp), one=True)
    if row and row['duration']:
        return float(row['duration'])
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'json', str(path)], capture_output=True, timeout=20)
    return float(json.loads(result.stdout).get('format', {}).get('duration') or 0)


def bilibili_markers(url, expected_duration=None):
    """Domestic PGC OP/ED lives under result.video_info, outside yt-dlp chapters.

    None allows extractor fallback; [] means the original APIs returned no marks.
    Only numeric video identifiers are sent to fixed Bilibili metadata endpoints.
    """
    parsed = urlsplit(url)
    if parsed.hostname not in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com'):
        return None
    ep = re.match(r'/bangumi/play/ep(\d+)(?:/|$)', parsed.path)
    bv = re.match(r'/video/(BV[0-9A-Za-z]+)(?:/|$)', parsed.path)
    if not ep and not bv:
        return None
    session = requests.Session()
    session.headers.update({'User-Agent': core.UA, 'Referer': 'https://www.bilibili.com/'})
    try:
        if core.COOKIES.is_file():
            jar = http.cookiejar.MozillaCookieJar(str(core.COOKIES))
            try:
                jar.load(ignore_discard=True, ignore_expires=False)
                session.cookies.update(jar)
            except (OSError, http.cookiejar.LoadError):
                pass
        def get(path, params):
            response = session.get('https://api.bilibili.com/' + path, params=params, timeout=(3, 6))
            response.raise_for_status()
            if len(response.content) > 8 << 20:
                raise ValueError('metadata budget')
            body = response.json()
            if body.get('code') != 0:
                raise ValueError('metadata unavailable')
            return body.get('data') or body.get('result') or {}
        params = {}
        if ep:
            data = get('pgc/view/web/season', {'ep_id': ep[1]})
            episodes = list(data.get('episodes') or [])
            for section in (data.get('section') or data.get('sections') or []):
                if isinstance(section, dict):
                    episodes.extend(section.get('episodes') or [])
            episode = next((item for item in episodes if isinstance(item, dict) and str(item.get('id')) == ep[1]), None)
            if episode is None:
                return None
            params.update(aid=episode['aid'], cid=episode['cid'], ep_id=ep[1])
        else:
            data = get('x/web-interface/view', {'bvid': bv[1]})
            raw_part = parse_qs(parsed.query).get('p', ['1'])[0]
            if not raw_part.isascii() or not raw_part.isdecimal() or not 1 <= int(raw_part) <= 10000:
                return None
            page = next((item for item in data.get('pages', []) if item.get('page') == int(raw_part)), None)
            if page is None and int(raw_part) != 1:
                return None
            params.update(aid=data['aid'], cid=page['cid'] if page else data['cid'])
        player = get('x/player/wbi/v2', {k: v for k, v in params.items() if k in ('aid', 'cid')})
        marks = []
        for point in player.get('view_points') or []:
            try:
                start, end = float(point['from']), float(point['to'])
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                    marks.append({'t': start, 'end': end, 'title': str(point.get('content') or '')[:40]})
            except (ValueError, TypeError, KeyError):
                continue
        endpoint = 'pgc/player/web/v2/playurl' if ep else 'x/player/wbi/playurl'
        play = get(endpoint, {**params, 'qn': 16, 'fnval': 4048, 'fnver': 0, 'fourk': 1})
        info = play.get('video_info') or play
        if expected_duration is not None:
            remote_duration = float(info.get('timelength') or 0) / 1000
            if not math.isfinite(remote_duration) or remote_duration <= 0 or abs(remote_duration - expected_duration) > 3:
                return []
        clips = info.get('clip_info_list') or []
        for clip in clips:
            title = {'CLIP_TYPE_OP': '片头', 'CLIP_TYPE_ED': '片尾'}.get(clip.get('clipType'))
            if title is None:
                continue
            try:
                start, end = float(clip['start']), float(clip['end'])
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                    marks.append({'t': start, 'end': end, 'title': title, 'kind': 'intro' if title == '片头' else 'outro'})
            except (ValueError, TypeError, KeyError):
                continue
        return sorted(marks, key=lambda mark: mark['t'])
    except (OSError, ValueError, TypeError, KeyError, requests.RequestException):
        return None
    finally:
        session.close()


def source_markers(url, expected_duration=None):
    """Bounded metadata-only refresh for existing Bilibili downloads."""
    if urlsplit(url).hostname not in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'bilibili.tv', 'www.bilibili.tv'):
        return []
    native = bilibili_markers(url, expected_duration=expected_duration)
    if native is not None:
        return native
    from .download import timeline
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-cache-dir', '--dump-single-json',
            '--skip-download', '--no-playlist', '--no-warnings', '--quiet', '--socket-timeout', '8',
            '--retries', '1', '--extractor-retries', '1', '--no-write-subs', '--no-write-auto-subs', '--no-write-thumbnail']
    try:
        with tempfile.TemporaryDirectory(prefix='intro-metadata-') as temp:
            if core.COOKIES.is_file():
                cookies = Path(temp) / 'cookies.txt'
                shutil.copyfile(core.COOKIES, cookies)
                cookies.chmod(0o600)
                args += ['--cookies', str(cookies)]
            result = subprocess.run([*args, '--', url], capture_output=True, timeout=40)
        if result.returncode or len(result.stdout) > 8 << 20:
            return []
        info = json.loads(result.stdout)
        if expected_duration is not None:
            remote_duration = float(info.get('duration') or 0)
            if not math.isfinite(remote_duration) or remote_duration <= 0 or abs(remote_duration - expected_duration) > 3:
                return []
        return timeline(info, url).get('markers', [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _cache_key(owner, jid, part, signature, analysis):
    owner_key = hashlib.sha256(owner.encode()).hexdigest()[:20]
    metadata = hashlib.sha256(json.dumps([group_of({'analysis': analysis}), analysis.get('markers')],
                                        sort_keys=True).encode()).hexdigest()[:16]
    return f'intro:v2:{owner_key}:{jid}:{part}:{signature}:{metadata}'


def _job(jid, owner):
    row = q('SELECT id,owner,url,status,analysis,files FROM jobs WHERE id=? AND owner=?', (jid, owner), one=True)
    return {**core.job_dict(row), '_analysis_raw': row['analysis'], '_files_raw': row['files']} if row and row['status'] == 'done' else None


def _analyze(key, owner, job, part, path, signature, peers):
    pending_key = key
    try:
        job = {**job, 'analysis': dict(job['analysis'])}
        duration = _duration(path)
        found = chapter_intro(job['analysis'], part, duration)
        original_marks = (job['analysis'].get('markers') or {}).get(str(part), [])
        native_bangumi = urlsplit(job['url']).hostname in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com') and '/bangumi/play/ep' in job['url']
        if not found or native_bangumi and not any(isinstance(mark, dict) and mark.get('kind') == 'intro' for mark in original_marks):
            marks = source_markers(job['url'], expected_duration=duration)
            if marks:
                analysis = {**job['analysis'], 'markers': {**(job['analysis'].get('markers') or {}), str(part): marks}}
                q("UPDATE jobs SET analysis=?,updated=? WHERE id=? AND owner=? AND status='done' AND analysis=? AND files=?",
                  (json.dumps(analysis), time.time(), job['id'], owner,
                   job['_analysis_raw'], job['_files_raw']))
                current = _job(job['id'], owner)
                if current is None or current['analysis'] != analysis or current['files'] != job['files']:
                    return
                job = current
                key = _cache_key(owner, job['id'], part, signature, analysis)
                found = chapter_intro(analysis, part, duration)
        if not found and duration >= 600 and peers:
            step, delay = fingerprint_timing()
            first = fingerprint(path, signature)
            matches = []
            signatures = {signature}
            seen_audio = {first}
            for peer in peers:
                other = _job(peer['id'], owner)
                if not other or group_of(other) != group_of(job):
                    continue
                media = library.playable(other)
                if part >= len(media):
                    continue
                other_path = media[part]['path']
                other_key = file_key(other_path)
                if other_key in signatures:
                    continue
                signatures.add(other_key)
                audio = fingerprint(other_path, other_key)
                if audio in seen_audio:
                    continue
                seen_audio.add(audio)
                hit = match_audio(first, audio, step, delay, duration)
                if hit:
                    matches.append({**hit, 'episode': episode_of(other)})
            if matches:
                start, end = max(m['start'] for m in matches), min(m['end'] for m in matches)
                if max(m['start'] for m in matches) - min(m['start'] for m in matches) <= 3 and max(m['end'] for m in matches) - min(m['end'] for m in matches) <= 3:
                    found = interval(start, end, duration, minimum=25)
                    if found:
                        episodes = {m['episode'] for m in matches}
                        found.update(method='audio', automatic=len(matches) >= 2 and None not in episodes
                                     and len(episodes) >= 2 and episode_of(job) is not None and episode_of(job) not in episodes)
        current = _job(job['id'], owner)
        if not current or current['files'] != job['files'] or current['analysis'] != job['analysis'] or file_key(path) != signature:
            return
        core.kv_set(key, {'status': 'ready' if found else 'not_found', **(found or {}), 'updated': time.time()})
    except (OSError, ValueError, RuntimeError, AttributeError, subprocess.SubprocessError):
        core.kv_set(key, {'status': 'unavailable', 'updated': time.time()})
    finally:
        with _lock:
            _pending.discard(pending_key)


@app.route('/api/intro/<int:jid>', methods=['GET', 'POST'])
def api_intro(jid):
    if request.headers.get('X-Privacy-Token') and not web.privacy_token():
        return jsonify(error='隐私权限已失效'), 403
    owner, reveal = g.owner, web.revealed()
    job = _job(jid, owner)
    if not job or jid in web.hidden_ids():
        return jsonify(error='找不到这个视频'), 404
    try:
        raw = request.args.get('part', '0')
        if not raw.isascii() or not raw.isdecimal() or int(raw) > 1000:
            raise ValueError()
        part = int(raw)
        media = library.playable(job)
        if part >= len(media):
            raise ValueError()
        path = media[part]['path']
        signature = file_key(path)
    except (OSError, ValueError):
        return jsonify(error='这个分集已不可用'), 400
    if not group_of(job):
        return jsonify(status='unsupported'), 200, {'Cache-Control': 'no-store'}
    key = _cache_key(owner, jid, part, signature, job['analysis'])
    cached = core.kv_get(key)
    ttl = 30 * 86400 if cached and cached.get('status') == 'ready' else 300
    result = {k: v for k, v in (cached or {}).items() if k != 'updated'} if cached and time.time() - cached.get('updated', 0) < ttl else None
    if result is None:
        with _lock:
            active = key in _pending
            full = len(_pending) >= 3
            if request.method == 'POST' and not active and not full:
                hidden = web.hidden_ids()
                rows = q("SELECT id,owner,url,status,analysis,files FROM jobs WHERE owner=? AND status='done' AND id!=? "
                         "AND json_extract(analysis,'$.library')='TV' ORDER BY id DESC", (owner, jid))
                peers, episodes = [], {episode_of(job)} - {None}
                for row in rows:
                    peer = core.job_dict(row)
                    ep = episode_of(peer)
                    if row['id'] in hidden or group_of(peer) != group_of(job) or ep is not None and ep in episodes:
                        continue
                    peers.append(peer)
                    if ep is not None:
                        episodes.add(ep)
                    if len(peers) == 2:
                        break
                _pending.add(key)
                _pool.submit(_analyze, key, owner, job, part, path, signature, peers)
                active = True
            result = {'status': 'pending' if active else 'busy' if full else 'unknown'}
    for field in ('hidden_ids', 'privacy_job_ids', 'source_hidden_ids', 'tag_aliases'):
        g.pop(field, None)
    try:
        unchanged = file_key(path) == signature
    except (OSError, ValueError):
        unchanged = False
    if (web.revealed() != reveal or request.headers.get('X-Privacy-Token') and not web.privacy_token()
            or not unchanged or _job(jid, owner) != job or jid in web.hidden_ids()):
        return jsonify(error='视频或隐私权限已变化'), 403
    return jsonify(**result), 200, {'Cache-Control': 'no-store'}
