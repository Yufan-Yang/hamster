"""A download job from link to library, the worker that runs jobs, job control."""
import glob
import hashlib
import json
import os
import re
import requests
import shutil
import subprocess
import sys
import threading
import time
import traceback

from pathlib import Path
from .core import (AUDIO_EXT, Cancelled, DOWNLOAD_WORKERS, ENTRY, INCOMPLETE, LLM_API_KEY, MEDIA, SUB_EXT, UA, VIDEO_EXT, bell_mark, bell_wait, check_cancel, db_lock, ffprobe, finishing_touch, human, job_dict, link_key, log_usage, q, ring, safe_name, unique_path, update)


SHARED_FIELDS = ("status", "kind", "title", "files", "thumb", "analysis", "error", "transcript", "progress")


def add_job(url, source="web", chat_id=None, msg_id=None, owner=None, device=None):
    return add_job_ex(url, source, chat_id, msg_id, owner, device)[0]


def add_job_ex(url, source="web", chat_id=None, msg_id=None, owner=None, device=None):
    """Queue a link. Returns (job id, how): "new", "duplicate" when this owner already has it queued,
    downloading or downloaded (the existing job is returned), or "linked" when someone else already has it:
    then this owner gets their own entry that points at the same files instead of a second download."""
    url = url.strip()
    key = link_key(url)
    existing = q("SELECT id FROM jobs WHERE key=? AND owner IS ? AND status NOT IN ('failed','cancelled') "
                 "ORDER BY id DESC LIMIT 1", (key, owner), one=True)
    if existing:
        return existing["id"], "duplicate"
    source_job = q("SELECT * FROM jobs WHERE key=? AND ref IS NULL AND status IN ('queued','downloading','processing','done') "
                   "ORDER BY status='done' DESC, id DESC LIMIT 1", (key,), one=True)
    with db_lock:
        cur = core.DB.execute("INSERT INTO jobs (url, key, source, chat_id, msg_id, owner, device, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (url, key, source, chat_id, msg_id, owner, device, time.time(), time.time()))
        core.DB.commit()
        job_id = cur.lastrowid
    if source_job:
        if source_job["status"] == "done":
            # Already downloaded: copy the result over (same files on disk)
            update(job_id, ref=source_job["id"], **{f: source_job[f] for f in SHARED_FIELDS})
        else:
            # Still downloading: wait for it; finish_links() fills this in when it's done
            update(job_id, ref=source_job["id"], status="linked")
        return job_id, "linked"
    ring("jobs")
    return job_id, "new"


def finish_links(source_id):
    """A job finished, failed or was cancelled: update the entries other accounts linked to it."""
    src = q("SELECT * FROM jobs WHERE id=?", (source_id,), one=True)
    if not src:
        return
    if src["status"] == "failed" and src["retry_at"]:
        return  # the original retries by itself; entries linked to it keep waiting for that
    for r in q("SELECT id FROM jobs WHERE ref=? AND status='linked'", (source_id,)):
        if src["status"] in ("done", "failed"):
            update(r["id"], **{f: src[f] for f in SHARED_FIELDS})
        else:  # the original was cancelled: download it for this account after all
            update(r["id"], ref=None, status="queued", progress=0)
            ring("jobs")


# ---------------------------------------------------------------- B站 joint videos

def bili_staff(url):
    """Names of everyone credited on a B站 joint video (创作团队: UP主, 参演 ...), [] otherwise."""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    if not m:
        return []
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": m.group(1)}, timeout=15,
                         headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        return [s["name"] for s in (r.json().get("data") or {}).get("staff") or [] if s.get("name")]
    except Exception:
        return []


def add_people_tags(a, names):
    """Everyone in it is a tag right after the creator, like the creator is."""
    tags = a.setdefault("tags", [])
    at = 1 if tags and tags[0] == a.get("creator") else 0
    for name in names:
        if name not in tags:
            tags.insert(at, name)
            at += 1


# ---------------------------------------------------------------- the pipeline

def process(job_id):
    job = job_dict(q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True))
    url = job["url"]
    workdir = INCOMPLETE / str(job_id)
    # Keep what's already there: after a restart or a failure, yt-dlp and aria2 resume their partial downloads.
    # (Cancelled and removed jobs clean up their folder.)
    workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o2775)
    update(job_id, status="downloading", stage="starting", progress=0, error="")
    meta = {}

    # 1. download
    if url.startswith("magnet:") or url.endswith(".torrent") or url.startswith("torrent-file:"):
        kind = "torrent"
        update(job_id, kind=kind, stage="downloading torrent")
        src = url[len("torrent-file:"):] if url.startswith("torrent-file:") else url
        download.aria2(job_id, ["--dir", str(workdir), "--seed-time=0", "--bt-stop-timeout=1800", "--follow-torrent=mem",
                       "--bt-remove-unselected-file=true", "--enable-dht=true", "--bt-enable-lpd=true", src], as_bt=True)
        meta["source"] = "torrent"
    else:
        info = download.ytdlp_probe(url)
        if info:
            kind = "video"
            update(job_id, kind=kind, title=info.get("title") or "")
            meta = download.ytdlp_download(job_id, url, workdir, info.get("_net"))
            meta["source"] = "ytdlp"
        elif download.head_is_file(url):
            kind = "file"
            update(job_id, kind=kind, stage="downloading file")
            download.aria2(job_id, ["--dir", str(workdir), "-x", "8", "-s", "8", "--user-agent", UA,
                           "--content-disposition-default-utf8=true", url])
            meta["source"] = "file"
        else:
            kind = "video"
            update(job_id, kind=kind)
            found = download.sniff_page(job_id, url)
            if not found:
                raise RuntimeError("No video found on that page")
            check_cancel(job_id)
            update(job_id, title=found["title"])
            urls = found["media_urls"]
            try:
                for i, media_url in enumerate(urls, 1):
                    suffix = f" {i:02}" if len(urls) > 1 else ""
                    extra = {"cookiefile": found["cookiefile"], "http_headers": {"Referer": url, "User-Agent": UA},
                             "outtmpl": str(workdir / f"{safe_name(found['title'])}{suffix}.%(ext)s")}
                    if len(urls) > 1:
                        update(job_id, stage=f"downloading video {i}/{len(urls)}")
                    meta = download.ytdlp_download(job_id, media_url, workdir, extra)
            finally:
                Path(found["cookiefile"]).unlink(missing_ok=True)
            meta.update(title=found["title"], webpage_url=url, source="sniffed")

    # 2. post-process each media file
    check_cancel(job_id)
    update(job_id, status="processing", stage="inspecting", progress=0, speed="")
    files = sorted((p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but produced no files")
    media = [p for p in files if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT
             and (kind != "torrent" or p.suffix.lower() in AUDIO_EXT  # album tracks are small; only videos get the sample filter
                  or ("sample" not in p.name.lower() and p.stat().st_size > 30 << 20))]

    from guessit import guessit
    results, final_files, plex_items = [], [], []
    thumb_url = ""
    for idx, m in enumerate(media):
        check_cancel(job_id)
        if m.suffix.lower() in VIDEO_EXT:
            started, before = time.time(), m
            m = library.make_plex_friendly(job_id, m, keep_mkv=kind == "torrent")
            if m != before:
                log_usage("encode", "plex", job_id, amount=float(ffprobe(m).get("format", {}).get("duration") or 0),
                          seconds=time.time() - started)
        guess = dict(guessit(m.name)) if kind in ("torrent", "file") else {}
        if m.suffix.lower() in VIDEO_EXT and not any(m.parent.glob(glob.escape(m.stem) + ".jpg")):
            library.grab_frame(m, m.with_suffix(".jpg"))
        if results and not (guess.get("type") == "episode" and results[0].get("library") == "TV"):
            # Extra videos from the same page/torrent: same filing, their own names, no more AI calls
            a = {**results[0], "title": re.sub(r" \[[\w-]+\]$", "", m.stem), "usage": {}}
        elif results:
            # Next episode of a season pack: reuse the show, take the numbers from the file name
            ep = guess.get("episode")
            a = {**results[0], "season": guess.get("season") or results[0].get("season"),
                 "episode": ep if isinstance(ep, int) else (ep or [None])[0], "usage": {}}
        else:
            staff = bili_staff(meta.get("webpage_url") or url)  # B站 joint videos list everyone who's in them
            a = llm.classify(job_id, m.name, {**meta, "file": m.name, "size": human(m.stat().st_size),
                                          "duration_s": float(ffprobe(m).get("format", {}).get("duration") or 0),
                                          **({"people_in_it": staff} if staff else {})},
                         guess)
            add_people_tags(a, staff)
            if re.fullmatch(r"\d{8}", str(meta.get("upload_date") or "")):  # when it came out (for 追更周报)
                d = meta["upload_date"]
                a["published"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            subs = sorted(m.parent.glob(glob.escape(m.stem) + "*.srt"))
            transcript, note = (library.srt_to_text(subs[0]), f"(from subtitles {subs[0].name[len(m.stem):]})") if subs else (None, "")
            # Subtitles that came with it: summarise now (when the classifier says it's worth it). Otherwise the
            # task board makes them (transcribe -> save_subs -> summarize), on the Mac when it's there
            if transcript and a.get("needs_transcript") and LLM_API_KEY:
                a = llm.summarize(job_id, a, transcript, note)
            elif not transcript and a.get("needs_transcript"):
                a["note"] = "summarised once subtitles have been made"
            if transcript:
                update(job_id, transcript=transcript[:200_000])
        a.setdefault("tags", [])
        dest_dir, base = library.destination(a, m.suffix.lower(), m.stem)
        if dest_dir.parent.name == "Videos":
            a["library"] = "Videos"  # e.g. a "movie" without a year can't go in Movies
            a["folder"] = dest_dir.name
        update(job_id, stage="filing into library")
        moved = library.move_with_sidecars(m, dest_dir, base)
        final_files += [str(p) for p in moved]
        plex_items.append((moved[0], a))
        jpg = next((p for p in moved if p.suffix == ".jpg"), None)
        if jpg and not thumb_url:
            thumb_url = str(jpg)
        results.append(a)

    # Leftovers (non-media torrents/files, extras) go to Downloads as-is
    leftovers = [p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")]
    if leftovers and (not media or kind in ("torrent", "file")):
        root_items = list(workdir.iterdir())
        for item in root_items:
            if item.name.endswith(".aria2"):
                continue
            if not media or any(p.suffix.lower() not in SUB_EXT | {".jpg", ".nfo", ".txt"} for p in
                                ([item] if item.is_file() else item.rglob("*"))):
                target = unique_path(MEDIA / "Downloads" / item.name)
                shutil.move(str(item), str(target))
                final_files.append(str(target))
    shutil.rmtree(workdir, ignore_errors=True)

    analysis = results[0] if results else {"title": meta.get("title") or url, "summary": "", "library": "Downloads"}
    if len(results) > 1:
        analysis = {**results[0], "episodes": len(results)}
    update(job_id, status="done", stage="", progress=100, files=final_files, analysis=analysis,
           title=analysis.get("title") or job["title"], thumb=thumb_url)
    log_usage("download", kind, job_id, amount=sum(Path(f).stat().st_size for f in final_files if Path(f).exists()))
    try:  # its slow work goes on the task board: links you sent first, older videos of followed uploaders last
        board.publish_job_work(job_id, 10 if job.get("backfill") else 50, force=True)
    except Exception:
        traceback.print_exc()
    for f in final_files:  # so the list doesn't have to ffprobe it later
        if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT:
            library.probe_of(f)
    library.plex_refresh()
    finishing_touch(library.plex_set_metadata, plex_items)
    finishing_touch(llm.merge_tags_if_new)
    for f in final_files:
        if Path(f).suffix.lower() in (VIDEO_EXT | AUDIO_EXT) - library.BROWSER_DIRECT:
            finishing_touch(library.remux_for_browser, Path(f))


def run_job(job_id):
    try:
        process(job_id)
    except Cancelled:
        update(job_id, status="cancelled", stage="", speed="")
        shutil.rmtree(INCOMPLETE / str(job_id), ignore_errors=True)
    except Exception as e:
        traceback.print_exc()
        # Keep the partial download so a retry continues where it stopped. Network trouble is retried
        # automatically a few times (1, 5, then 15 minutes later); errors that won't fix themselves aren't.
        attempts = (q("SELECT attempts FROM jobs WHERE id=?", (job_id,), one=True)["attempts"] or 0) + 1
        permanent = re.search(r"No video found|Unsupported URL|no longer supported|produced no files|404|"
                              r"Private video|removed|not available", str(e), re.I)
        # Being taken for a bot passes after a while: wait longer and try more often
        delays = (1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600) if download.BOT_CHECK.search(str(e)) or "机器人" in str(e) else (60, 300, 900)
        if attempts <= len(delays) and not permanent:
            delay = delays[attempts - 1]
            wait = f"{delay // 3600} 小时" if delay >= 3600 else f"{delay // 60} 分钟"
            update(job_id, status="failed", stage="", speed="", attempts=attempts, retry_at=time.time() + delay,
                   error=f"{str(e)[:900]}\n（{wait}后自动重试，第 {attempts}/{len(delays)} 次，已下载的部分会保留）")
        else:
            update(job_id, status="failed", stage="", speed="", attempts=attempts, retry_at=None, error=str(e)[:1000],
                   advice="")
            if LLM_API_KEY:  # failed for good: why, in plain words, and what to do
                board.publish("explain_failure", f"job:{job_id}", 50, force=True)
    finally:
        finish_links(job_id)
        ring("jobs", "tasks")  # a download slot is free; the CPU may be (the Pi's idle work waits for that)
        telegram.notify(job_id)


finishing = []  # job processes past their final status, still doing finishing touches
claim_lock = threading.Lock()


def worker_loop():
    while True:
        mark = bell_mark("jobs")
        with claim_lock:
            # failed jobs whose automatic retry is due go back in the queue
            if q("SELECT 1 FROM jobs WHERE status='failed' AND retry_at IS NOT NULL AND retry_at<=? LIMIT 1", (time.time(),), one=True):
                q("UPDATE jobs SET status='queued', retry_at=NULL WHERE status='failed' AND retry_at IS NOT NULL AND retry_at<=?",
                  (time.time(),))
            # links you send go first; followed channels' videos take at most SUB_WORKERS slots, newest first
            # (a job parked waiting for the speech-to-text/encoder slot doesn't count, so downloads keep going
            # meanwhile; one worker always stays free for links you send)
            row = q("SELECT id FROM jobs WHERE status='queued' AND (COALESCE(source,'') NOT LIKE 'sub:%' OR "
                    "((SELECT COUNT(*) FROM jobs WHERE status IN ('downloading','processing') AND source LIKE 'sub:%' "
                    "  AND COALESCE(speed,'') != '排队等资源') < ? AND "
                    " (SELECT COUNT(*) FROM jobs WHERE status IN ('downloading','processing') AND source LIKE 'sub:%') < ?)) "
                    "ORDER BY COALESCE(source,'') LIKE 'sub:%', CASE WHEN source LIKE 'sub:%' THEN -id ELSE id END LIMIT 1",
                    (channels.SUB_WORKERS, DOWNLOAD_WORKERS - 1), one=True)
            if row:
                update(row["id"], status="downloading", stage="starting", cancel=0)
        if not row:
            bell_wait("jobs", mark, 60)  # a new job rings; a due automatic retry is found within the minute
            continue
        jid = row["id"]
        # Each job in its own lower-priority process: it gets its own CPU core and can't slow the page
        proc = subprocess.Popen(["nice", "-n", "10", sys.executable, ENTRY, "run-job", str(jid)])
        # Once the job is done/failed its process may still be busy for minutes with finishing touches (Plex
        # metadata, tag tidy-up, browser copy); don't hold a download slot for that, reap it later
        while proc.poll() is None:
            time.sleep(3)
            if (q("SELECT status FROM jobs WHERE id=?", (jid,), one=True) or {"status": None})["status"] \
                    not in ("downloading", "processing"):
                finishing.append(proc)
                break
        with claim_lock:
            finishing[:] = [p for p in finishing if p.poll() is None]
        status = (q("SELECT status FROM jobs WHERE id=?", (jid,), one=True) or {"status": None})["status"]
        if proc.poll() is not None and proc.returncode != 0 and status in ("downloading", "processing"):
            update(jid, status="failed", stage="", speed="", error=f"任务进程意外退出（代码 {proc.returncode}）")
            finish_links(jid)


# ---------------------------------------------------------------- job control

def cancel_job(jid):
    row = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not row:
        return f"No job #{jid}"
    if row["status"] in ("queued", "linked"):
        update(jid, status="cancelled")
        finish_links(jid)
    elif row["status"] in ("downloading", "processing"):
        update(jid, cancel=1)  # the job process sees this within a couple of seconds
    else:
        return f"#{jid} is already {row['status']}"
    return f"Cancelling #{jid}"


def retry_job(jid):
    row = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] not in ("failed", "cancelled"):
        return f"#{jid} can't be retried"
    update(jid, status="queued", error="", progress=0, stage="", ref=None, attempts=0, retry_at=None, cancel=0)
    return f"Retrying #{jid}"


def delete_job_files(row):
    """Delete a job's files from disk: the media, its subtitles/cover, and the browser copy.
    Only paths inside the library are touched; empty folders it leaves behind are removed."""
    removed = 0
    for f in json.loads(row["files"] or "[]"):
        p = Path(f)
        try:
            p.resolve().relative_to(MEDIA.resolve())
        except ValueError:
            continue
        if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT and p.exists():
            st = p.stat()
            (library.PLAY_CACHE / f"{hashlib.sha1(f'{p}:{st.st_mtime}'.encode()).hexdigest()}.mp4").unlink(missing_ok=True)
            # subtitles made after the download (speech-to-text, translation) aren't in the job's file list
            for side in p.parent.glob(glob.escape(p.stem) + ".*"):
                if side.suffix.lower() in SUB_EXT and side.is_file():
                    side.unlink()
                    removed += 1
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        elif p.exists():
            p.unlink()
            removed += 1
        # Tidy empty folders, but keep the library's own folders (Movies, TV, Videos/Talks...)
        parent = p.parent
        while (parent != MEDIA and parent.parent != MEDIA and parent.parent != MEDIA / "Videos"
               and parent.exists() and not any(parent.iterdir())):
            parent.rmdir()
            parent = parent.parent
    return removed


def sweep_orphans():
    """Covers and subtitles left behind when a video was deleted some other way (Plex, Samba, Finder).
    Only looks in Videos/, which this app manages; Movies/TV/Music may hold Plex's own artwork."""
    removed = []
    for folder in [p for p in (MEDIA / "Videos").rglob("*") if p.is_dir()] + [MEDIA / "Videos"]:
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        stems = [e.stem for e in entries if e.is_file() and e.suffix.lower() in VIDEO_EXT | AUDIO_EXT]
        for e in entries:
            if e.is_file() and e.suffix.lower() in SUB_EXT | {".jpg", ".png", ".webp"} \
                    and not any(e.name.startswith(st + ".") for st in stems):
                e.unlink(missing_ok=True)
                removed.append(str(e))
    return removed


def sweep_loop():
    while True:
        try:
            for f in sweep_orphans():
                print("removed orphan", f)
        except Exception:
            traceback.print_exc()
        time.sleep(3600)


def remove_job(jid, with_files):
    """Remove a job from the list (stopping it first); optionally delete its files too."""
    cancel_job(jid)
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row:
        return "gone", 0
    if row["status"] in ("downloading", "processing"):
        return "stopping", 0
    shared = [r for r in q("SELECT id, files FROM jobs WHERE id != ? AND status NOT IN ('failed','cancelled')", (jid,))
              if set(json.loads(r["files"] or "[]")) & set(json.loads(row["files"] or "[]"))]
    # Files another account still uses stay on disk; only this entry goes
    files = delete_job_files(row) if with_files and not shared else 0
    shutil.rmtree(INCOMPLETE / str(jid), ignore_errors=True)  # any partial download
    q("DELETE FROM jobs WHERE id=?", (jid,))
    q("UPDATE jobs SET ref=NULL WHERE ref=?", (jid,))
    search.forget_index(jid)
    if with_files and shared:
        return "kept_shared", 0
    return "removed", files


# The other modules, imported last: they import this one too, and are only used at run time
from . import board, channels, core, download, library, llm, search, telegram  # noqa: E402
