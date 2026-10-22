"""Live clipping: private workspaces, two editorial lanes, and a reviewable publishing queue."""
import base64
import json
import re
import secrets
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path

from flask import Response, g, jsonify, request, send_file
from . import board, core, library, llm, tasks
from . import live_bilibili as bili
from .live_curator import BOUNDARY_PROMPT, CURATOR_FIELDS, CURATOR_PROMPT, confirmed_boundaries, evaluate_highlight, finite
from .core import MEDIA, app, q, _write


ROOT = MEDIA / ".live-clips"
SEGMENT_SECONDS = 60
DEFAULTS = {"auto_highlights": True, "auto_topics": False, "score_min": 80, "min_seconds": 15,
            "max_seconds": 180, "topic_minutes": 20, "daily_limit": 10, "tid": 138,
            "preferences": "", "topic_unit": "auto", "calibration_enabled": True, "calibration_days": 7}
LOGINS = {}


def settings(value=None):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("设置必须是对象")
    out = dict(DEFAULTS)
    if 'topic_unit' in value:
        if value['topic_unit'] not in ('auto', 'story', 'topic'):
            raise ValueError('切片单位应为自动、完整故事或完整话题')
        out['topic_unit'] = value['topic_unit']
    for key in ("auto_highlights", "auto_topics", "calibration_enabled"):
        if key in value:
            if not isinstance(value[key], bool):
                raise ValueError("自动开关必须是 true 或 false")
            out[key] = value[key]
    for key, low, high in (("score_min", 50, 100), ("min_seconds", 5, 120), ("max_seconds", 15, 300),
                           ("topic_minutes", 2, 60), ("daily_limit", 1, 50), ("tid", 1, 9999), ("calibration_days", 1, 30)):
        if key in value:
            number = finite(value[key], -1)
            if not low <= number <= high or number != int(number):
                raise ValueError(f"{key} 应为 {low}–{high} 的整数")
            out[key] = int(number)
    if out["min_seconds"] > out["max_seconds"]:
        raise ValueError("最短切片不能长于最长切片")
    out["preferences"] = str(value.get("preferences") or "")[:2000]
    out["search_keywords"] = str(value.get("search_keywords") or "")[:200]
    out['source_aliases'] = str(value.get('source_aliases') or '')[:200]
    out['title_name'] = str(value.get('title_name') or '').strip()[:20]
    videos = str(value.get('reference_bvids') or '').strip()
    bvids = list(dict.fromkeys(re.findall(r'BV[0-9A-Za-z]{10}', videos)))
    if videos and not bvids:
        raise ValueError('参考视频请填写 BV 号或包含 BV 号的视频链接')
    out['reference_bvids'] = ','.join(bvids[:80])
    accounts = str(value.get('reference_mids') or '').strip()
    mids = re.findall(r'space\.bilibili\.com/([0-9]{1,20})', accounts)
    mids += [x for x in re.split(r'[\s,，;；]+', accounts) if re.fullmatch(r'[0-9]{1,20}', x)]
    if accounts and not mids:
        raise ValueError('切片 UP 主请填写 B站空间链接或数字 UID')
    out['reference_mids'] = ','.join(dict.fromkeys(mids))[:500]
    return out


def workspace():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ROOT


def delete_clip_files(cid):
    for path in (ROOT / 'clips').glob(f'{int(cid)}-v*'):
        if path.is_file() or path.is_symlink():
            path.unlink()


def media_path(value):
    path = Path(value).resolve()
    path.relative_to(ROOT.resolve())
    if not path.is_file():
        raise ValueError("素材不存在或原始缓存已清理")
    return path


def owned(table, item):
    if table not in ("live_sources", "live_recordings", "live_clips", "live_references"):
        raise ValueError("unknown table")
    visible = ' AND removed_at IS NULL' if table == 'live_clips' else ''
    return q(f"SELECT * FROM {table} WHERE id=? AND owner=?{visible}", (item, g.owner), one=True)


def login_required():
    return (jsonify(error="请先登录拾光账号"), 401) if not g.user else None


def source_settings(source):
    return settings(json.loads(source["settings"] or "{}"))


def clip_dict(row, source=None):
    d = dict(row)
    for field in ("tags", "scores", "performance"):
        d[field] = json.loads(d[field] or ("[]" if field == "tags" else "{}"))
    d["duration"] = round(d["end"] - d["start"], 2)
    d["preview"] = f"/api/live/clips/{d['id']}/media?v={d['version']}" if d["file"] and d["render_version"] == d["version"] else ""
    d['cover_revision'] = Path(d['cover']).stem if d['cover'] else ''
    d['publication_description'] = bili.publication_description(d, source) if source else d['description']
    if source:
        origin = bili.original_source(d, source)
        d['source_name'], d['source_url'] = origin['name'], f"https://live.bilibili.com/{origin['room_id']}"
    d.pop('origin', None)
    d.pop("file", None); d.pop("cover", None)
    return d


def render_inputs(clip):
    rows = q("SELECT * FROM live_recordings WHERE session_key=? AND owner=? AND offset < ? "
             "AND offset + duration > ? ORDER BY offset", (clip["session_key"], clip["owner"], clip["end"], clip["start"]))
    if not rows or rows[0]["offset"] > clip["start"] + 1 or rows[-1]["offset"] + rows[-1]["duration"] < clip["end"] - 1:
        raise ValueError("切片范围超出可用直播缓存")
    for left, right in zip(rows, rows[1:]):
        if right["offset"] - (left["offset"] + left["duration"]) > 2:
            raise ValueError("切片中间有缺失的直播素材")
    return [media_path(r["path"]) for r in rows], max(0, clip["start"] - rows[0]["offset"])


def concat_list(paths):
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", dir=workspace(), delete=False)
    with handle:
        for path in paths:
            # Generated recording paths contain only numeric IDs, timestamps and UUIDs.
            if any(ch in str(path) for ch in ("'", "\n", "\r")):
                raise ValueError("素材路径无效")
            handle.write(f"file '{path}'\n")
    return Path(handle.name)


def queue_render(cid):
    row = q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
    if not row or row["state"] in ("uploading", "submitted", "uncertain", "rejected"):
        return
    _, seek = render_inputs(row)
    _write("UPDATE live_clips SET state='queued', error='', updated=? WHERE id=?", (time.time(), cid))
    board.publish("live_render", f"live-clip:{cid}:v{row['version']}", 95, force=True,
                  payload={"clip": cid, "version": row["version"], "seek": seek,
                           "duration": row["end"] - row["start"], "title": row["title"]})


def create_clip(owner, source, session_key, lane, candidate):
    """Serialize overlapping-window deduplication across web and AI worker processes."""
    if candidate.get('content_kind') == 'advertisement':
        return None
    from .live_editorial import normalized_scores, queue_score
    rated = normalized_scores(candidate.get('scores'))
    start, end = candidate["start"], candidate["end"]
    session = q('SELECT origin FROM live_sessions WHERE key=? AND owner=?', (session_key, owner), one=True)
    title = bili.publication_title({'title': candidate['title'], 'origin': session['origin'] if session else '{}'}, dict(source))
    with core.db_lock:
        try:
            core.DB.execute("BEGIN IMMEDIATE")
            old = core.DB.execute("SELECT start, end FROM live_clips WHERE owner=? AND session_key=? AND lane=?",
                                  (owner, session_key, lane)).fetchall()
            if any(max(0, min(end, r["end"]) - max(start, r["start"])) / max(1, (max if lane == "topic" else min)(end - start, r["end"] - r["start"])) > (.8 if lane == "topic" else .5)
                   for r in old):
                core.DB.commit()
                return None
            now = time.time()
            cur = core.DB.execute("INSERT INTO live_clips (owner, source_id, session_key, lane, start, end, title, "
                                  "description, tags, score, scores, reason, created, updated, needs_review, cover_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (owner, source["id"], session_key, lane, start, end, title,
                                   candidate.get("description", ""), json.dumps(candidate.get("tags") or ["直播切片", source["name"][:20]], ensure_ascii=False),
                                   rated[0] if rated else candidate.get("score", 0), json.dumps(rated[1] if rated else candidate.get("scores") or {}),
                                   candidate.get("reason", ""), now, now, int(bool(candidate.get("needs_review"))),
                                   str(candidate.get('cover_title') or '').strip()[:48]))
            cid = cur.lastrowid
            core.DB.commit()
        except BaseException:
            core.DB.rollback()
            raise
    queue_render(cid)
    if lane == 'topic' and not rated:
        queue_score(cid)
    return cid


def queue_upload(cid, automatic=False):
    row = q("SELECT * FROM live_clips WHERE id=?", (cid,), one=True)
    if not row or row["state"] != "ready" or row["version"] != row["render_version"]:
        return False
    if row['content_kind'] == 'advertisement' or row['merged_into']:
        return False
    source = q("SELECT * FROM live_sources WHERE id=?", (row["source_id"],), one=True)
    if not source:
        return False
    config = source_settings(source)
    if automatic and not automatic_allowed(row, source):
        return False
    account = q("SELECT * FROM live_accounts WHERE owner=? AND status='ready'", (row["owner"],), one=True)
    if not account or not bili.account_path(row["owner"]).exists() or not bili.ready():
        if automatic:
            return False
        raise ValueError("请先在直播切片页扫码绑定 B站投稿账号")
    if automatic and row["lane"] == "topic" and q("SELECT id FROM live_clips WHERE session_key=? AND lane='highlight' "
                                                  "AND state IN ('awaiting_upload','uploading','submitted','uncertain') "
                                                  "AND start < ? AND end > ? LIMIT 1", (row["session_key"], row["end"], row["start"]), one=True):
        return False
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    with core.db_lock:
        try:
            core.DB.execute("BEGIN IMMEDIATE")
            n = core.DB.execute("SELECT COUNT(*) n FROM live_clips WHERE owner=? AND source_id=? AND "
                                "(submit_started >= ? OR state='awaiting_upload')", (row["owner"], source["id"], midnight)).fetchone()["n"]
            if n >= config["daily_limit"]:
                core.DB.commit()
                if automatic:
                    return False
                raise ValueError("已达到这个 UP 主今天的投稿上限，可在追踪设置中调整")
            changed = core.DB.execute("UPDATE live_clips SET state='awaiting_upload', needs_review=CASE WHEN ? THEN needs_review ELSE 0 END, updated=? WHERE id=? AND state='ready'",
                                      (int(automatic), time.time(), cid)).rowcount
            core.DB.commit()
        except BaseException:
            core.DB.rollback()
            raise
    if not changed:
        return False
    board.publish("live_upload", f"live-upload:{cid}:v{row['version']}", 100 if row["lane"] == "highlight" else 40,
                  payload={"clip": cid, "version": row["version"], "title": row["title"], "automatic": automatic}, force=True)
    return True


def automatic_block_reason(clip, source, validated=None):
    """One source of automatic-publication eligibility and its explanation."""
    from .live_editorial import normalized_scores
    if clip['content_kind'] == 'advertisement' or clip['merged_into']:
        return 'excluded', '广告或已合并片段不自动投稿'
    if clip['lane'] == 'topic':
        rated = normalized_scores(json.loads(clip['scores'] or '{}'))
        if not rated:
            return 'score', '话题片段尚未完成有效评分'
        if rated[1]['completeness'] < 80:
            return 'quality', '话题完整度未达到自动投稿标准'
    from .live_calibration import active_rubric
    config = source_settings(source)
    if not source['enabled']:
        return 'configure', '主播追踪已暂停，自动投稿也已暂停'
    if clip['needs_review']:
        return 'edit', '手动新增或编辑过的片段需要你确认'
    if clip['error']:
        return 'edit', str(clip['error'])
    if clip['version'] != clip['render_version']:
        return 'retry', '当前编辑版本尚未制作完成'
    if not config['auto_' + ('highlights' if clip['lane'] == 'highlight' else 'topics')]:
        return 'publish', '此类片段未开启自动投稿，需要手动确认'
    if not (active_rubric(source['id']).get('validated') if validated is None else validated):
        return 'calibrate', '当前主播的评分标准尚未通过有效校准'
    if clip['lane'] == 'highlight':
        scores = json.loads(clip['scores'] or '{}')
        if (clip['score'] < config['score_min'] or finite(scores.get('completeness')) < 80
                or min(finite(scores.get(k)) for k in ('topic', 'traffic', 'fun')) < 55):
            return 'quality', '亮点评分或完整度未达到自动投稿标准'
    return None, ''


def automatic_allowed(clip, source):
    """Apply the same current eligibility checks when queuing and immediately before publishing."""
    return automatic_block_reason(clip, source)[0] is None


def after_transcribe(task, result):
    p = json.loads(task["payload"])
    segments = [[finite(a), finite(b), str(text)[:2000]] for a, b, text in result.get("segments", [])
                if finite(b) > finite(a) >= 0 and str(text).strip()]
    if p.get("reference"):
        q("UPDATE live_references SET segments=?, state='ready', source_match='pending', updated=? WHERE id=?",
          (json.dumps(segments, ensure_ascii=False), time.time(), p["reference"]))
        return
    q("UPDATE live_recordings SET segments=?, state='ready', updated=?, error='' WHERE id=?",
      (json.dumps(segments, ensure_ascii=False), time.time(), p["recording"]))


@board.task("live_transcribe", "直播转写", "now", prefer="gpu", prefer_wait=20, then=after_transcribe)
def pi_live_transcribe(task, beat):
    return tasks.pi_transcribe(task, beat)


@board.task("live_analyze", "直播选题：亮点与话题", "ai-quick")
def analyze(task, beat):
    key = task["payload"]["session"]
    session = q("SELECT * FROM live_sessions WHERE key=?", (key,), one=True)
    if not session:
        raise ValueError("直播记录已删除")
    source = q("SELECT * FROM live_sources WHERE id=?", (session["source_id"],), one=True)
    if not source:
        raise ValueError("追踪已删除")
    config = source_settings(source)
    rows = q("SELECT * FROM live_recordings WHERE session_key=? ORDER BY seq", (key,))
    cursor, latest = session["cursor"], session["cursor"]
    for row in rows:
        end = row["offset"] + row["duration"]
        if end <= cursor + .5:
            continue
        if latest == cursor and (row["state"] in ("failed", "expired") or row["offset"] > cursor + 2):
            # Close the known discussion before a permanent ASR failure or missing recording; never bridge it.
            if cursor - session["topic_start"] >= 30:
                create_clip(session["owner"], source, key, "topic",
                            {"start": session["topic_start"], "end": cursor,
                             "title": session["topic_title"] or session["title"], "reason": "转写中断，保留此前的话题"})
            until = end if row["state"] in ("failed", "expired") else row["offset"]
            q("UPDATE live_sessions SET cursor=?, topic_start=?, topic_title='',topic_summary='',topic_kind='content' WHERE key=? AND cursor=?",
              (until, until, key, cursor))
            return {"gap": True, "until": until}
        if row["state"] != "ready" or row["offset"] > latest + 2:
            break
        latest = end
        if latest - cursor >= 120:
            break
    final_pass = (session['status'] != 'recording' and cursor - session['topic_start'] >= 30
                  and not any(r['offset'] + r['duration'] > cursor + 1 for r in rows))
    if latest <= cursor + .5 and not final_pass:
        return {"waiting": True}
    # A skipped segment also separates context windows: later highlights must not span that gap.
    gap_end = max((r["offset"] + r["duration"] for r in rows
                   if r["state"] in ("failed", "expired") and r["offset"] < latest), default=0)
    previous_end = 0
    for row in rows:
        if row["offset"] >= latest:
            break
        if row["offset"] > previous_end + 2:
            gap_end = max(gap_end, row["offset"])
        previous_end = max(previous_end, row["offset"] + row["duration"])
    earliest = max(gap_end, latest - max(360, config["max_seconds"] + 60), 0)
    transcript = []
    for row in rows:
        if row["state"] == "ready" and row["offset"] < latest and row["offset"] + row["duration"] > earliest:
            for start, end, text in json.loads(row["segments"] or "[]"):
                at = row["offset"] + start
                if earliest <= at < latest:
                    transcript.append([round(at, 2), round(min(latest, row["offset"] + end), 2), text])
    from . import live_calibration
    rubric = live_calibration.active_rubric(source["id"])
    payload = {"up": source["name"], "title_name": bili.title_name(dict(source)), "preferences": config["preferences"], "rubric": rubric,
               "already_processed_until": cursor, "available_start": earliest, "available_end": latest,
               "topic_start": session["topic_start"], "known_topic": session["topic_title"],
               "known_summary": session['topic_summary'], "known_kind": session['topic_kind'], "topic_unit": config['topic_unit'],
               "clip_seconds": [config["min_seconds"], config["max_seconds"]], "transcript": transcript}
    if transcript:
        out = llm.llm_json(CURATOR_PROMPT, json.dumps(payload, ensure_ascii=False), CURATOR_FIELDS,
                           5000, {}, "live-curator")
    else:
        out = {"highlights": [], "topic_changes": [], "current_topic": session["topic_title"]}
        if not session["topic_title"]:
            q("UPDATE live_sessions SET cursor=?, topic_start=? WHERE key=? AND cursor=?", (latest, latest, key, cursor))
            return {"silence": True, "until": latest}
    # Recording can stop or append its final segment while the model is working.
    finished = bool(q("SELECT 1 FROM live_sessions s WHERE s.key=? AND s.status!='recording' "
                      "AND NOT EXISTS (SELECT 1 FROM live_recordings r WHERE r.session_key=s.key "
                      "AND r.offset+r.duration>?)", (key, latest + 1), one=True))
    # Wait for some dialogue after a possible switch; reconsider recent unclosed boundaries next time.
    boundary_end = latest if finished else latest - 30
    candidates = sorted([x for x in (out.get('topic_changes') or [])[:10] if isinstance(x, dict)
                         and max(session['topic_start'] + 30, cursor - 60, earliest) <= finite(x.get('at'), -1) <= latest],
                        key=lambda x: finite(x.get('at')))
    changes = [change for change in candidates if finite(change.get('at')) <= boundary_end]
    if changes:
        beat({'phase': 'checking-boundaries', 'until': latest})
        review = llm.llm_json(BOUNDARY_PROMPT, json.dumps({
            'up': source['name'], 'preferences': config['preferences'], 'topic_unit': config['topic_unit'],
            'known_topic': session['topic_title'], 'known_summary': session['topic_summary'],
            'topic_start': session['topic_start'], 'transcript': transcript,
            'candidates': [dict(change, index=index) for index, change in enumerate(changes)]}, ensure_ascii=False),
            {'boundaries': [{'index': 'integer', 'previous_complete': 'boolean', 'next_independent': 'boolean',
                             'confidence': '0-1', 'reason': 'string'}]}, 1800, {}, 'live-boundary', think=False)
        changes = confirmed_boundaries(review, changes)
    beat({"phase": "curating", "until": latest})
    count = 0
    for candidate in (out.get("highlights") or [])[:5]:
        valid = evaluate_highlight(candidate, config, earliest, latest, cursor)
        if valid and create_clip(session["owner"], source, key, "highlight", valid):
            count += 1
    topic_start, topic_title = session["topic_start"], session["topic_title"] or str(out.get("current_topic") or source["name"])[:80]
    topic_kind = session['topic_kind'] or 'content'
    if not session['topic_title']:
        initial_kind = candidates[0].get('previous_kind') if candidates else out.get('current_kind')
        topic_kind = initial_kind or topic_kind
    for change in changes:
        at = finite(change.get("at"), -1)
        if not max(topic_start + 30, cursor - 60, earliest) <= at <= boundary_end:
            continue
        create_clip(session["owner"], source, key, "topic", {"start": topic_start, "end": at,
                    "title": str(change.get('previous_title') or topic_title)[:45],
                    "content_kind": change.get('previous_kind') or topic_kind,
                    "scores": change.get('previous_scores'), "description": str(change.get("previous_summary") or "")[:1800],
                    "reason": "按直播话题切换分段"})
        topic_start, topic_title = at, str(change.get("next_title") or out.get("current_topic") or "下一话题")[:80]
        topic_kind = change.get('next_kind') or 'content'
    # Window-end classification belongs to the new topic only after its boundary is accepted.
    # Otherwise a deferred advertising break would erase the content before it on finalization.
    # A long case stays together until an actual switch or the recording ends.
    if finished:
        if latest - topic_start >= 30:
            create_clip(session["owner"], source, key, "topic", {"start": topic_start, "end": latest,
                        "title": topic_title[:45], "content_kind": topic_kind, 'scores': out.get('current_scores'),
                        "reason": "录制停止，保留最后一个完整话题"})
        topic_start = latest
    q("UPDATE live_sessions SET cursor=?, topic_start=?, topic_title=?,topic_summary=?,topic_kind=? WHERE key=? AND cursor=?",
      (latest, topic_start, topic_title, str(out.get('current_summary') or session['topic_summary'])[:1500], topic_kind, key, cursor))
    return {"highlights": count, "until": latest}


def after_render(task, result):
    p = json.loads(task["payload"])
    row = q("SELECT * FROM live_clips WHERE id=? AND version=?", (p["clip"], p["version"]), one=True)
    if not row or row["state"] == "rejected":
        return
    path = workspace() / "clips" / f"{row['id']}-v{row['version']}.mp4"
    media_path(path)
    _write("UPDATE live_clips SET file=?, cover=?, state='ready', render_version=?, error='', updated=? "
           "WHERE id=? AND version=? AND state NOT IN ('rejected','submitted','uploading','uncertain')",
           (str(path), "", row["version"], time.time(), row["id"], row["version"]))
    if not row['needs_review']:
        queue_upload(row["id"], automatic=True)


@board.task("live_render", "制作直播切片", "now", prefer="gpu", prefer_wait=20, then=after_render)
def render(task, beat):
    p = task["payload"]
    row = q("SELECT c.*, s.origin FROM live_clips c LEFT JOIN live_sessions s ON s.key=c.session_key "
            "WHERE c.id=? AND c.version=?", (p["clip"], p["version"]), one=True)
    if not row or row["state"] not in ("queued", "rendering"):
        raise ValueError("切片已修改或已取消")
    paths, seek = render_inputs(row)
    listing = concat_list(paths)
    destination = workspace() / "clips" / f"{row['id']}-v{row['version']}.mp4"
    destination.parent.mkdir(exist_ok=True)
    temp = destination.with_suffix(".partial.mp4")
    _write("UPDATE live_clips SET state='rendering', updated=? WHERE id=? AND version=?", (time.time(), row["id"], row["version"]))
    command = ["ffmpeg", "-v", "error", "-y", "-ss", str(seek), "-f", "concat", "-safe", "0", "-i", str(listing),
               "-t", str(row["end"] - row["start"]), "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale='min(1280,iw)':-2",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2", "-c:a", "aac", "-movflags", "+faststart", str(temp)]
    proc = None
    try:
        with core.heavy_slot("encode"):
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.time()
            while proc.poll() is None:
                current = q("SELECT version, state FROM live_clips WHERE id=?", (row["id"],), one=True)
                if not current or current["version"] != row["version"] or current["state"] == "rejected":
                    raise ValueError("切片已修改或已取消")
                if time.time() - started > 3600 or not beat({"phase": "rendering"}):
                    raise ValueError("切片制作已中断，可重试")
                time.sleep(2)
            if proc.returncode or not temp.exists() or not library.duration_of(str(temp)):
                raise ValueError("切片制作失败，请检查直播素材")
        temp.replace(destination)
        return {"rendered": True}
    finally:
        if proc and proc.poll() is None:
            proc.kill(); proc.wait()
        listing.unlink(missing_ok=True); temp.unlink(missing_ok=True)


@board.task("live_upload", "直播切片投稿", "publish")
def upload(task, beat):
    p = task["payload"]
    row = q("SELECT c.*, s.origin FROM live_clips c LEFT JOIN live_sessions s ON s.key=c.session_key "
            "WHERE c.id=? AND c.version=?", (p["clip"], p["version"]), one=True)
    if not row or row["state"] != "awaiting_upload":
        return {"skipped": "not awaiting upload"}
    source = q("SELECT * FROM live_sources WHERE id=?", (row["source_id"],), one=True)
    media_path(row["file"])
    if not source:
        raise ValueError("追踪设置不存在")
    if p.get("automatic") and not automatic_allowed(row, source):
        q("UPDATE live_clips SET state='ready', updated=? WHERE id=? AND state='awaiting_upload'", (time.time(), row["id"]))
        return {"paused": True, 'reason': '当前校准、编辑状态或投稿设置需要重新确认'}
    if not _write("UPDATE live_clips SET state='uploading', submit_started=?, updated=? WHERE id=? AND state='awaiting_upload'",
                  (time.time(), time.time(), row["id"])):
        return {"skipped": "already claimed"}
    try:
        result = bili.publish(row, source, beat)
    except ValueError as exc:  # validation happens before a process can submit anything
        q("UPDATE live_clips SET state='ready', error=?, submit_started=NULL, updated=? WHERE id=?",
          (str(exc), time.time(), row["id"]))
        if "登录" in str(exc) or "账号" in str(exc):
            q("UPDATE live_accounts SET status='needs_login' WHERE owner=?", (row["owner"],))
        return {"blocked": str(exc)}
    except Exception:
        q("UPDATE live_clips SET state='uncertain', error=?, updated=? WHERE id=?",
          ("投稿结果待核实，请到 B站创作中心确认；系统不会自动重复投稿", time.time(), row["id"]))
        return {"uncertain": True}
    q("UPDATE live_clips SET state='submitted', bvid=?, aid=?, error='', updated=? WHERE id=?",
      (result["bvid"], result["aid"], time.time(), row["id"]))
    return result


def review_context(sources, account, uploader_ready):
    """Shared read-only facts, fetched once for the whole list."""
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return {
        'sources': sources, 'rubrics': {},
        'account_ready': bool(account and account['status'] == 'ready' and bili.account_path(g.owner).exists()),
        'uploader_ready': uploader_ready,
        'daily': {r['source_id']: r['n'] for r in q(
            "SELECT source_id,COUNT(*) n FROM live_clips WHERE owner=? "
            "AND (submit_started>=? OR state='awaiting_upload') GROUP BY source_id", (g.owner, midnight))},
        'scoring': {r['clip'] for r in q(
            "SELECT json_extract(t.payload,'$.clip') clip FROM tasks t JOIN live_clips c "
            "ON c.id=json_extract(t.payload,'$.clip') WHERE c.owner=? AND t.kind='live_score' "
            "AND t.state IN ('queued','running')", (g.owner,))},
        'published_highlights': q(
            "SELECT session_key,start,end FROM live_clips WHERE owner=? AND lane='highlight' "
            "AND state IN ('awaiting_upload','uploading','submitted','uncertain')", (g.owner,)),
    }


def clip_review(clip, context):
    """Explain the next useful action without reserving a slot or changing posting policy."""
    source = context['sources'].get(clip['source_id'])
    state = clip['state']
    result = {'needs_attention': False, 'review_reason': '', 'next_action': '', 'action': 'none',
              'can_upload': False, 'can_edit': state not in ('uploading', 'submitted', 'uncertain')}

    def explain(reason, next_action, action, attention=True):
        return {**result, 'review_reason': reason, 'next_action': next_action,
                'action': action, 'needs_attention': attention}

    if state == 'uncertain':
        return explain('投稿结果尚未核实，系统不会重复投稿',
                       '先到 B站创作中心确认，再填写 BV 号或确认未成功后重试', 'check_publish')
    if state == 'failed':
        return explain(clip['error'] or '切片制作失败', '检查原始素材后重试制作', 'retry')
    if state in ('queued', 'rendering'):
        return explain('正在等待或进行成片制作', '等待制作完成后预览', 'wait', False)
    if state in ('awaiting_upload', 'uploading'):
        return explain('已进入投稿队列', '等待投稿结果，避免重复提交', 'wait', False)
    if state in ('submitted', 'rejected') or clip['merged_into'] or clip['content_kind'] == 'advertisement':
        return result
    if state != 'ready':
        return result
    if not source:
        return explain('原主播追踪信息已不存在', '检查该片段的来源信息', 'none')
    if clip['version'] != clip['render_version'] or not clip['file']:
        return explain('当前编辑版本还没有可用成片', '重新制作后再预览或投稿', 'retry')
    config = source_settings(source)
    daily_full = context['daily'].get(source['id'], 0) >= config['daily_limit']
    result['can_upload'] = bool(context['account_ready'] and context['uploader_ready'] and not daily_full)
    if not context['account_ready']:
        return explain('尚未绑定可用的 B站投稿账号，或账号需要重新登录', '扫码绑定或重新登录投稿账号', 'bind_account')
    if not context['uploader_ready']:
        return explain('投稿组件尚未安装', '请管理员安装投稿组件后再试', 'none')
    if daily_full:
        return explain('已达到该主播今天的投稿上限', '等待次日，或在追踪设置中调整上限', 'configure')
    if source['id'] not in context['rubrics']:
        from .live_calibration import active_rubric
        context['rubrics'][source['id']] = bool(active_rubric(source['id']).get('validated'))
    code, reason = automatic_block_reason(clip, source, context['rubrics'][source['id']])
    if code == 'score' and clip['id'] in context['scoring']:
        return explain('评分任务正在排队或处理中', '等待评分完成；也可先预览成片', 'wait', False)
    if code:
        actions = {
            'score': ('重新安排评分，或预览后手动决定', 'score'),
            'quality': ('预览并检查内容，可编辑、忽略或手动决定投稿', 'edit'),
            'configure': ('恢复追踪，或预览后手动决定投稿', 'configure'),
            'edit': ('预览并确认内容后，再手动投稿', 'edit'),
            'retry': ('重新制作成片', 'retry'),
            'publish': ('预览确认后，手动投稿', 'publish'),
            'calibrate': ('到评分校准页完成验证，或预览后手动决定投稿', 'calibrate'),
        }
        next_action, action = actions.get(code, ('检查片段后再决定', 'edit'))
        return explain(reason, next_action, action)
    if clip['lane'] == 'topic' and any(
            r['session_key'] == clip['session_key'] and r['start'] < clip['end'] and r['end'] > clip['start']
            for r in context['published_highlights']):
        return explain('相同时间范围已有亮点进入投稿流程，完整话题不会重复自动投稿',
                       '核对已有投稿，再决定是否手动投稿完整话题', 'publish')
    return explain('已满足自动投稿条件，等待后台安排', '等待投稿队列更新', 'wait', False)


def clip_page(raw_sources, account, uploader_ready, tab):
    """Apply owner/source/state filters before limiting, with full counts and a stable cursor."""
    source_arg, state = request.args.get('source', ''), request.args.get('state', 'all')
    states = {'all', 'queued', 'rendering', 'ready', 'awaiting_upload', 'uploading', 'submitted',
              'uncertain', 'rejected', 'failed'}
    if source_arg and (len(source_arg) > 19 or not source_arg.isascii() or not source_arg.isdigit()
                       or not 0 < int(source_arg) <= 9223372036854775807):
        raise ValueError('主播筛选无效')
    if state not in states:
        raise ValueError('片段状态无效')
    try:
        limit = int(request.args.get('limit', '150'))
    except (ValueError, TypeError):
        raise ValueError('列表数量无效') from None
    if not 1 <= limit <= 300:
        raise ValueError('列表数量应为 1–300')
    cursor = request.args.get('cursor', '')
    cursor_match = re.fullmatch(r'([0-2]):([1-9][0-9]{0,18})', cursor) if cursor else None
    if cursor and not cursor_match:
        raise ValueError('分页位置无效')
    if cursor_match and int(cursor_match.group(2)) > 9223372036854775807:
        raise ValueError('分页位置无效')
    scope, args = "c.owner=? AND c.removed_at IS NULL", [g.owner]
    if source_arg:
        scope += " AND c.source_id=?"
        args.append(int(source_arg))
    ordinary = "COALESCE(c.content_kind,'content')!='advertisement' AND c.merged_into IS NULL"
    counts = {'review': 0, 'highlight': 0, 'topic': 0}
    for row in q(f"SELECT c.lane,COUNT(*) n FROM live_clips c WHERE {scope} AND {ordinary} GROUP BY c.lane", args):
        if row['lane'] in counts:
            counts[row['lane']] = row['n']
    context = review_context(raw_sources, account, uploader_ready)
    attention = {}
    rows = q("SELECT c.id,c.source_id,c.session_key,c.state,c.lane,c.start,c.end,c.version,c.render_version,"
             "c.file,c.error,c.needs_review,c.content_kind,c.merged_into,c.scores,c.score "
             f"FROM live_clips c WHERE {scope} AND {ordinary} AND c.state IN ('ready','failed','uncertain')", args)
    for row in rows:
        attention[row['id']] = clip_review(row, context)
    review_ids = [cid for cid, details in attention.items() if details['needs_attention']]
    counts['review'] = len(review_ids)
    if tab == 'review':
        scope += " AND c.id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(review_ids))
    elif tab in ('highlight', 'topic'):
        scope += " AND c.lane=?"
        args.append(tab)
    if state != 'all':
        scope += " AND c.state=?"
        args.append(state)
    elif tab in ('highlight', 'topic'):
        scope += " AND " + ordinary
    total = q(f"SELECT COUNT(*) n FROM live_clips c WHERE {scope}", args, one=True)['n']
    priority = "CASE c.state WHEN 'uncertain' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END" if tab == 'review' else "0.0"
    page_scope, page_args = scope, list(args)
    if cursor_match:
        rank, cid = map(int, cursor_match.groups())
        page_scope += f" AND (({priority})>? OR (({priority})=? AND c.id<?))"
        page_args.extend((rank, rank, cid))
    found = q("SELECT c.*,s.origin FROM live_clips c LEFT JOIN live_sessions s ON s.key=c.session_key "
              f"WHERE {page_scope} ORDER BY ({priority}),c.id DESC LIMIT ?", (*page_args, limit + 1))
    more, rows = len(found) > limit, found[:limit]
    clips = [dict(clip_dict(row, raw_sources.get(row['source_id'])),
                  **(attention.get(row['id']) or clip_review(row, context))) for row in rows]
    rank = (0 if rows[-1]['state'] == 'uncertain' else 1 if rows[-1]['state'] == 'failed' else 2) \
        if rows and tab == 'review' else 0
    return {'clips': clips, 'counts': counts, 'total': total, 'has_more': more,
            'next_cursor': f"{rank}:{rows[-1]['id']}" if more and rows else None}


@app.get("/api/live")
def overview():
    if denied := login_required():
        return denied
    from .live_calibration import PROTOCOL_VERSION
    tab = request.args.get('tab', 'calibration')
    sources = []
    raw_sources = {r['id']: r for r in q("SELECT * FROM live_sources WHERE owner=? ORDER BY id DESC", (g.owner,))}
    for row in raw_sources.values():
        d = dict(row); d["settings"] = source_settings(row)
        if tab in ('review', 'highlight', 'topic', 'recordings'):
            sources.append(d)
            continue
        monitor = core.kv_get(f"live-monitor:{row['id']}", None)
        d["monitor"] = monitor if monitor and monitor.get("room_id") == row["room_id"] and monitor.get("owner") == row["owner"] else None
        d["search_audit"] = core.kv_get(f"live-search-audit:{row['id']}", None)
        status = q("SELECT id, state, kind, error, progress, updated FROM tasks WHERE kind IN ('live_calibrate','live_refine','live_verify','live_style') "
                   "AND json_extract(payload, '$.source')=? ORDER BY updated DESC LIMIT 1", (row["id"],), one=True)
        d["calibration_task"] = dict(status) if status else None
        from . import live_calibration
        d['sample_progress'] = live_calibration.cached_sample_progress(row)
        d['calibration_wait'] = core.kv_get(f"live-calibration-wait:{row['id']}")
        aliases = core.kv_get(f"live-source-aliases:{row['id']}", {})
        d['learned_aliases'] = aliases.get('aliases', []) if aliases.get('uid') == row['uid'] else []
        profile = core.kv_get(f"live-style:{row['id']}")
        valid_style_ids = {r['id'] for r in q("SELECT id FROM live_references WHERE source_id=? AND excluded=0 AND source_match='matched'", (row['id'],))}
        d['style_profile'] = profile if profile and set(profile['reference_ids']) <= valid_style_ids else None
        attempt = q("SELECT version, rubric, evidence, created FROM live_rubrics WHERE source_id=? ORDER BY version DESC LIMIT 1", (row["id"],), one=True)
        d["last_calibration"] = dict(attempt) if attempt else None
        if d["last_calibration"]:
            d["last_calibration"]["evidence"] = json.loads(d["last_calibration"]["evidence"])
            d["last_calibration"]["rubric"] = json.loads(d["last_calibration"]["rubric"])
            d["last_calibration"]["current"] = d["last_calibration"]["evidence"].get("protocol") == PROTOCOL_VERSION
        d["rubric"] = q("SELECT id, version, rubric, evidence, created FROM live_rubrics WHERE source_id=? AND active=1 ORDER BY version DESC LIMIT 1", (row["id"],), one=True)
        if d["rubric"]:
            d["rubric"] = dict(d["rubric"])
            for f in ("rubric", "evidence"):
                d["rubric"][f] = json.loads(d["rubric"][f])
            if not d["rubric"]["evidence"].get("validated") or d["rubric"]["evidence"].get("protocol") != PROTOCOL_VERSION or not live_calibration.active_rubric(row['id']):
                d["rubric"] = None
        sources.append(d)
    by_source = {s['id']: s for s in sources}
    account = q("SELECT mid, name, status, updated FROM live_accounts WHERE owner=?", (g.owner,), one=True)
    uploader_ready = bili.ready()
    try:
        page = clip_page(raw_sources, account, uploader_ready, tab)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(sources=sources, account=dict(account) if account else None, uploader_ready=uploader_ready,
                   segment_seconds=SEGMENT_SECONDS,
                   **page,
                   recordings=[{k: r[k] for k in ("id", "source_id", "session_key", "seq", "offset", "duration", "state", "error", "created")}
                               for r in q("SELECT * FROM live_recordings WHERE owner=? ORDER BY id DESC LIMIT 60", (g.owner,))],
                   sessions=[dict(r) for r in q("SELECT * FROM live_sessions WHERE owner=? ORDER BY started DESC LIMIT 30", (g.owner,))],
                   references=[{**{k: r[k] for k in ("id", "source_id", "bvid", "title", "author", "published", "duration", "views", "state", "error", "excluded", "source_match", "match_reason")},
                                'role': live_calibration.reference_role(r, by_source[r['source_id']])}
                               for r in q("SELECT id, source_id, bvid, title, author, published, duration, views, state, error, "
                                          "excluded, source_match, match_reason, reference_info FROM live_references "
                                          "WHERE owner=? ORDER BY source_id,id DESC", (g.owner,)) if r['source_id'] in by_source]
                   if tab == 'calibration' else [])


@app.post("/api/live/sources")
@app.post("/api/live/sources/<int:sid>")
def save_source(sid=None):
    if denied := login_required():
        return denied
    old = owned("live_sources", sid) if sid else None
    if sid and not old:
        return jsonify(error="追踪不存在"), 404
    body = request.get_json(silent=True) or {}
    try:
        base = json.loads(old["settings"]) if old else {}
        config = settings({**base, **(body.get("settings") or {})})
        reference = body.get("url") or (old["url"] if old else "")
        info = bili.room_info(reference, g.owner, lookup_name=True) if not old or reference != old["url"] else dict(old)
        if old and info['room_id'] != old['room_id']:
            return jsonify(error='更换主播请新增追踪；原直播记录、来源和专属标准会保留。'), 409
        if not old:
            existing = q("SELECT id, name, archived FROM live_sources WHERE owner=? AND room_id=?", (g.owner, info["room_id"]), one=True)
            if existing:
                if existing['archived']:
                    q("UPDATE live_sources SET archived=0,enabled=1,checked=NULL,error='',updated=? WHERE id=?", (time.time(), existing['id']))
                    return jsonify(ok=True, id=existing['id'], name=existing['name'])
                return jsonify(ok=True, id=existing["id"], name=existing["name"], already_tracked=True)
        name = str(body.get("name") or (old["name"] if old else info.get("name")) or f"直播间 {info['room_id']}")[:80].strip()
        enabled = int(body.get("enabled", bool(old["enabled"]) if old else True) is True)
        now = time.time()
        if old:
            q("UPDATE live_sources SET archived=0,room_id=?, uid=?, url=?, name=?, enabled=?, settings=?, checked=NULL, error='', updated=? WHERE id=?",
              (info["room_id"], info.get("uid"), info["url"], name, enabled, json.dumps(config, ensure_ascii=False), now, sid))
        else:
            q("INSERT INTO live_sources (owner, room_id, uid, url, name, enabled, settings, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
              (g.owner, info["room_id"], info.get("uid"), info["url"], name, enabled, json.dumps(config, ensure_ascii=False), now, now))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="无法保存，请检查直播链接是否正确，或是否已添加这个直播间"), 400
    return jsonify(ok=True)


@app.post("/api/live/sources/<int:sid>/<action>")
def source_action(sid, action):
    if denied := login_required():
        return denied
    row = owned("live_sources", sid)
    if not row:
        return jsonify(error="追踪不存在"), 404
    if action == "delete":
        q("UPDATE live_sources SET archived=1,enabled=0, live_status='paused', updated=? WHERE id=?", (time.time(), sid))
        # Keep the source attribution for clips and published archives.
    elif action == "check":
        q("UPDATE live_sources SET checked=NULL, error='' WHERE id=?", (sid,))
    elif action == "calibrate":
        board.publish("live_calibrate", f"live-calibration:{sid}", 50, force=True,
                      payload={"source": sid, "title": row["name"]})
    elif action == "refine":
        from .live_calibration import queue_refine
        if not queue_refine(row, force=True):
            return jsonify(error="需要至少 10 对同账号、发布时间和时长接近的样本，以及未用于调优的新验证素材；已有校准任务时请等待完成"), 409
    else:
        return jsonify(error="未知操作"), 400
    return jsonify(ok=True)


@app.post("/api/live/references/<int:rid>/<action>")
def reference_action(rid, action):
    if denied := login_required():
        return denied
    row = owned('live_references', rid)
    if not row:
        return jsonify(error='样本不存在'), 404
    if action in ('exclude', 'include'):
        q('UPDATE live_references SET excluded=?, updated=? WHERE id=?', (int(action=='exclude'), time.time(), rid))
    elif action == 'verify' and row['state'] == 'ready':
        q("UPDATE live_references SET source_match='pending', match_reason='', updated=? WHERE id=?", (time.time(), rid))
    elif action == 'retry' and row['state'] == 'failed':
        board.publish('live_reference', f'live-reference:{rid}', 30, force=True,
                      payload={'reference': rid, 'title': row['title']})
    else:
        return jsonify(error='当前状态不支持此操作'), 409
    return jsonify(ok=True)


@app.post("/api/live/clips")
@app.post("/api/live/clips/<int:cid>")
def save_clip(cid=None):
    if denied := login_required():
        return denied
    row = owned("live_clips", cid) if cid else None
    if cid and not row:
        return jsonify(error="切片不存在"), 404
    if row and row["state"] in ("uploading", "submitted", "uncertain"):
        return jsonify(error="这条切片已进入投稿流程，不能修改成片"), 409
    body = request.get_json(silent=True) or {}
    try:
        session_key = row["session_key"] if row else str(body.get("session") or "")
        session = q("SELECT * FROM live_sessions WHERE key=? AND owner=?", (session_key, g.owner), one=True)
        if not session:
            raise ValueError("请选择一场自己的直播记录")
        start, end = finite(body.get("start"), -1), finite(body.get("end"), -1)
        if not 0 <= start < end or end - start < 5:
            raise ValueError("切片时长应至少为 5 秒")
        title = str(body.get("title") or "").strip()
        if not title or len(title) > 80:
            raise ValueError("标题需为 1–80 字")
        source = q("SELECT * FROM live_sources WHERE id=?", (session["source_id"],), one=True)
        title = bili.publication_title({'title': title, 'origin': session['origin']}, dict(source))
        cover_title = str(body.get('cover_title', row['cover_title'] if row else '') or '').strip()
        if len(cover_title) > 48:
            raise ValueError('封面文案请控制在 48 字以内')
        tags = body.get("tags") or ["直播切片"]
        if not isinstance(tags, list) or not 1 <= len(tags) <= 10 or any(not isinstance(t, str) or not t.strip() or len(t) > 20 or "," in t for t in tags):
            raise ValueError("请填写 1–10 个标签，每个不超过 20 字")
        render_inputs({"session_key": session_key, "owner": g.owner, "start": start, "end": end})
        if row:
            changed = abs(start - row["start"]) > .01 or abs(end - row["end"]) > .01
            # Cancel an upload that was queued but has not started; the edited item needs an explicit publish.
            q("DELETE FROM tasks WHERE kind='live_upload' AND target=? AND state='queued'", (f"live-upload:{cid}:v{row['version']}",))
            q("UPDATE live_clips SET start=?, end=?, title=?, cover_title=?, cover='', description=?, tags=?, version=version+?, "
              "state=CASE WHEN ? THEN 'queued' WHEN file != '' AND render_version=version THEN 'ready' ELSE state END, "
              "needs_review=1, score=CASE WHEN ? THEN 0 ELSE score END, scores=CASE WHEN ? THEN '{}' ELSE scores END, "
              "error='', updated=? WHERE id=? AND state NOT IN ('uploading','submitted','uncertain')",
              (start, end, title, cover_title, str(body.get("description") or "")[:1800], json.dumps(tags, ensure_ascii=False), int(changed),
               int(changed), int(changed), int(changed), time.time(), cid))
            if changed:
                queue_render(cid)
                if row['lane'] == 'topic':
                    from .live_editorial import queue_score
                    queue_score(cid)
        else:
            source = q("SELECT * FROM live_sources WHERE id=?", (session["source_id"],), one=True)
            cid = create_clip(g.owner, source, session_key, "topic", {"start": start, "end": end, "title": title,
                              "description": str(body.get("description") or "")[:1800], "cover_title": cover_title,
                              "tags": tags, "reason": "手动新增", "needs_review": True})
            if not cid:
                raise ValueError("已有覆盖这个时间范围的话题切片，请直接编辑它")
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, id=cid)


@app.post('/api/live/clips/batch')
def batch_clips():
    if denied := login_required():
        return denied
    body = request.get_json(silent=True) or {}
    ids, action = body.get('ids'), body.get('action')
    if (action not in ('reject', 'delete') or not isinstance(ids, list) or not 1 <= len(ids) <= 150
            or any(type(cid) is not int or cid <= 0 for cid in ids)):
        return jsonify(error='请选择片段和有效的批量操作'), 400
    ids = list(dict.fromkeys(ids))
    changed, skipped = [], []
    with core.db_lock:
        try:
            core.DB.execute('BEGIN IMMEDIATE')
            rows = core.DB.execute(f"SELECT * FROM live_clips WHERE owner=? AND removed_at IS NULL AND id IN ({','.join('?' for _ in ids)})", (g.owner, *ids)).fetchall()
            if len(rows) != len(ids):
                core.DB.rollback()
                return jsonify(error='部分片段不存在或不属于当前账号，请刷新后重试'), 404
            for row in rows:
                cid = row['id']
                reason = ''
                if row['state'] in ('awaiting_upload', 'uploading', 'submitted', 'uncertain') or row['bvid'] or row['aid'] or row['submit_started'] is not None:
                    reason = '已进入投稿流程，保留投稿记录'
                elif action == 'delete' and core.DB.execute("SELECT 1 FROM tasks WHERE kind IN ('live_render','live_score','live_upload') AND state='running' AND json_extract(payload,'$.clip')=?", (cid,)).fetchone():
                    reason = '片段正在处理，请稍后删除'
                if reason:
                    skipped.append({'id': cid, 'reason': reason})
                    continue
                if action == 'reject':
                    core.DB.execute("UPDATE live_clips SET state='rejected',updated=? WHERE id=?", (time.time(), cid))
                else:
                    delete_clip_files(cid)
                    core.DB.execute("DELETE FROM tasks WHERE kind IN ('live_render','live_score','live_upload') AND state='queued' AND json_extract(payload,'$.clip')=?", (cid,))
                    if core.DB.execute('SELECT 1 FROM live_clips WHERE merged_into=?', (cid,)).fetchone():
                        # Keep only the association metadata for merged publication history; remove the visible draft and media.
                        core.DB.execute("UPDATE live_clips SET state='rejected',removed_at=?,file='',cover='',updated=? WHERE id=?",
                                        (time.time(),time.time(),cid))
                    else:
                        core.DB.execute('DELETE FROM live_clips WHERE id=?', (cid,))
                changed.append(cid)
            core.DB.commit()
        except BaseException:
            core.DB.rollback()
            raise
    return jsonify(ok=True, changed=changed, skipped=skipped)


@app.post("/api/live/clips/<int:cid>/<action>")
def clip_action(cid, action):
    if denied := login_required():
        return denied
    row = owned("live_clips", cid)
    if not row:
        return jsonify(error="切片不存在"), 404
    try:
        if action == "upload":
            if not queue_upload(cid):
                raise ValueError("请先等待成片制作完成")
        elif action == "reject":
            if row["state"] in ("uploading", "submitted", "uncertain"):
                raise ValueError("请先核实当前投稿结果")
            q("UPDATE live_clips SET state='rejected', updated=? WHERE id=? AND state != 'uploading'", (time.time(), cid))
        elif action == 'score':
            from .live_editorial import queue_score
            queue_score(cid, force=True)
        elif action == "render":
            if row["state"] in ("uploading", "submitted", "uncertain"):
                raise ValueError("这条切片已进入投稿流程")
            q("UPDATE live_clips SET state='queued', updated=? WHERE id=?", (time.time(), cid))
            queue_render(cid)
        elif action == "retry-upload":
            if row["state"] != "uncertain" or (request.get_json(silent=True) or {}).get("confirmed_absent") is not True:
                raise ValueError("先在 B站创作中心确认这条切片没有投稿成功")
            q("UPDATE live_clips SET state='ready', submit_started=NULL, error='' WHERE id=? AND state='uncertain'", (cid,))
            queue_upload(cid)
        elif action == "confirm-upload":
            import re
            bvid = str((request.get_json(silent=True) or {}).get("bvid") or "")
            if row["state"] != "uncertain" or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                raise ValueError("请输入已投稿视频的 BV 号")
            q("UPDATE live_clips SET state='submitted', bvid=?, error='' WHERE id=?", (bvid, cid))
        else:
            raise ValueError("未知操作")
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@app.get("/api/live/clips/<int:cid>/media")
@app.get("/api/live/clips/<int:cid>/cover")
def clip_media(cid):
    row = owned("live_clips", cid)
    if not row:
        return "", 404
    try:
        path = media_path(row["cover"] if request.path.endswith("/cover") else row["file"])
        if row["version"] != row["render_version"]:
            return "", 409
    except ValueError:
        return "", 404
    return send_file(path, conditional=True, max_age=0)


@app.get("/api/live/recordings/<int:rid>/media")
def recording_media(rid):
    row = owned("live_recordings", rid)
    if not row:
        return "", 404
    try:
        path = media_path(row["path"])
    except ValueError:
        return "", 404
    # Browser-friendly remux; recordings use MKV so a power cut doesn't destroy the whole file.
    return remux([path], mp4=True)


def remux(paths, mp4=False):
    listing = concat_list(paths)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                             "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                             *(["-movflags", "frag_keyframe+empty_moov"] if mp4 else []),
                             "-f", "mp4" if mp4 else "matroska", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def chunks():
        try:
            while data := proc.stdout.read(1 << 16):
                yield data
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(); listing.unlink(missing_ok=True)
    return Response(chunks(), mimetype="video/mp4" if mp4 else "video/x-matroska")


@app.get("/api/tasks/<int:tid>/live-source")
def remote_source(tid):
    from .web import compute_auth, my_task
    if denied := compute_auth():
        return denied
    task = my_task(tid)
    if not task or task["kind"] != "live_render":
        return "", 404
    p = task["payload"]
    row = q("SELECT * FROM live_clips WHERE id=? AND version=?", (p["clip"], p["version"]), one=True)
    if not row or row["state"] not in ("queued", "rendering"):
        return "", 409
    try:
        paths, _ = render_inputs(row)
    except ValueError:
        return "", 404
    q("UPDATE live_clips SET state='rendering', updated=? WHERE id=? AND version=?", (time.time(), row["id"], row["version"]))
    return remux(paths)


@app.post("/api/tasks/<int:tid>/live-result")
def remote_result(tid):
    from .web import compute_auth, my_task, _claim_identity, _worker
    if denied := compute_auth():
        return denied
    try:
        token = _claim_identity({"claim_token": request.args.get("claim_token")})["claim_token"]
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    task = my_task(tid)
    if not task or task["kind"] != "live_render":
        return "", 404
    if token is not None and token != task.get("claim_token"):
        return jsonify(error="这次制作任务已被重新领取"), 409
    request.max_content_length = 2 << 30
    p = task["payload"]
    row = q("SELECT * FROM live_clips WHERE id=? AND version=?", (p["clip"], p["version"]), one=True)
    if not row or row["state"] not in ("queued", "rendering"):
        return "", 409
    uploaded = request.files.get("video")
    if not uploaded:
        return jsonify(error="缺少成片"), 400
    destination = workspace() / "clips" / f"{row['id']}-v{row['version']}.mp4"
    destination.parent.mkdir(exist_ok=True)
    # Two attempts can overlap while uploading. They must never write the same temporary file.
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.stem}-",
                                     suffix=".remote.partial.mp4", delete=False) as output:
        temporary = Path(output.name)
    try:
        uploaded.save(temporary)
        duration = library.duration_of(str(temporary)) or 0
        if abs(duration - (row["end"] - row["start"])) > 3:
            return jsonify(error="成片时长与切片范围不符"), 400
        # Validation/upload may take minutes; serialize the final rename with task reclamation
        # and clip edits, checking the exact claim captured before accepting the upload.
        with core.db_lock:
            try:
                core.DB.execute("BEGIN IMMEDIATE")
                current = core.DB.execute(
                    "SELECT 1 FROM tasks WHERE id=? AND worker=? AND kind='live_render' AND state='running' "
                    "AND attempts=? AND json_extract(payload,'$._claim_token') IS ? AND lease_until>?",
                    (tid, _worker(), task["attempt"], task.get("claim_token"), time.time())).fetchone()
                clip = core.DB.execute(
                    "SELECT 1 FROM live_clips WHERE id=? AND version=? AND state IN ('queued','rendering')",
                    (row["id"], row["version"])).fetchone()
                if not current or not clip:
                    core.DB.rollback()
                    return jsonify(error="任务或切片已更新，本次成片未保存"), 409
                temporary.replace(destination)
                core.DB.commit()
            except BaseException:
                core.DB.rollback()
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return jsonify(ok=True)


@app.post("/api/live/login/qr")
def login_qr():
    if denied := login_required():
        return denied
    try:
        import qrcode
        body = bili.tv_request("auth_code")
        if body.get("code") != 0 or not (body.get("data") or {}).get("url"):
            raise ValueError("B站没有返回二维码")
        data = body["data"]
        image = qrcode.make(data["url"]); output = BytesIO(); image.save(output, format="PNG")
        token = secrets.token_urlsafe(24)
        now = time.time()
        for key in list(LOGINS):
            if LOGINS[key]["expires"] < now or LOGINS[key]["owner"] == g.owner:
                LOGINS.pop(key, None)
        LOGINS[token] = {"owner": g.owner, "auth_code": data["auth_code"], "expires": now + 180}
        return jsonify(login=token, qr="data:image/png;base64," + base64.b64encode(output.getvalue()).decode())
    except Exception:
        return jsonify(error="B站登录二维码暂时不可用，请稍后重试"), 502


@app.get("/api/live/login/qr/<token>")
def login_poll(token):
    if denied := login_required():
        return denied
    item = LOGINS.get(token)
    if not item or item["owner"] != g.owner or item["expires"] < time.time():
        return jsonify(status="expired")
    try:
        body = {"code": 0, "data": item["credentials"]} if "credentials" in item else bili.tv_request("poll", auth_code=item["auth_code"])
        if body.get("code") in (86039, 86101):
            return jsonify(status="waiting")
        if body.get("code") == 86090:
            return jsonify(status="scanned")
        if body.get("code") == 86038:
            LOGINS.pop(token, None)
            return jsonify(status="expired")
        if body.get("code") != 0:
            return jsonify(error=f"B站登录状态查询暂时失败（{body.get('code')}），正在重试"), 502
        item["credentials"] = body["data"]
        item["expires"] = time.time() + 600
        mid = bili.save_account(g.owner, body["data"])
        info = bili.api(bili.client(g.owner), "https://api.bilibili.com/x/web-interface/nav")
        q("INSERT INTO live_accounts (owner, mid, name, status, updated) VALUES (?,?,?,'ready',?) "
          "ON CONFLICT(owner) DO UPDATE SET mid=excluded.mid, name=excluded.name, status='ready', updated=excluded.updated",
          (g.owner, mid, str(info.get("uname") or mid), time.time()))
        LOGINS.pop(token, None)
        return jsonify(status="done")
    except ValueError as exc:
        return jsonify(error=str(exc)), 502
    except Exception as exc:
        app.logger.warning("Live login finalization failed: %s", type(exc).__name__)
        return jsonify(error="登录确认暂未完成，正在重试，无需重新扫码"), 502


@app.post("/api/live/account/logout")
def account_logout():
    if denied := login_required():
        return denied
    if q("SELECT id FROM live_clips WHERE owner=? AND state='uploading'", (g.owner,), one=True):
        return jsonify(error="请等待当前投稿结束"), 409
    bili.account_path(g.owner).unlink(missing_ok=True)
    q("DELETE FROM live_accounts WHERE owner=?", (g.owner,))
    return jsonify(ok=True)
