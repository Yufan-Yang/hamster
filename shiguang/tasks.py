"""What each kind of task does on the Pi, and what follows it."""
import json
import os
import re
import requests
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse

from pathlib import Path

from . import board, books, download, library, llm, notes, search
from .migrations import once
from .core import (AUDIO_EXT, INCOMPLETE, LLM_API_KEY, NOTES_DIR, NOTE_WHISPER_MODEL, STATE, UA, VIDEO_EXT, WHISPER_FAST, ffprobe, heavy_slot, job_dict, kv_set, log_usage, q, update)


IDLE_WHISPER_MODEL = os.environ.get("IDLE_WHISPER_MODEL", "small")


# ---- what happens when a task is done (on the Pi, whoever did it)

def after_transcribe(task, result):
    p = json.loads(task["payload"])
    log_usage("whisper", "mac" if task["worker"] != board.PI_WORKERS["cpu"] else "idle", p["job"],
              amount=float(result.get("audio_seconds") or 0), seconds=float(result.get("seconds") or 0))
    board.publish("save_subs", task["target"], task["priority"], parent=task["id"], force=True)


@board.task("save_subs", "保存字幕", "light")
def pi_save_subs(task, beat):
    """The subtitles a transcribe task worked out: .srt next to the video (player, Plex), lines into the search
    index, the transcript; then the summary that waited for them."""
    res = board.parent_result(task)
    segs = [[float(a), float(b), str(t).strip()] for a, b, t in res.get("segments") or [] if str(t).strip()]
    lang = re.sub(r"[^a-z-]", "", str(res.get("language") or "und"))[:8] or "und"
    path, _ = board.task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]

    def ts(t):
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(sec):02},{int((sec % 1) * 1000):03}"
    old = " ".join(r["text"] for r in q("SELECT text FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕' "
                                         "ORDER BY t", (jid, n)))
    if segs:
        # Made again (a forced redo, a better model) and saying the same: the summary, chapters and duplicates found
        # from the old ones still hold; only the lines and their times change
        same = bool(old) and text_alike(old, " ".join(t for _, _, t in segs)) >= 0.85
        when = ai_when(task["priority"])
        srt = path.with_name(f"{path.stem}.{lang}.srt")
        srt.write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(segs, 1)))
        q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (jid, n))
        for a, _, t in segs:
            q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'字幕',?)", (jid, n, a, t))
        a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
        summary = False
        if n == 0:
            update(jid, transcript="\n".join(t for _, _, t in segs)[:200_000])
            if a.get("needs_transcript") and not a.get("key_points") and LLM_API_KEY:
                # chapters come after it (they need its key points, and read the subtitles from DeepSeek's cache)
                board.publish("summarize", f"job:{jid}", task["priority"], parent=task["id"], force=True, not_before=when)
                summary = True
        library.plex_refresh()  # Plex picks up the new subtitle file
        maybe_translate(task, path, lang)
        if not same:
            board.publish("similar", f"job:{jid}", task["priority"] - 3, parent=task["id"], force=True)
        if not summary and (not same or str(n) not in (a.get("chapters") or {})):
            board.publish("chapters", task["target"], task["priority"] - 1, parent=task["id"], force=True, not_before=when)
    board.drop_parent_result(task, {"language": lang, "lines": len(segs), "audio_seconds": res.get("audio_seconds")})
    return {"lines": len(segs), "language": lang, **({"unchanged": True} if segs and same else {})}


def text_alike(a, b):
    """How much two transcripts say the same (0-1): shared two-character pieces, punctuation and spaces ignored."""
    def grams(t):
        t = re.sub(r"[\W_]+", "", t.casefold())
        return {t[i:i + 2] for i in range(len(t) - 1)}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(len(ga | gb), 1)


def ai_when(priority):
    """When AI work for a task of this priority may run: links you sent and new videos of followed uploaders (50 and
    up) right away; the backlog (older videos of followed uploaders, the library's catch-up) at DeepSeek's half price."""
    return None if priority >= 50 else llm.offpeak_from()


@board.task("summarize", "AI 总结", "ai-quick", remote=True)
def pi_summarize(task, beat):
    jid = task["payload"]["job"]
    row = q("SELECT analysis, transcript, files FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    if not row["transcript"]:
        return {"skipped": "no transcript"}
    a.pop("note", None)
    text, lines, _ = llm.timed_transcript(jid, 0)
    if lines >= 20:  # the same text chapters will send, so it reads it from the cache
        a = llm.summarize_timed(jid, a, text)
    else:
        a = llm.summarize(jid, a, row["transcript"], "(subtitles of the whole video)")
    update(jid, analysis=a, stage="")
    llm.merge_tags_if_new()  # tags it brought that mean the same as ones in the library: folded in
    # chapters next (the key points changed). DeepSeek takes a few seconds to keep a request's opening for reuse;
    # asked at once, the subtitles were paid in full twice (measured: 0 of 17k tokens from the cache; 20 s later, 16.5k)
    board.publish("chapters", f"job:{jid}:0", task["priority"], parent=task["id"], force=True,
                  not_before=max(ai_when(task["priority"]) or 0, time.time() + 20))
    vids = [f for f in json.loads(row["files"] or "[]") if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT]
    if vids:
        threading.Thread(target=library.plex_set_metadata, args=([(vids[0], a)],), daemon=True).start()
    return {"summary": bool(a.get("key_points"))}


def maybe_translate(task, path, subs_or_lang):
    """English subtitles and no Chinese ones yet: translate them (off-peak, at half the price)."""
    langs = [subs_or_lang] if isinstance(subs_or_lang, str) else [library.sub_lang(x, path.stem) for x in subs_or_lang]
    if any(l.split("-")[0] == "en" for l in langs) and not any(l.split("-")[0] == "zh" for l in langs) and LLM_API_KEY:
        board.publish("translate", task["target"], task["priority"] - 2, parent=task["id"], not_before=llm.offpeak_from())


TRANSLATE_SYSTEM = """You translate a video's subtitles from {src} into natural Simplified Chinese.
The input is consecutive subtitle lines, numbered; auto-generated captions break sentences anywhere and have no
punctuation. Give exactly one Chinese line per input line, in the same order: you may move a few words between
neighbouring lines so the Chinese reads naturally, but every line must stay about where its words are said.
Keep names, terms and code identifiers that are usually left untranslated; use the common Chinese renderings
of well-known names. Reply with one JSON object: {{"lines": ["...", ...]}} with exactly as many lines as given."""


@board.task("translate", "翻译字幕", "ai")
def pi_translate(task, beat):
    """Chinese subtitles for an English video: X.zh.srt next to it (Plex shows it as Chinese; the page also offers
    both together), and the Chinese lines go into search (a Chinese search finds the English moment)."""
    path, subs = board.task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]
    en = sorted((x for x in subs if library.sub_lang(x, path.stem).split("-")[0] == "en"),
                key=lambda x: library.sub_lang(x, path.stem) != "en")  # the uploader's own .en before the automatic -orig
    if not en or any(library.sub_lang(x, path.stem).split("-")[0] == "zh" for x in subs):
        return {"skipped": "no English subtitles, or Chinese ones already"}
    cues = library.srt_cues(en[0])
    # carry on from what an earlier run (stopped by a restart) translated already
    prog = task["progress"] or {}
    out = prog.get("out") if prog.get("from") == Path(en[0]).name else None
    out = out or []
    i, size, usage = len(out), 60, {}
    while i < len(cues):
        batch = cues[i:i + size]
        user = "\n".join(f"{k + 1}. {t}" for k, (_, _, t) in enumerate(batch))
        try:
            got = llm.llm_json(TRANSLATE_SYSTEM.replace("{src}", "English"), user, {"lines": "array of strings"}, 12000,
                           usage, "translate", jid, think=False)["lines"]
        except Exception:
            got = None
        if not isinstance(got, list) or len(got) != len(batch):
            if size > 8:  # the model merged or split lines: try smaller pieces
                size //= 2
                continue
            got = [t for _, _, t in batch]  # give up on these few: keep the English
        out += [[a, b, str(t).strip()] for (a, b, _), t in zip(batch, got)]
        i += len(batch)
        size = min(60, size * 2)
        beat({"pct": round(i / len(cues) * 100), "from": Path(en[0]).name, "out": out})
    library.write_srt(path.with_name(f"{path.stem}.zh.srt"), out)
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='译文'", (jid, n))
    for a, _, t in out:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'译文',?)", (jid, n, round(a, 1), t))
    library.plex_refresh()
    return {"lines": len(out), "from": Path(en[0]).name, "calls": usage.get("calls"), "cost": usage.get("cost")}


DIGEST_SYSTEM = """You write a digest of the videos some followed uploaders published in a period (usually a week), for
the person following them, in Simplified Chinese. For each uploader: an overview of 2-4 sentences on what they
covered and their main views (not a list of titles), then one short line per video saying what it's about. Finally
one sentence for the whole period. Be concrete: names, places, claims, numbers. The ids are only for the JSON:
never mention a video's id in the text.
Reply with one JSON object: {"headline": "...", "uploaders": [{"sub": <id>, "overview": "...",
"videos": [{"job": <id>, "line": "..."}]}]}"""


def digest_videos(owner, start, end):
    """The followed uploaders' videos that came out from `start` to `end` (dates) and are downloaded: by their
    upload date, or when that isn't known, by when a weekly check (not the first look back) fetched them."""
    subs = {r["id"]: r for r in q("SELECT * FROM subs WHERE owner=?", (owner,))}
    t0 = time.mktime(time.strptime(start, "%Y-%m-%d"))
    t1 = time.mktime(time.strptime(end, "%Y-%m-%d")) + 86400
    out = {}
    for r in q("SELECT * FROM jobs WHERE status='done' AND owner=? AND source LIKE 'sub:%'", (owner,)):
        sid = int(r["source"][4:])
        a = json.loads(r["analysis"] or "{}")
        pub = a.get("published")
        if sid in subs and ((start <= pub <= end) if pub else (not r["backfill"] and t0 <= r["created"] < t1)):
            out.setdefault(sid, []).append((pub or time.strftime("%Y-%m-%d", time.localtime(r["created"])), r, a))
    return subs, {sid: sorted(v, key=lambda x: x[0]) for sid, v in out.items()}


@board.task("digest", "追更周报", "ai-quick")
def pi_digest(task, beat):
    p = task["payload"]
    subs, found = digest_videos(p["owner"], p["start"], p["end"])
    body = {"start": p["start"], "end": p["end"], "headline": "", "uploaders": []}
    if found:
        parts = []
        for sid, vids in found.items():
            parts.append(f"Uploader {sid}: {subs[sid]['name']}")
            for pub, r, a in vids:
                points = "；".join(a.get("key_points") or [])
                parts.append(f"- video {r['id']} ({pub}): {a.get('title') or r['title']}\n  {a.get('summary', '')}"
                             + (f"\n  要点：{points}" if points else ""))
        out = llm.llm_json(DIGEST_SYSTEM.replace("{", "{{").replace("}", "}}"), "\n".join(parts)[:60000],
                       {"headline": "string", "uploaders": "array"}, 8000, {}, "digest")
        body["headline"] = str(out.get("headline") or "")
        for u in out.get("uploaders") or []:
            sid = llm.as_int(u.get("sub"))
            if sid in found:
                titles = {r["id"]: (a.get("title") or r["title"], pub) for pub, r, a in found[sid]}
                body["uploaders"].append({"sub": sid, "name": subs[sid]["name"], "overview": str(u.get("overview") or ""),
                                          "videos": [{"job": llm.as_int(v.get("job")), "line": str(v.get("line") or ""),
                                                      "title": titles[llm.as_int(v.get("job"))][0],
                                                      "published": titles[llm.as_int(v.get("job"))][1]}
                                                     for v in u.get("videos") or [] if llm.as_int(v.get("job")) in titles]})
    q("DELETE FROM digests WHERE owner=? AND start=? AND end=?", (p["owner"], p["start"], p["end"]))
    q("INSERT INTO digests (owner, start, end, body, created) VALUES (?,?,?,?,?)",
      (p["owner"], p["start"], p["end"], json.dumps(body, ensure_ascii=False), time.time()))
    return {"uploaders": len(body["uploaders"]), "videos": sum(len(u["videos"]) for u in body["uploaders"])}


@once("published_backfilled", background=True)
def backfill_published():
    """Once, in the background: upload dates for videos downloaded before they were kept (B站: its API; YouTube:
    yt-dlp), so 追更周报 can tell what came out when."""
    for r in q("SELECT id, url, analysis FROM jobs WHERE status='done' AND source LIKE 'sub:%'"):
        a = json.loads(r["analysis"] or "{}")
        if a.get("published"):
            continue
        day = None
        try:
            bv = re.search(r"(BV[0-9A-Za-z]{10})", r["url"])
            if bv:
                data = requests.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bv.group(1)},
                                    timeout=15, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}).json().get("data") or {}
                if data.get("pubdate"):
                    day = time.strftime("%Y-%m-%d", time.localtime(data["pubdate"]))
                time.sleep(1)
            elif "youtu" in r["url"]:
                info = download.ytdlp_probe(r["url"]) or {}
                d = str(info.get("upload_date") or "")
                day = f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else None
        except Exception:
            traceback.print_exc()
        if day:
            a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (r["id"],), one=True)["analysis"] or "{}")
            a["published"] = day
            update(r["id"], analysis=a)


def publish_digests():
    """Monday mornings: last week's digest for every account that follows someone."""
    now = time.localtime()
    if now.tm_wday != 0 or now.tm_hour < 8:
        return
    end = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    for r in q("SELECT DISTINCT owner FROM subs"):
        board.publish("digest", f"digest:{r['owner']}:{start}:{end}", 40)
    t0 = time.mktime(time.strptime(start, "%Y-%m-%d"))
    for r in q("SELECT DISTINCT owner FROM notes WHERE created >= ?", (t0,)):
        board.publish("notes_recap", f"notes-recap:{r['owner']}:{start}:{end}", 40)


@board.task("similar", "找重复和切片", "light")
def pi_similar(task, beat):
    """Fingerprint this video, then compare it with every other one: candidates by fingerprint (cheap), checked frame by
    frame and by what's said. "same": most of each is in the other; "clip": most of the shorter one is in the longer."""
    import numpy as np
    jid = task["payload"]["job"]
    row = q("SELECT transcript FROM jobs WHERE id=?", (jid,), one=True)
    sig, nsh = search.minhash(row["transcript"] or "")
    frames = search.job_frames(jid)
    mean = (frames.mean(0) / np.linalg.norm(frames.mean(0))).astype(np.float32) if frames is not None else None
    dur = sum(library.duration_of(m["path"]) or 0 for m in library.playable(job_dict(q("SELECT * FROM jobs WHERE id=?", (jid,), one=True))))
    q("INSERT OR REPLACE INTO fingerprints (job_id, minhash, shingles, frames, nframes, duration, updated) VALUES (?,?,?,?,?,?,?)",
      (jid, sig.tobytes() if sig is not None else None, nsh, mean.tobytes() if mean is not None else None,
       0 if frames is None else len(frames), dur, time.time()))
    q("DELETE FROM similar WHERE a=? OR b=?", (jid, jid))
    found = 0
    for o in q("SELECT * FROM fingerprints WHERE job_id != ? AND job_id IN (SELECT id FROM jobs WHERE status='done' AND ref IS NULL)", (jid,)):
        said_ab = said_ba = 0.0
        if sig is not None and o["minhash"]:
            jac = float((sig == np.frombuffer(o["minhash"], np.uint32)).mean())
            if jac > 0.02:  # containment from Jaccard and the two set sizes
                said_ab = min(1, jac * (nsh + o["shingles"]) / ((1 + jac) * nsh))
                said_ba = min(1, jac * (nsh + o["shingles"]) / ((1 + jac) * o["shingles"]))
        # What's said decides when both have enough of it (a talk show's episodes share a studio: their frames look
        # alike). Frames decide only for videos with little speech, and must match more (0.8 against 0.6).
        if nsh >= 300 and o["shingles"] >= 300:
            ab, ba, need = said_ab, said_ba, 0.6
        else:
            if not (mean is not None and o["frames"] and len(frames) >= 10 and o["nframes"] >= 10
                    and float(mean @ np.frombuffer(o["frames"], np.float32)) > 0.85):
                continue
            other = search.job_frames(o["job_id"])
            sims = frames @ other.T
            seen_ab, seen_ba = float((sims.max(1) > 0.92).mean()), float((sims.max(0) > 0.92).mean())
            ab, ba, need = seen_ab, seen_ba, 0.8
        kind = "same" if ab >= need and ba >= need else "clip" if max(ab, ba) >= need else None
        if kind:
            a, b = (jid, o["job_id"]) if jid < o["job_id"] else (o["job_id"], jid)
            a_in_b, b_in_a = (ab, ba) if a == jid else (ba, ab)
            q("INSERT OR REPLACE INTO similar (a, b, kind, a_in_b, b_in_a, updated) VALUES (?,?,?,?,?,?)",
              (a, b, kind, round(a_in_b, 3), round(b_in_a, 3), time.time()))
            found += 1
    return {"found": found, "shingles": nsh, "frames": 0 if frames is None else len(frames)}


CHAPTERS_TASK = """TASK: split this video into chapters from the subtitles above. Give 4-12 chapters covering the
whole video in order: where each starts (seconds, taken from a block's time) and a short Chinese title (at most 16
characters) naming the topic, not "第一部分". Then, for each numbered KEY POINT below, the time (seconds) where it is
said or argued most directly, or null.
Reply with one JSON object: {"chapters": [{"t": <seconds>, "title": "..."}], "points": [<seconds or null>, ...]}

KEY POINTS:
"""


@board.task("chapters", "章节", "ai-quick", remote=True)
def pi_chapters(task, beat):
    """Chapters of a video, and where each key point of its summary is said, from its subtitles (one AI call
    without thinking: ~¥0.03 for an hour and a half). The watch page lists both; a click jumps there."""
    jid, n = task["payload"]["job"], task["payload"]["part"]
    text, lines, end = llm.timed_transcript(jid, n)  # ~30-second blocks keep the input small
    if lines < 20:
        return {"skipped": "too few subtitles"}
    a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
    points = (a.get("key_points") or []) if n == 0 else []
    user = f"{text}\n\n{CHAPTERS_TASK}" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(points))
    out = llm.llm_json(llm.TRANSCRIPT_SYSTEM, user, {"chapters": "array", "points": "array"}, 4000, {}, "chapters", jid,
                       think=False)
    chapters = sorted(({"t": float(c["t"]), "title": str(c.get("title") or "")[:24]} for c in out.get("chapters") or []
                       if isinstance(c, dict) and isinstance(c.get("t"), (int, float)) and 0 <= c["t"] <= end + 60),
                      key=lambda c: c["t"])
    times = [float(t) if isinstance(t, (int, float)) and 0 <= t <= end + 60 else None for t in (out.get("points") or [])]
    a = json.loads(q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
    a.setdefault("chapters", {})[str(n)] = chapters
    if n == 0 and len(times) == len(points):
        a["point_times"] = times
    update(jid, analysis=a)
    return {"chapters": len(chapters), "points": sum(t is not None for t in times)}


@board.task("index_subs", "整理已有字幕", "light", remote=True)
def pi_index_subs(task, beat):
    path, subs = board.task_media(task)
    jid, n = task["payload"]["job"], task["payload"]["part"]
    if not subs:
        return {"lines": 0}
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕'", (jid, n))
    cues = library._cues(subs[0], Path(subs[0]).stat().st_mtime)  # the first track; others are mostly the same lines translated
    for t, text in cues:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,?,?,'字幕',?)", (jid, n, t, text))
    maybe_translate(task, path, subs)
    board.publish("chapters", task["target"], task["priority"] - 1, parent=task["id"], force=True)
    return {"lines": len(cues), "file": Path(subs[0]).name}


@board.task("cover", "识别封面", "cpu", prefer="gpu", remote=True, then="save_cover")
def pi_cover(task, beat):
    """What the cover looks like and the text on it, worked out on the Pi's CPU (~30 s; the Mac takes ~0.2 s)."""
    thumb, started = library.job_thumb(task), time.time()
    v = search.clip_image(thumb)
    return {"vector": search.vec_to(v) if v is not None else None, "lines": search.ocr_text(thumb), "seconds": round(time.time() - started, 1)}


@board.task("save_cover", "保存封面识别", "light")
def pi_save_cover(task, beat):
    jid, res = task["payload"]["job"], board.parent_result(task)
    q("DELETE FROM vec WHERE kind='job' AND ref=? AND src='封面'", (jid,))
    q("DELETE FROM seg WHERE kind='job' AND ref=? AND src='封面文字'", (jid,))
    if res.get("vector"):
        search.store_vec("job", jid, 0, None, "封面", search.vec_from(res["vector"]), res.get("model") or search.CLIP_MODEL)
    for line in res.get("lines") or []:
        q("INSERT INTO seg (kind, ref, part, t, src, text) VALUES ('job',?,0,NULL,'封面文字',?)", (jid, str(line)))
    log_usage("clip", "cover", jid, amount=1, seconds=float(res.get("seconds") or 0))
    board.drop_parent_result(task, {"lines": len(res.get("lines") or [])})
    return {"lines": len(res.get("lines") or [])}


@board.task("frames", "识别画面", "cpu", prefer="gpu", remote=True, then="save_frames")
def pi_frames(task, beat):
    """What's on screen, keyframe by keyframe, on the Pi's CPU (~3 s a frame; the Mac does them 100x faster).
    A frame much like the last one kept is skipped."""
    path, _ = board.task_media(task)
    started, kept, prev = time.time(), [], None
    with tempfile.TemporaryDirectory(dir=INCOMPLETE / "tmp") as tmp:
        frames = library.keyframes(path, Path(tmp))
        for i, (t, f) in enumerate(frames):
            if i % 20 == 0 and (not board.pi_idle() or not beat({"pct": round(i / max(len(frames), 1) * 100)})):
                raise board.Paused()  # starts over next time: the keyframes are quick, the vectors are the slow part
            v = search.clip_image(f)
            if v is not None and (prev is None or float(v @ prev) < 0.85):  # same shot as the last kept: skip
                kept.append([round(t, 2), search.vec_to(v)])
                prev = v
    return {"frames": kept, "keyframes": len(frames), "seconds": round(time.time() - started, 1)}


@board.task("save_frames", "保存画面识别", "light")
def pi_save_frames(task, beat):
    jid, n, res = task["payload"]["job"], task["payload"]["part"], board.parent_result(task)
    q("DELETE FROM vec WHERE kind='job' AND ref=? AND part=? AND src='画面'", (jid, n))
    for t, b64 in res.get("frames") or []:
        search.store_vec("job", jid, n, float(t), "画面", search.vec_from(b64), res.get("model") or search.CLIP_MODEL)
    log_usage("clip", "frames", jid, amount=res.get("keyframes") or 0, seconds=float(res.get("seconds") or 0))
    board.publish("similar", f"job:{jid}", task["priority"] - 3, parent=task["id"], force=True)
    board.drop_parent_result(task, {"kept": len(res.get("frames") or []), "keyframes": res.get("keyframes")})
    return {"kept": len(res.get("frames") or [])}


@board.task("transcribe", "转文字", "cpu", prefer="gpu", remote=True, then=after_transcribe)
def pi_transcribe(task, beat):
    """Speech-to-text on the Pi's CPU (whisper small, ~1x realtime) when no Mac is around: 90 seconds at a time,
    the progress kept on the board, so a pause loses nothing."""
    from faster_whisper import WhisperModel
    import numpy as np
    path, _ = board.task_media(task)
    prog = task["progress"] if (task["progress"] or {}).get("by") == "pi" else {}
    until, segs, lang = prog.get("until", 0), prog.get("segs", []), prog.get("language") or task["payload"].get("language")
    dur, chunk, started = library.duration_of(str(path)) or 0, 90, time.time()
    model = None
    try:
        while until < dur:
            if not board.pi_idle():
                raise board.Paused()
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(until), "-i", str(path), "-t", str(chunk), "-vn",
                                  "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
            if not pcm:
                break
            audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            if model is None:
                model = WhisperModel(IDLE_WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4,
                                     download_root=str(STATE / "models"))
            with heavy_slot("whisper"):
                if lang is None:
                    _, info = model.transcribe(audio[:30 * 16000], beam_size=1)
                    lang = info.language
                # the prompt keeps Chinese in simplified characters (search is by simplified text)
                found, _ = model.transcribe(audio, language=lang, vad_filter=True, beam_size=1,
                                            initial_prompt="以下是普通话的句子，用简体中文。" if lang == "zh" else None,
                                            **WHISPER_FAST)
                segs += [[round(until + s.start, 2), round(until + s.end, 2), s.text.strip()] for s in found if s.text.strip()]
            until += chunk
            if not beat({"by": "pi", "until": until, "segs": segs, "language": lang, "pct": round(min(until / dur, 1) * 100)}):
                raise board.Paused()  # the task isn't ours any more
    finally:
        prog_seconds = time.time() - started
    return {"language": lang, "segments": segs, "audio_seconds": round(min(until, dur), 1), "seconds": round(prog_seconds, 1)}


def note_files(nid):
    row = q("SELECT media FROM notes WHERE id=?", (nid,), one=True)
    return [m for m in json.loads(row["media"]) if m.get("todo")] if row else []


@board.task("note_media", "识别随记附件", "now", prefer="gpu", prefer_wait=90, then="save_note_media")
def pi_note_media(task, beat):
    """Photos: the text on them and what they look like; videos: what's said and what the first frame looks like;
    voice: what's said. Worked out here when the Mac doesn't take it within a minute and a half."""
    out = {}
    for m in note_files(task["payload"]["note"]):
        path, item = NOTES_DIR / m["file"], {}
        try:
            if m["kind"] == "image":
                item["ocr"] = "\n".join(search.ocr_text(path))
                v = search.clip_image(path)
            else:
                item["duration"] = float(ffprobe(path).get("format", {}).get("duration") or 0) or None
                v = search.clip_image(path, t=min(1.0, (item["duration"] or 2) / 2)) if m["kind"] == "video" else None
                # the prompt steers Chinese towards simplified characters (search is by simplified text)
                text, _ = library.transcribe(None, path, None, prompt="以下是普通话的日常随记，用简体中文。", model=NOTE_WHISPER_MODEL,
                                     purpose="note")
                item["transcript"] = (text or "").strip()
            item["vector"] = search.vec_to(v) if v is not None else None
        except Exception as e:
            traceback.print_exc()
            item["error"] = str(e)[:200]
        out[m["file"]] = item
    return {"items": out}


@board.task("save_note_media", "保存随记识别", "light")
def pi_save_note_media(task, beat):
    """Write what was worked out into the note; the Pi adds what needs the file itself (a video's poster, where a
    photo or video was taken)."""
    nid = task["payload"]["note"]
    items = board.parent_result(task).get("items") or {}
    row = q("SELECT media FROM notes WHERE id=?", (nid,), one=True)
    if not row:
        return {"skipped": "note deleted"}
    media = json.loads(row["media"])
    for m in media:
        it = items.get(m["file"])
        if it is None:
            continue
        path = NOTES_DIR / m["file"]
        m.update({k: it[k] for k in ("ocr", "transcript", "duration") if k in it}, todo=False)
        try:
            gps = notes.photo_gps(path) if m["kind"] == "image" else notes.video_gps(path) if m["kind"] == "video" else None
            if gps:
                m["gps"], m["place"] = [round(gps[0], 5), round(gps[1], 5)], notes.place_name(*gps)
            if m["kind"] == "video" and library.grab_frame(path, path.with_suffix(".poster.jpg")):
                m["poster"] = path.with_suffix(".poster.jpg").name
        except Exception:
            traceback.print_exc()
        q("DELETE FROM vec WHERE kind='note' AND ref=? AND src=?", (nid, "照片:" + m["file"]))
        if it.get("vector"):
            search.store_vec("note", nid, 0, None, "照片:" + m["file"], search.vec_from(it["vector"]),
                             it.get("model") or search.CLIP_MODEL)
    q("UPDATE notes SET media=?, pending=? WHERE id=?",
      (json.dumps(media, ensure_ascii=False), int(any(m.get("todo") for m in media)), nid))
    board.drop_parent_result(task, {"items": len(items)})
    board.publish("note_ai", f"note:{nid}", 30, force=True)  # now there's what was said: tags, punctuation
    return {"items": len(items)}


NOTE_AI_SYSTEM = """You help with someone's private notes (a diary). Given one note: its text, what was said in its voice
recordings or videos (speech-to-text: unpunctuated, may have misheard words), text read off its photos, and where it
was made. Reply with one JSON object: {{"tags": [...], "tidy": {{"<file>": "...", ...}}}}
- tags: 1-4 short Chinese tags for what the note is about (a person's name, a place, an activity, a topic). No dates.
- tidy: for each recording given, the same words with punctuation and paragraphs (blank lines between them), obvious
  mishearings fixed only when the meaning is clear; never add or drop content. Leave out recordings under a sentence."""


@board.task("note_ai", "整理随记", "ai-quick")
def pi_note_ai(task, beat):
    """A 随记's tags, and its voice recordings with punctuation (one call without thinking, ~¥0.001)."""
    nid = task["payload"]["note"]
    row = q("SELECT text, media FROM notes WHERE id=?", (nid,), one=True)
    if not row:
        return {"skipped": "note deleted"}
    media = json.loads(row["media"])
    said = {m["file"]: m["transcript"] for m in media if len(m.get("transcript") or "") >= 12}
    parts = [f"TEXT: {row['text']}"] + [f"RECORDING {f}: {t}" for f, t in said.items()]
    parts += [f"ON A PHOTO: {m['ocr']}" for m in media if m.get("ocr")]
    parts += [f"PLACE: {m['place']}" for m in media if m.get("place")][:1]
    if len(row["text"].strip()) < 4 and not said and len(parts) == 1:
        return {"skipped": "nothing to read"}
    out = llm.llm_json(NOTE_AI_SYSTEM, "\n".join(parts)[:12000], {"tags": "array", "tidy": "object"}, 3000, {}, "notes",
                       think=False)
    tags = [str(t).strip()[:12] for t in out.get("tags") or [] if str(t).strip()][:4]
    tidy = out.get("tidy") if isinstance(out.get("tidy"), dict) else {}
    row = q("SELECT media FROM notes WHERE id=?", (nid,), one=True)  # re-read: it may have changed meanwhile
    if not row:
        return {"skipped": "note deleted"}
    media = [{**m, "tidy": str(tidy[m["file"]])} if m["file"] in said and tidy.get(m["file"]) else m
             for m in json.loads(row["media"])]
    q("UPDATE notes SET tags=?, media=? WHERE id=?",
      (json.dumps(tags, ensure_ascii=False), json.dumps(media, ensure_ascii=False), nid))
    return {"tags": tags, "tidied": len([f for f in said if tidy.get(f)])}


NOTES_RECAP_SYSTEM = """You write a short look back at someone's private notes from one week, in Simplified Chinese, plainly
and warmly, like a friend recalling the week with them: 2-4 sentences on what they did, saw and thought, naming the
places and people that come up. Don't invent anything that isn't in the notes. Then up to 4 highlights, each one line
pointing at one note by its id.
Reply with one JSON object: {{"text": "...", "highlights": [{{"note": <id>, "line": "..."}}]}}"""


@board.task("notes_recap", "随记一周回顾", "ai-quick")
def pi_notes_recap(task, beat):
    p = task["payload"]
    t0 = time.mktime(time.strptime(p["start"], "%Y-%m-%d"))
    t1 = time.mktime(time.strptime(p["end"], "%Y-%m-%d")) + 86400
    rows = q("SELECT * FROM notes WHERE owner=? AND created >= ? AND created < ? ORDER BY created", (p["owner"], t0, t1))
    if not rows:
        return {"notes": 0}
    lines = []
    for r in rows:
        media = json.loads(r["media"])
        said = " ".join(m.get("tidy") or m.get("transcript") or "" for m in media)
        place = next((m["place"] for m in media if m.get("place")), "")
        photos = sum(m["kind"] == "image" for m in media)
        lines.append(f"note {r['id']} {time.strftime('%m-%d %a %H:%M', time.localtime(r['created']))} {place}"
                     f"{' (' + str(photos) + ' photos)' if photos else ''}: {r['text']} {said}"[:1200])
    out = llm.llm_json(NOTES_RECAP_SYSTEM, "\n".join(lines)[:30000], {"text": "string", "highlights": "array"}, 3000, {},
                       "notes", think=False)
    ids = {r["id"] for r in rows}
    recap = {"start": p["start"], "end": p["end"], "text": str(out.get("text") or ""), "notes": len(rows),
             "highlights": [{"note": llm.as_int(h.get("note")), "line": str(h.get("line") or "")}
                            for h in out.get("highlights") or [] if isinstance(h, dict) and llm.as_int(h.get("note")) in ids]}
    kv_set(f"notes_recap:{p['owner']}", recap)
    return {"notes": len(rows)}


FAILURE_SYSTEM = """A download in a home media box failed. From the link's site and the error output, say in one short
Chinese sentence why (for someone who isn't a programmer), and in another what to do: wait and retry, log in (give the
box a cookies file), the video is gone or private, region-locked, the site isn't supported, a members-only video...
Reply with one JSON object: {{"cause": "...", "fix": "..."}}"""


@board.task("explain_failure", "解释下载失败", "ai-quick")
def pi_explain_failure(task, beat):
    """Why a download failed, in plain words, and what to do (one call without thinking, ~¥0.0005)."""
    jid = task["payload"]["job"]
    row = q("SELECT url, error, attempts, status FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] != "failed" or not row["error"]:
        return {"skipped": "not failed"}
    host = urllib.parse.urlsplit(row["url"]).netloc or row["url"][:40]
    out = llm.llm_json(FAILURE_SYSTEM, f"SITE: {host}\nATTEMPTS: {row['attempts']}\nERROR:\n{row['error'][-1500:]}",
                       {"cause": "string", "fix": "string"}, 600, {}, "failure", jid, think=False)
    advice = f"{out.get('cause') or ''} {out.get('fix') or ''}".strip()
    q("UPDATE jobs SET advice=? WHERE id=?", (advice, jid))
    return {"advice": advice}


@board.task("llm", "AI（Mac 上的 Claude）", "mac")
def pi_llm(task, beat):
    """A request for Claude (llm.claude_json): only the Mac's claim loop does these; the asker falls back to DeepSeek."""
    raise ValueError("only the Mac answers these")


@board.task("book_import", "导入电子书", "now")
def pi_book_import(task, beat):
    """A book just added (uploaded, a link, the Mac's drop folder): read it into chapters / pages, its text and the
    pack phones cache. Someone is waiting for it, so it's done right away."""
    bid = task["payload"]["book"]
    try:
        return books.import_book(bid, beat)
    except books.BookError as e:  # not a readable book: say why on the shelf, don't try again
        q("UPDATE books SET status='failed', error=?, updated=? WHERE id=?", (str(e), time.time(), bid))
        raise ValueError(str(e))
    except Exception as e:  # the network, a full disk...: the board tries again (1, 4, 9, 16 minutes)
        last = (q("SELECT attempts FROM tasks WHERE id=?", (task["id"],), one=True) or {"attempts": 5})["attempts"] >= 5
        q("UPDATE books SET status=?, error=?, updated=? WHERE id=?",
          ("failed" if last else "importing", (f"导入出错{'' if last else '，稍后自动重试'}：{e}")[:300], time.time(), bid))
        raise


@once("notes_ai_backfilled", background=True)
def backfill_note_ai():
    """Once: tags and tidied recordings for the notes written before there were any; and plain-words reasons for
    downloads that have already failed for good."""
    for r in q("SELECT id FROM notes"):
        board.publish("note_ai", f"note:{r['id']}", 5)
    for r in q("SELECT id FROM jobs WHERE status='failed' AND retry_at IS NULL AND error != ''"):
        board.publish("explain_failure", f"job:{r['id']}", 5)
