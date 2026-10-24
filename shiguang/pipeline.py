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
from .migrations import once
from .core import (AUDIO_EXT, Cancelled, DOWNLOAD_WORKERS, ENTRY, INCOMPLETE, LLM_API_KEY, MEDIA, SUB_EXT, UA, VIDEO_EXT, bell_mark, bell_wait, check_cancel, db_lock, ffprobe, finishing_touch, human, job_dict, link_key, log_usage, q, ring, unique_path, update)


SHARED_FIELDS = ("status", "kind", "title", "files", "thumb", "analysis", "error", "transcript", "progress")


def add_job(url, source="web", chat_id=None, msg_id=None, owner=None, device=None):
    return add_job_ex(url, source, chat_id, msg_id, owner, device)[0]


def add_job_ex(url, source="web", chat_id=None, msg_id=None, owner=None, device=None,
               key_aliases=(), download_context=None):
    """Queue a link. Returns (job id, how): "new", "duplicate" when this owner already has it queued,
    downloading or downloaded (the existing job is returned), or "linked" when someone else already has it:
    then this owner gets their own entry that points at the same files instead of a second download."""
    url = url.strip()
    key = link_key(url)
    from .crawl import identity, MEDIA_EXT

    def precise_key(address):
        return identity(address, bool(MEDIA_EXT.search(address))) if address.startswith(('http://', 'https://')) else link_key(address)

    equivalent = {precise_key(url)}
    if key.startswith(('youtube:', 'bilibili:', 'btih:')):
        # link_key may already have resolved a platform's short share URL.
        equivalent.add(key)
    for alias in key_aliases:
        if alias.startswith(('http://', 'https://')):
            equivalent.add(precise_key(alias))
        elif alias.startswith(('youtube:', 'bilibili:', 'btih:')):
            equivalent.add(alias)
    keys = json.dumps(list(dict.fromkeys([key, *key_aliases, *sorted(equivalent)])))
    stored_key = key_aliases[0] if key_aliases else key
    with db_lock:
        # Serialize lookup + insert across web/subscription/crawl processes, including aliases.
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            # A regular add may use a collected detail-page alias while the job
            # URL is its media stream. Resolve that direction through the ledger.
            alias_jobs = {row['job_id'] for row in db.execute(
                'SELECT DISTINCT i.job_id FROM crawl_aliases a JOIN crawl_items i ON i.id=a.item_id '
                'WHERE i.job_id IS NOT NULL AND a.url_key IN (SELECT value FROM json_each(?))',
                (json.dumps(sorted(equivalent)),)).fetchall()}
            clause = '(key IN (SELECT value FROM json_each(?)) OR url=? OR id IN (SELECT value FROM json_each(?)))'
            args = (keys, url, json.dumps(sorted(alias_jobs)))

            def same_video(row):
                # Legacy generic keys omit some query fields (e.g. mid/ref) and
                # the scheme. They locate candidates but cannot prove identity.
                return row['id'] in alias_jobs or row['key'] in equivalent or precise_key(row['url']) in equivalent

            existing = next((row for row in db.execute(
                "SELECT id,url,key FROM jobs WHERE " + clause +
                " AND owner IS ? AND status NOT IN ('failed','cancelled') ORDER BY id DESC", (*args, owner)).fetchall()
                if same_video(row)), None)
            if existing:
                db.commit()
                return existing['id'], 'duplicate'
            source_job = next((row for row in db.execute(
                "SELECT * FROM jobs WHERE " + clause + " AND ref IS NULL "
                "AND status IN ('queued','downloading','processing','done') ORDER BY status='done' DESC,id DESC", args).fetchall()
                if same_video(row)), None)
            cur = db.execute("INSERT INTO jobs (url,key,source,chat_id,msg_id,owner,device,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                             (url, stored_key, source, chat_id, msg_id, owner, device, time.time(), time.time()))
            job_id = cur.lastrowid
            if source_job:
                if source_job['status'] == 'done':
                    fields = {f: source_job[f] for f in SHARED_FIELDS}
                    fields['ref'] = source_job['id']
                    db.execute('UPDATE jobs SET ' + ','.join(f'{f}=?' for f in fields) + ' WHERE id=?', (*fields.values(), job_id))
                else:
                    db.execute("UPDATE jobs SET ref=?,status='linked' WHERE id=?", (source_job['id'], job_id))
            if download_context:
                db.execute('INSERT INTO crawl_downloads(job_id,context) VALUES(?,?)', (job_id, json.dumps(download_context)))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    ring("jobs")
    return job_id, 'linked' if source_job else 'new'


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


_views = {}  # B站 video id -> its info from the view API (parts, title...), {} for one B站 won't give; a few per show
_unanswered = {}  # B站 video id -> when B站 last failed to answer about it (not asked again for a while)
UNANSWERED_FOR = 300
PARTS_MAX = int(os.environ.get("PARTS_MAX", "50"))  # a video with more parts than this asks before taking them all
PART_KEY = re.compile(r"(bilibili:(BV\w+|av\d+):p)(\d+)")
BILI_BUSY = (-412, -352, -509, -799)  # request intercepted, risk control, rate limited: may answer a little later


def bili_view(vid, tries=4):
    """What B站's view API says about a video (BV... or av...): {"pages": [{"page", "part"}...], ...}. {} when B站
    won't give it (no such video, hidden, under review, not for this region or account); None when it didn't answer
    (it turns some requests away with 412: tried again a few times, a little later each time, and not asked again
    for UNANSWERED_FOR seconds)."""
    if vid in _views:
        return _views[vid]
    if time.time() - _unanswered.get(vid, 0) < UNANSWERED_FOR:
        return None
    for i in range(tries):
        if i:
            time.sleep(2 * i)
        try:
            r = requests.get("https://api.bilibili.com/x/web-interface/view", timeout=15,
                             params={"aid": vid[2:]} if vid.lower().startswith("av") else {"bvid": vid},
                             headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
            body = r.json() if r.status_code == 200 else {}
        except Exception:
            continue
        code = body.get("code")
        if code == 0 and body.get("data"):
            _views[vid] = body["data"]
        elif code is not None and code not in BILI_BUSY:  # B站 answered: this video isn't to be had
            _views[vid] = {}
        else:
            continue
        if len(_views) > 200:
            _views.pop(next(iter(_views)))
        return _views[vid]
    _unanswered[vid] = time.time()
    return None


def part_link(vid, n):
    return f"https://www.bilibili.com/video/{vid}?p={n}"


def bili_parts(url):
    """A B站 video with several parts (分P: a whole show uploaded as one video): (a link to each part in order, so
    every part becomes its own job, how many parts B站 lists). Any other link: ([url], None); one with more than
    PARTS_MAX parts (the video's page in 拾光 offers the rest): ([url], the count); when B站 doesn't answer (the
    worker looks again): ([url], None). (The app's share link always says p=1, whichever part was open, so all
    parts are taken whatever p says.)"""
    m = PART_KEY.fullmatch(link_key(url))
    pages = ((bili_view(m.group(2), tries=2) or {}).get("pages") or []) if m else []  # (someone's waiting)
    if len(pages) < 2:
        return [url], None
    if len(pages) > PARTS_MAX:
        return [url], len(pages)
    return [part_link(m.group(2), p["page"]) for p in pages], len(pages)


def parts_left_note(count):
    """What to tell whoever sent a link to a video with more parts than were taken."""
    return f"有个视频有 {count} 个分P，只下了这一个，要全部下载在拾光里打开它点「下载全部分P」"


def queue_parts(job, pages):
    """Queue every part of `job`'s video for its owner, as if they'd sent each one (parts they have are kept)."""
    vid = PART_KEY.fullmatch(job["key"]).group(2)
    return [add_job_ex(part_link(vid, p["page"]), source=job["source"] or "web", owner=job["owner"], device=job["device"])
            for p in pages]


def check_parts(job, a):
    """A B站 video just downloaded: when it has parts and none of its other parts are in 拾光 (B站 didn't answer
    when the link came, or a follow brought it), queue them. Too many to just take, or B站 still not answering:
    noted in `a` ("parts"), so its page can offer them."""
    m = PART_KEY.fullmatch(job["key"] or "")
    if not m or q("SELECT 1 FROM jobs WHERE key LIKE ? AND key<>? AND owner IS ? LIMIT 1",
                  (m.group(1) + "%", job["key"], job["owner"]), one=True):
        return
    view = bili_view(m.group(2))
    pages = (view or {}).get("pages") or []
    if view is None:
        a["parts"] = {"total": None}
    elif len(pages) > PARTS_MAX:
        a["parts"] = {"total": len(pages)}
    elif len(pages) > 1:
        queue_parts(job, pages)


def take_parts(jid):
    """The video page's button: queue all the parts of job `jid`'s video. Returns what to tell the user."""
    job = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    m = PART_KEY.fullmatch(job["key"] or "") if job else None
    if not m:
        return "这不是 B站 视频"
    view = bili_view(m.group(2))
    if view is None:
        return "B站 暂时没回应，过一会儿再试"
    pages = view.get("pages") or []
    root = job["ref"] or jid  # the offer was copied to every entry of this video: gone from all of them
    for r in q("SELECT id, analysis FROM jobs WHERE id=? OR ref=?", (root, root)):
        a = json.loads(r["analysis"] or "{}")
        if "parts" in a:
            a.pop("parts")
            update(r["id"], analysis=a)
    if len(pages) < 2:
        return "这个视频只有一个分P"
    new = sum(how != "duplicate" for _, how in queue_parts(dict(job), pages))
    return f"开始下载其余 {new} 个分P" if new else "所有分P 都已经在拾光里了"


@once("part_specials", background=True)
def part_specials_now():
    """Once, in the background: the specials of 分P videos already downloaded go into part order."""
    videos = {}
    for r in q("SELECT key FROM jobs WHERE key LIKE 'bilibili:%:p%' AND json_extract(analysis, '$.season')=0"):
        if (m := PART_KEY.fullmatch(r["key"])):
            videos.setdefault(m.group(1), r["key"])
    if any([renumber_part_specials(key) for key in videos.values()]):
        library.plex_refresh()


def renumber_part_specials(key):
    """分P of one video that ended up as specials (小剧场, 番外...) get their show's numbers in part order, not in
    the order their downloads happened to finish. Returns whether any moved (Plex then needs a refresh)."""
    m = PART_KEY.fullmatch(key or "")
    if not m:
        return False
    with library.specials_lock():  # two parts finishing at once, or one being placed meanwhile
        rows = [r for r in q("SELECT * FROM jobs WHERE key LIKE ? AND status='done' AND ref IS NULL AND "
                             "json_extract(analysis, '$.library')='TV' AND json_extract(analysis, '$.season')=0 AND "
                             "json_extract(analysis, '$.episode') IS NOT NULL", (m.group(1) + "%",))
                if PART_KEY.fullmatch(r["key"])]
        rows.sort(key=lambda r: int(PART_KEY.fullmatch(r["key"]).group(3)))
        numbers = sorted(json.loads(r["analysis"])["episode"] for r in rows)
        wrong = [(r["id"], n) for r, n in zip(rows, numbers) if json.loads(r["analysis"])["episode"] != n]
        # through numbers nobody uses first, so two that trade places don't land on each other
        for step in ("aside", "home"):
            for i, (jid, n) in enumerate(wrong):
                r = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
                a = json.loads(r["analysis"])
                print("part_specials:", library.refile(r, {**a, "episode": 9000 + i if step == "aside" else n}), flush=True)
    return bool(wrong)


def episode_title(url, key):
    """An episode's own name as B站 shows it: a 番剧 正片's ("慕兰之战19"), a 分P's part ("第一季04"); None otherwise."""
    if "bilibili.com" not in (url or ""):
        return None
    ep = bili_episode(url)
    if ep:
        return (ep.get("name") or "").strip() or None if ep["episode"] is not None else None
    m = PART_KEY.fullmatch(key or "")
    pages = ((bili_view(m.group(2)) or {}).get("pages") or []) if m else []
    if len(pages) < 2:
        return None
    return next((str(p.get("part") or "").strip() or None for p in pages if p.get("page") == int(m.group(3))), None)


def part_show(key):
    """The show another part of the same B站 video was filed under (so all its parts end up in one show), or None."""
    m = re.fullmatch(r"(bilibili:\w+:p)\d+", key or "")
    if not m:
        return None
    r = q("SELECT json_extract(analysis, '$.show') show FROM jobs WHERE key LIKE ? AND key<>? AND status='done' AND "
          "json_extract(analysis, '$.library')='TV' AND json_extract(analysis, '$.show') IS NOT NULL "
          "ORDER BY id LIMIT 1", (m.group(1) + "%", key), one=True)
    return r["show"] if r else None




def bangumi_season(params):
    """A B站 番剧 season (params: {"season_id": ...} or {"ep_id": ...}): its name, cover, its show and season number,
    and every video of it: {"episodes": [entry...]}, each {"id", "bvid", "url", "title" (as the page shows it:
    "第193话 慕兰之战17", "慕兰之战第19集预告"), "badge" ("会员" / "预告"...), "extra" (not 正片), "episode" (its
    number, for 正片), "date", "duration"}."""
    res = requests.get("https://api.bilibili.com/pgc/view/web/season", params=params, timeout=15,
                       headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}).json()["result"]
    seasons = [x.get("season_id") for x in res.get("seasons") or []]
    out = {"season_id": res["season_id"], "name": res.get("title") or "", "cover": res.get("cover") or "",
           "show": (res.get("series") or {}).get("series_title") or res.get("title") or "",
           "season": seasons.index(res["season_id"]) + 1 if res["season_id"] in seasons else 1, "episodes": []}
    for section, eps in [(None, res.get("episodes") or [])] + [(x, x.get("episodes") or []) for x in res.get("section") or []]:
        for e in eps:
            # only 正片's own list says what is 正片 (section_type 0); the sections hold 花絮 and PVs, but also old
            # 正片 (凡人修仙传's 2020版): those are left to the classifier
            main = section is None and e.get("section_type", 0) == 0
            num = str(e.get("title") or "")
            out["episodes"].append({
                "id": e.get("id"), "bvid": e.get("bvid"),
                "url": f"https://www.bilibili.com/video/{e['bvid']}" if e.get("bvid") else f"https://www.bilibili.com/bangumi/play/ep{e.get('id')}",
                "title": e.get("show_title") or " ".join(str(e.get(k) or "") for k in ("title", "long_title")).strip(),
                "name": e.get("long_title") or "", "badge": e.get("badge") or "", "extra": section is None and not main,
                "episode": int(num) if main and num.isdigit() else None,
                "date": time.strftime("%Y-%m-%d", time.localtime(e["pub_time"])) if e.get("pub_time") else None,
                "duration": (e.get("duration") or 0) / 1000 or None})
    return out


_bangumi = {}  # bvid -> its entry (one season list answers for all of the show's videos)


def bili_episode(url):
    """For a video of a B站 番剧: its entry in bangumi_season, plus "show" and "season"; None otherwise. (yt-dlp names
    an episode "<number> <long title>", so the preview listed as 195 came out as "195 慕兰之战19", like an episode.)"""
    m = re.search(r"/bangumi/play/ep(\d+)", url or "") or re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    if not m:
        return None
    try:
        ep_id = m.group(1) if m.group(1).isdigit() else None
        if not ep_id:
            if m.group(1) in _bangumi:
                return _bangumi[m.group(1)]
            ep = re.search(r"/bangumi/play/ep(\d+)", (bili_view(m.group(1)) or {}).get("redirect_url") or "")
            if not ep:
                return None  # an ordinary video
            ep_id = ep.group(1)
        season = bangumi_season({"ep_id": ep_id})
        found = None
        for e in season["episodes"]:
            info = {**e, "show": season["show"], "season": season["season"]}
            if e["bvid"]:
                _bangumi.setdefault(e["bvid"], info)
            if str(e["id"]) == ep_id:
                found = found or info
        return found
    except Exception:
        traceback.print_exc()
        return None


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
    download_dir = workdir

    # 1. download
    if url.startswith("magnet:") or url.endswith(".torrent") or url.startswith("torrent-file:"):
        kind = "torrent"
        update(job_id, kind=kind, stage="downloading torrent")
        src = url[len("torrent-file:"):] if url.startswith("torrent-file:") else url
        download.aria2(job_id, ["--dir", str(workdir), "--seed-time=0", "--bt-stop-timeout=1800", "--follow-torrent=mem",
                       "--bt-remove-unselected-file=true", "--enable-dht=true", "--bt-enable-lpd=true", src], as_bt=True)
        meta["source"] = "torrent"
    elif q('SELECT 1 FROM crawl_downloads WHERE job_id=?', (job_id,), one=True):
        from . import crawl
        kind = 'video'
        update(job_id, kind=kind)
        context = crawl.download_context(job_id)
        meta, download_dir = download.page_video_download(job_id, url, workdir, media_context=context)
    else:
        probe_error = None
        try:
            info = download.ytdlp_probe(url)
        except Exception as error:
            check_cancel(job_id)
            if not download.can_fallback(error):
                raise
            info, probe_error = None, error
        check_cancel(job_id)
        if info or probe_error:
            kind = "video"
            update(job_id, kind=kind, title=(info or {}).get("title") or "")
            meta, download_dir = download.page_video_download(job_id, url, workdir, info, probe_error)
        elif download.head_is_file(url):
            kind = "file"
            update(job_id, kind=kind, stage="downloading file")
            download_dir = workdir / ".file"
            if not download_dir.exists():
                download_dir.mkdir()
                for old in workdir.iterdir():
                    if not old.name.startswith("."):
                        shutil.move(str(old), str(download_dir / old.name))
            download.aria2(job_id, ["--dir", str(download_dir), "-x", "8", "-s", "8", "--user-agent", UA,
                           "--content-disposition-default-utf8=true", url])
            meta["source"] = "file"
        else:
            kind = "video"
            update(job_id, kind=kind)
            meta, download_dir = download.page_video_download(job_id, url, workdir)

    # 2. post-process each media file
    check_cancel(job_id)
    update(job_id, status="processing", stage="inspecting", progress=0, speed="")
    files = sorted((p for p in download_dir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")),
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
            site_line = meta.pop("_timeline", None) or {}  # (not for the classifier: long lists of numbers)
            staff = bili_staff(meta.get("webpage_url") or url)  # B站 joint videos list everyone who's in them
            bangumi = bili_episode(meta.get("webpage_url") or url) if "bilibili.com" in (meta.get("webpage_url") or url) else None
            if bangumi:  # the name its 番剧 page shows, and whether it's 正片
                meta.update(title=bangumi["title"], bilibili_badge=bangumi["badge"])
            a = llm.classify(job_id, m.name, {**meta, "file": m.name, "size": human(m.stat().st_size),
                                          "duration_s": float(ffprobe(m).get("format", {}).get("duration") or 0),
                                          **({"people_in_it": staff} if staff else {})},
                         guess)
            add_people_tags(a, staff)
            if bangumi and bangumi["extra"]:
                a["episode"] = None  # B站 lists it outside 正片 (a preview, PV, 花絮...): not an episode
            if a.get("library") == "TV" and (sibling := part_show(job["key"])):
                a["show"] = sibling  # 分P of one video: one show, however the classifier spelt it this time
            if a.get("library") == "TV" and (name := episode_title(meta.get("webpage_url") or url, job["key"])):
                a["episode_title"] = name  # shown with the episode number (拾光, and Plex when it has no name for it)
            if "bilibili.com" in (meta.get("webpage_url") or url):
                check_parts(job, a)  # its other 分P, if it has some that weren't queued when the link came
            if kind not in ("torrent", "file"):  # a show's trailers, OP/ED... go with its episodes, not in 片段
                library.place_in_show(a, job["source"], [str(a.get("title") or ""), meta.get("title") or "", job["title"] or ""])
            if bangumi and bangumi["episode"] is not None:  # 番剧 正片: an episode, numbered as B站 numbers it
                a.update(library="TV", folder="Other", show=bangumi["show"], season=bangumi["season"], episode=bangumi["episode"])
            if site_line.get("markers"):  # what the site shows along its progress bar (only one video per link)
                a["markers"] = {"0": site_line["markers"]}
            if site_line.get("heat"):
                a["heat"] = {"0": site_line["heat"]}
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
    leftovers = [p for p in download_dir.rglob("*") if p.is_file() and not p.name.endswith(".aria2")]
    if leftovers and (not media or kind in ("torrent", "file")):
        root_items = list(download_dir.iterdir())
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
    if analysis.get("library") == "TV" and analysis.get("season") == 0:
        try:  # a 分P that became a special: in part order with its video's others (the job is done whatever happens here)
            renumber_part_specials(job["key"])
        except Exception:
            traceback.print_exc()
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


@once("bangumi_extras", background=True)
def refile_bangumi_extras():
    """Once: B站 番剧 videos filed as episodes that the 番剧 lists outside 正片 (a preview named like an episode)
    become specials of their show, under the name the page shows."""
    for r in q("SELECT * FROM jobs WHERE status='done' AND ref IS NULL AND url LIKE '%bilibili.com%' "
               "AND json_extract(analysis, '$.library')='TV' AND json_extract(analysis, '$.season')!=0"):
        ep = bili_episode(r["url"])
        if not ep or not ep["extra"]:
            continue
        a = json.loads(r["analysis"])
        b = library.place_in_show({**a, "title": ep["title"], "episode": None}, r["source"], [ep["title"]])
        print("bangumi_extras:", library.refile(r, b), flush=True)
        update(r["id"], title=ep["title"])
    library.plex_refresh()


def _commit_job_failure(job_id, **fields):
    """Cancellation wins over a network failure racing with the last cancel check."""
    with db_lock:
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT cancel,status FROM jobs WHERE id=?', (job_id,)).fetchone()
            cancelled = not row or row['cancel'] or row['status'] == 'cancelled'
            if cancelled:
                fields = {'status': 'cancelled', 'stage': '', 'speed': '', 'retry_at': None}
            fields['updated'] = time.time()
            db.execute('UPDATE jobs SET ' + ','.join(f'{field}=?' for field in fields) + ' WHERE id=?',
                       (*fields.values(), job_id))
            db.commit()
            return bool(cancelled)
        except BaseException:
            db.rollback()
            raise


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
        permanent = (download.terminal_download_reason(e)
                     or (not e.retryable if isinstance(e, download.DownloadFailure) else
                         not download.temporary_download_error(e) and
                         re.search(r"No video found|Unsupported URL|no longer supported|produced no files|404|"
                                   r"Private video|removed|not available|Cloudflare 人机验证", str(e), re.I)))
        # Being taken for a bot passes after a while: wait longer and try more often
        delays = (1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600) if download.BOT_CHECK.search(str(e)) or "机器人" in str(e) else (60, 300, 900)
        if attempts <= len(delays) and not permanent:
            delay = delays[attempts - 1]
            wait = f"{delay // 3600} 小时" if delay >= 3600 else f"{delay // 60} 分钟"
            failure = dict(status="failed", stage="", speed="", attempts=attempts, retry_at=time.time() + delay,
                           error=f"{str(e)[:900]}\n（{wait}后自动重试，第 {attempts}/{len(delays)} 次，可续传的部分会保留）")
        else:
            failure = dict(status="failed", stage="", speed="", attempts=attempts, retry_at=None, error=str(e)[:1000], advice="")
        if _commit_job_failure(job_id, **failure):
            shutil.rmtree(INCOMPLETE / str(job_id), ignore_errors=True)
        elif failure['retry_at'] is None and LLM_API_KEY:  # failed for good: plain-language advice
            board.publish("explain_failure", f"job:{job_id}", 50, force=True)
    finally:
        finish_links(job_id)
        ring("jobs", "tasks")  # a download slot is free; the CPU may be (the Pi's idle work waits for that)
        telegram.notify(job_id)


finishing = []  # job processes past their final status, still doing finishing touches
claim_lock = threading.Lock()


def _claim_job(jid):
    """A previously selected queue row may have been cancelled in another process."""
    with db_lock:
        changed = core.DB.execute("UPDATE jobs SET status='downloading',stage='starting',updated=? "
                                  "WHERE id=? AND status='queued' AND COALESCE(cancel,0)=0",
                                  (time.time(), jid)).rowcount
        core.DB.commit()
    return bool(changed)


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
            if row and not _claim_job(row['id']):
                row = None
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
            row = q("SELECT status, cancel FROM jobs WHERE id=?", (jid,), one=True) or {"status": None, "cancel": 0}
            if row["cancel"] and row["status"] in ("downloading", "processing"):
                # A yt-dlp probe can be stuck in a site request before it has
                # a progress hook to observe cancel=1. The worker owns this
                # child, so stop it here instead of leaving the card forever
                # in “starting”.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                update(jid, status="cancelled", stage="", speed="")
                shutil.rmtree(INCOMPLETE / str(jid), ignore_errors=True)
                finish_links(jid)
                break
            if row["status"] not in ("downloading", "processing"):
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
    finish = False
    with db_lock:
        # Serialize with the due-retry claim, so a waiting retry cannot revive
        # itself after cancellation. Downloading children observe cancel=1.
        db = core.DB
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT status,retry_at FROM jobs WHERE id=?', (jid,)).fetchone()
            if not row:
                db.rollback()
                return f"No job #{jid}"
            if row['status'] in ('queued', 'linked') or row['status'] == 'failed' and row['retry_at'] is not None:
                db.execute("UPDATE jobs SET status='cancelled',retry_at=NULL,cancel=1,stage='',speed='',updated=? WHERE id=?",
                           (time.time(), jid))
                finish = True
            elif row['status'] in ('downloading', 'processing'):
                db.execute('UPDATE jobs SET cancel=1,updated=? WHERE id=?', (time.time(), jid))
            else:
                db.rollback()
                return f"#{jid} is already {row['status']}"
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if finish:
        shutil.rmtree(INCOMPLETE / str(jid), ignore_errors=True)
        finish_links(jid)
    ring('jobs')
    return f"Cancelling #{jid}"


def retry_job(jid):
    row = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] not in ("failed", "cancelled"):
        return f"#{jid} can't be retried"
    update(jid, status="queued", error="", progress=0, stage="", ref=None, attempts=0, retry_at=None, cancel=0)
    return f"Retrying #{jid}"


def delete_job_files(row, protected_files=None, *, protection=None):
    """Delete only unshared library files; a recycle-bin entry protects its media too."""
    from . import trash
    if protection is None and protected_files is None:
        # Existing callers can still use this helper safely. The purge path passes
        # references collected while holding its transaction instead.
        with db_lock:
            protected_files = trash._referenced(core.DB)
    if protection is None:
        protection = trash._FileProtection(protected_files)
    root = MEDIA.resolve()
    removed = 0
    visited = set()

    def shared(path):
        return protection.shared(path)

    def unlink(path):
        nonlocal removed
        resolved = trash._safe_path(str(path), root=protection.root)
        if resolved is None or resolved in visited or shared(resolved):
            return
        visited.add(resolved)
        if path.is_symlink():
            path.unlink()
            removed += 1
            return
        if path.is_dir():
            # Never recursively remove a whole directory containing shared files.
            for child in list(path.iterdir()):
                unlink(child)
            if path != root and path.exists() and not any(path.iterdir()):
                path.rmdir()
            return
        if not path.exists() and not path.is_symlink():
            return
        if path.suffix.lower() in VIDEO_EXT | AUDIO_EXT and path.exists():
            st = path.stat()
            (library.PLAY_CACHE / f"{hashlib.sha1(f'{path}:{st.st_mtime}'.encode()).hexdigest()}.mp4").unlink(missing_ok=True)
            for side in path.parent.glob(glob.escape(path.stem) + ".*"):
                if side.suffix.lower() in SUB_EXT and side.is_file():
                    unlink(side)
        path.unlink()
        removed += 1
        parent = path.parent
        while (parent != root and parent.parent != root and parent.parent != root / "Videos"
               and parent.exists() and not shared(parent) and not any(parent.iterdir())):
            parent.rmdir()
            parent = parent.parent

    for value in trash._paths(dict(row)):
        path = Path(value)
        if trash._safe_path(value, root=protection.root) is not None:
            unlink(path)
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


def remove_job(jid, with_files=None, owner=None):
    """Move a stopped job to its owner's recycle bin. Old file flags are ignored."""
    cancel_job(jid)
    from . import trash
    result, files = trash.trash_job(jid, actor=owner)
    if result == "trashed":
        shutil.rmtree(INCOMPLETE / str(jid), ignore_errors=True)
        # Search scopes itself to active jobs. Keep its costly text/image indexes
        # for restoration; trash_job cancels queued work in the same transaction.
    return result, files


# The other modules, imported last: they import this one too, and are only used at run time
from . import board, channels, core, download, library, llm, telegram  # noqa: E402


@once("episode_titles", background=True)
def backfill_episode_titles():
    """Once: the B站 name of each 剧集 episode downloaded before episode titles were kept (番剧 正片, 分P parts),
    also written into Plex where Plex has no name for the episode."""
    plex = []
    for r in q("SELECT id, url, key, files, analysis FROM jobs WHERE status IN ('done', 'linked') AND url LIKE '%bilibili.com%' "
               "AND json_extract(analysis, '$.library')='TV' AND json_extract(analysis, '$.episode_title') IS NULL"):
        name = episode_title(r["url"], r["key"])
        if not name:
            continue
        a = {**json.loads(r["analysis"]), "episode_title": name}
        update(r["id"], analysis=a)
        plex += [(f, a) for f in json.loads(r["files"] or "[]")[:1]]
    library.plex_set_metadata(plex)
