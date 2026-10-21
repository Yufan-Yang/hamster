"""Continuous one-minute recording, independent of transcription, curation and upload."""
import csv
import fcntl
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import board, core, library, live
from .core import q
from . import live_bilibili as bili
from . import live_events


MAX_STREAMS = max(1, int(os.environ.get("LIVE_MAX_STREAMS", "3")))
BUFFER_HOURS = max(1, int(os.environ.get("LIVE_BUFFER_HOURS", "12")))
BUFFER_GB = max(1, int(os.environ.get("LIVE_BUFFER_GB", "30")))
RESERVE = 10 << 30
ACTIVE = {}
STOP = threading.Event()


def register_segment(session, path, seq, offset):
    duration = library.duration_of(str(path)) or 0
    if duration < 1:
        return
    now = time.time()
    q("INSERT OR IGNORE INTO live_recordings (owner, source_id, session_key, seq, offset, duration, path, created, updated) "
      "VALUES (?,?,?,?,?,?,?,?,?)", (session["owner"], session["source_id"], session["key"], seq, offset, duration, str(path), now, now))
    row = q("SELECT * FROM live_recordings WHERE session_key=? AND seq=?", (session["key"], seq), one=True)
    q("UPDATE live_recordings SET bytes=? WHERE id=?", (path.stat().st_size, row["id"]))
    board.publish("live_transcribe", f"live-recording:{row['id']}", 98,
                  payload={"recording": row["id"], "duration": duration, "language": "zh", "title": session["title"]})


def ingest_closed(session, directory):
    listing = directory / "segments.csv"
    if not listing.exists():
        return
    with listing.open(newline="") as file:
        for row in csv.reader(file):
            if len(row) != 3:
                continue
            name = Path(row[0]).name
            if not name.startswith("chunk-") or not name.endswith(".mkv"):
                continue
            try:
                seq = int(name[6:-4]); offset = float(row[1])
            except ValueError:
                continue
            if q("SELECT id FROM live_recordings WHERE session_key=? AND seq=?", (session["key"], seq), one=True):
                continue
            path = directory / name
            if path.is_file():
                register_segment(session, path, seq, offset)


def capture(source, info):
    key = secrets.token_hex(16)
    now = time.time()
    directory = live.workspace() / "recordings" / key
    directory.mkdir(parents=True, mode=0o700)
    origin = {k: source[k] for k in ('name', 'room_id', 'url', 'uid')}
    origin['live_start'] = info.get('live_start') or ''
    q("INSERT INTO live_sessions (key, source_id, owner, title, started, origin) VALUES (?,?,?,?,?,?)",
      (key, source["id"], source["owner"], info["title"], now, json.dumps(origin, ensure_ascii=False)))
    session = {"key": key, "source_id": source["id"], "owner": source["owner"], "title": info["title"]}
    proc = None
    error = ""
    stopped = False
    try:
        url = bili.stream_url(source["room_id"], source["owner"])
        command = ["ffmpeg", "-v", "error", "-rw_timeout", "20000000", "-user_agent", core.UA,
                   "-referer", source["url"], "-i", url, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                   "-f", "segment", "-segment_time", str(live.SEGMENT_SECONDS), "-reset_timestamps", "1",
                   "-segment_list", str(directory / "segments.csv"), "-segment_list_type", "csv",
                   "-segment_format", "matroska", str(directory / "chunk-%06d.mkv")]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        q("UPDATE live_sources SET live_status='recording', error='', updated=? WHERE id=?", (time.time(), source["id"]))
        while proc.poll() is None and not STOP.wait(3):
            ingest_closed(session, directory)
            current = q("SELECT enabled FROM live_sources WHERE id=?", (source["id"],), one=True)
            if not current or not current["enabled"]:
                stopped = True
                break
            if shutil.disk_usage(live.ROOT).free < RESERVE:
                error = "剩余空间不足 10GiB，已暂停录制"
                break
            if buffer_bytes() > BUFFER_GB * (1 << 30):
                cleanup(force=True)
                if buffer_bytes() > BUFFER_GB * (1 << 30):
                    error = "直播缓存已达上限，等待分析或清理后再录制"
                    break
            if time.time() - now > 6 * 3600:
                break  # refresh expiring live URLs without waiting for the stream to end
        if proc.poll() not in (None, 0):
            error = "直播流结束或连接中断，稍后自动重连"
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else "直播连接失败，稍后重试"
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        ingest_closed(session, directory)
        status = 'ended'
        retry_delay = 60
        if not stopped:
            # ffmpeg can exit cleanly when a live connection expires; that does not prove the UP went offline.
            try:
                current_info = bili.room_info(source['url'], source['owner'])
                if current_info['live']:
                    status = 'interrupted'
                    error = error or '录制连接结束，主播仍在直播，等待重新连接'
                    # A healthy connection expiring should reconnect promptly; repeated short failures back off.
                    if time.time() - now >= 30:
                        retry_delay = 5
            except Exception:
                status = 'interrupted'
                error = error or '录制连接结束，暂未确认主播是否下播'
        q("UPDATE live_sessions SET status=?, ended=?, error=? WHERE key=?", (status, time.time(), error, key))
        q("UPDATE live_sources SET live_status=?, error=?, updated=? WHERE id=?",
          ("waiting" if not error else "error", error, time.time(), source["id"]))
        live_events.request_check(source, "recording-ended", delay=retry_delay)
        ACTIVE.pop(source["id"], None)


def recover():
    for row in q("SELECT * FROM live_sessions WHERE status='recording'"):
        directory = live.ROOT / "recordings" / row["key"]
        if directory.is_dir():
            ingest_closed(dict(row), directory)
            known = {r["seq"] for r in q("SELECT seq FROM live_recordings WHERE session_key=?", (row["key"],))}
            # A final MKV is playable even if the Pi stopped before the CSV line was flushed.
            for path in sorted(directory.glob("chunk-*.mkv")):
                try:
                    seq = int(path.stem[6:])
                except ValueError:
                    continue
                if seq not in known:
                    register_segment(dict(row), path, seq, seq * live.SEGMENT_SECONDS)
        q("UPDATE live_sessions SET status='interrupted', ended=? WHERE key=?", (time.time(), row["key"]))
    q("UPDATE live_clips SET state='uncertain', error='服务重启，请在 B站创作中心核实投稿结果', updated=? WHERE state='uploading'",
      (time.time(),))


def schedule_analysis():
    # Only the current revision can fail a clip. Keep the check and update atomic with worker completions/edits.
    for table, field, kind, error in (
            ("live_recordings", "recording", "live_transcribe", "转写失败，将跳过该分段并继续分析"),
            ("live_clips", "clip", "live_render", "处理失败，可在页面重试")):
        version = " AND json_extract(t.payload, '$.version') = x.version" if table == "live_clips" else ""
        q(f"UPDATE {table} AS x SET state='failed', error=?, updated=? "
          "WHERE x.state IN ('transcribing','queued','rendering') AND EXISTS "
          "(SELECT 1 FROM tasks t WHERE t.kind=? AND t.state='failed' AND json_extract(t.payload, ?) = x.id"
          + version + ")", (error, time.time(), kind, "$." + field))
    for session in q("SELECT s.* FROM live_sessions s WHERE EXISTS (SELECT 1 FROM live_recordings r "
                     "WHERE r.session_key=s.key AND r.state IN ('ready','failed','expired') AND r.offset+r.duration > s.cursor+0.5)"):
        next_row = q("SELECT state FROM live_recordings WHERE session_key=? AND offset+duration > ? ORDER BY seq LIMIT 1",
                     (session["key"], session["cursor"] + .5), one=True)
        if next_row and next_row["state"] in ("ready", "failed", "expired"):
            board.publish("live_analyze", f"live-session:{session['key']}", 95, force=True,
                          payload={"session": session["key"], "title": session["title"]})
    for session in q("SELECT * FROM live_sessions s WHERE status != 'recording' AND cursor-topic_start >= 30 "
                     "AND NOT EXISTS (SELECT 1 FROM live_recordings r WHERE r.session_key=s.key AND r.offset+r.duration > s.cursor+1)"):
        source = q("SELECT * FROM live_sources WHERE id=?", (session["source_id"],), one=True)
        if source:
            live.create_clip(session["owner"], source, session["key"], "topic",
                             {"start": session["topic_start"], "end": session["cursor"],
                              "title": session["topic_title"] or session["title"],
                              "content_kind": session['topic_kind'], "description": session['topic_summary'],
                              "reason": "录制停止，保留最后一个话题"})
            q("UPDATE live_sessions SET topic_start=cursor WHERE key=?", (session["key"],))


def buffer_bytes():
    return q("SELECT COALESCE(SUM(bytes), 0) n FROM live_recordings WHERE state != 'expired'", one=True)["n"]


def cleanup(force=False):
    cutoff = time.time() if force else time.time() - BUFFER_HOURS * 3600
    for row in q("SELECT r.id, r.path FROM live_recordings r JOIN live_sessions s ON s.key=r.session_key "
                 "WHERE r.created < ? AND r.state='ready' AND r.offset+r.duration < s.cursor-600 "
                 "AND r.offset+r.duration <= s.topic_start "
                 "AND NOT EXISTS (SELECT 1 FROM live_clips c WHERE c.session_key=r.session_key "
                 "AND c.start < r.offset+r.duration AND c.end > r.offset AND c.state IN ('queued','rendering','failed'))", (cutoff,)):
        try:
            live.media_path(row["path"]).unlink()
        except ValueError:
            pass
        q("UPDATE live_recordings SET state='expired', segments='[]', updated=? WHERE id=?", (time.time(), row["id"]))


def check_sources(sources, now):
    checked = 0
    for source in sources:
        if not source["enabled"] or source["id"] in ACTIVE:
            continue
        request = live_events.check_request(source)
        if not live_events.check_due(source, now, request):
            continue
        if checked >= 10:
            break
        checked += 1
        live_events.consume_check(source, request)
        q("UPDATE live_sources SET checked=? WHERE id=?", (now, source["id"]))
        try:
            info = bili.room_info(source["url"], source["owner"])
            current = q("SELECT enabled, room_id FROM live_sources WHERE id=?", (source["id"],), one=True)
            if not current or not current["enabled"] or current["room_id"] != source["room_id"]:
                continue
            q("UPDATE live_sources SET live_status=?, error='' WHERE id=?", ("live" if info["live"] else "offline", source["id"]))
            if not info["live"]:
                continue
            error = ""
            if len(ACTIVE) >= MAX_STREAMS:
                error = "同时录制名额已满，等待空位后自动录制"
            elif shutil.disk_usage(live.ROOT).free < RESERVE:
                error = "剩余空间不足，暂不录制"
            elif buffer_bytes() > BUFFER_GB * (1 << 30):
                error = "直播缓存已达上限，请清理缓存或等待处理"
            if error:
                q("UPDATE live_sources SET error=? WHERE id=?", (error, source["id"]))
                live_events.request_check(source, "recording-blocked", delay=60)
                continue
            thread = threading.Thread(target=capture, args=(dict(source), info), daemon=True)
            ACTIVE[source["id"]] = thread
            thread.start()
        except Exception as exc:
            q("UPDATE live_sources SET live_status='error', error=? WHERE id=?",
              (str(exc) if isinstance(exc, ValueError) else "直播状态查询失败，稍后重试", source["id"]))
            live_events.request_check(source, "query-failed", delay=300)


def loop(wait_for_lock=False):
    lock = open(core.STATE / "live-recorder.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait_for_lock else fcntl.LOCK_NB))
    except BlockingIOError:
        lock.close(); return
    live.workspace()
    recover()
    # Confirm current state on worker startup rather than waiting for the next half-hour check.
    q("UPDATE live_sources SET checked=NULL WHERE enabled=1")
    subscriptions = live_events.Subscriptions()
    housekeeping = 0
    while not STOP.wait(5):
        try:
            schedule_analysis()
            for row in q("SELECT id FROM live_clips WHERE state='ready' ORDER BY CASE lane WHEN 'highlight' THEN 0 ELSE 1 END, id"):
                live.queue_upload(row["id"], automatic=True)
            now = time.time()
            if now - housekeeping >= 60:
                housekeeping = now
                cleanup()
                from . import live_calibration
                live_calibration.tick()
            sources = q("SELECT * FROM live_sources WHERE enabled=1 ORDER BY checked")
            subscriptions.sync(sources)
            check_sources(sources, now)
        except Exception:
            traceback.print_exc()
    subscriptions.close()
