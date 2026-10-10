"""Media files: making them play everywhere, speech-to-text, filing into the library, Plex, playing in the browser."""
import contextlib
import fcntl
import functools
import hashlib
import hmac
import json
import os
import re
import requests
import shutil
import subprocess
import threading
import time
import traceback

from flask import request
from pathlib import Path
from .migrations import once
from .core import (AUDIO_EXT, MEDIA, STATE, SUB_EXT, TRANSCRIBE_MAX_MIN, VIDEO_EXT, VIDEO_FOLDERS, WHISPER_FAST, WHISPER_MODEL, app, check_cancel, ffprobe, heavy_slot, job_dict, log_usage, q, safe_name, unique_path, update)


# ---------------------------------------------------------------- post-processing

def make_plex_friendly(job_id, path: Path, keep_mkv=False):
    """Pi can't transcode on the fly, so fix files Plex clients can't play directly.
    MKVs from video sites become MP4 when their streams allow it, so phone browsers can play them too
    (torrents keep MKV: they often carry several audio/subtitle tracks MP4 can't hold)."""
    info = ffprobe(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        return path
    vcodec, acodec = v.get("codec_name"), (a or {}).get("codec_name")
    # VP9/AV1 direct-play on current Plex apps; re-encoding them on the Pi takes longer than the video itself
    good_video = vcodec in ("h264", "hevc", "vp9", "av1")
    good_audio = acodec in (None, "aac", "ac3", "eac3", "mp3", "flac") or (path.suffix == ".mkv" and acodec in ("opus", "dts", "truehd"))
    good_container = path.suffix.lower() in (".mp4", ".mkv", ".m4v", ".mov", ".webm")
    if (good_video and good_audio and good_container and not keep_mkv and path.suffix.lower() == ".mkv"
            and vcodec in ("h264", "hevc") and acodec in (None, "aac", "mp3", "ac3", "eac3")):
        update(job_id, stage="making it Plex-friendly")
        out = path.with_suffix(".mp4")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                            "-tag:v", "hvc1" if vcodec == "hevc" else "avc1", "-movflags", "+faststart", str(out)],
                           capture_output=True)
        if r.returncode == 0:
            path.unlink()
            return out
        out.unlink(missing_ok=True)
        return path
    if good_video and good_audio and good_container:
        return path
    update(job_id, stage="making it Plex-friendly")
    out = path.with_name(path.stem + ".plex.mkv" if path.suffix == ".mkv" else path.stem + ".plex.mp4")
    cmd = ["nice", "-n", "15", "ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
           "-map", "0:s?", "-c:s", "copy" if out.suffix == ".mkv" else "mov_text"]
    if good_video:
        cmd += ["-c:v", "copy"]
    else:
        height = int(v.get("height") or 1080)
        if height > 1080:
            cmd += ["-vf", "scale=-2:1080"]
            height = 1080
        bitrate = {480: "2M", 720: "4M"}.get(min((480, 720, 1080), key=lambda h: abs(h - height)), "8M")
        # Pi 4 hardware H.264 encoder
        cmd += ["-c:v", "h264_v4l2m2m", "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if good_audio else ["-c:a", "aac", "-b:a", "192k"]
    if out.suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    with heavy_slot("encode", job_id):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and not good_video:
            # Hardware encoder can't take some inputs; fall back to software (slow but works)
            cmd[cmd.index("h264_v4l2m2m")] = "libx264"
            cmd[cmd.index("-b:v"):cmd.index("-b:v") + 2] = ["-preset", "veryfast", "-crf", "22"]
            r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        return path  # keep the original rather than failing the whole job
    path.unlink()
    final = path.with_suffix(out.suffix)
    out.rename(final)
    return final


def grab_frame(video: Path, dest: Path):
    dur = float(ffprobe(video).get("format", {}).get("duration") or 0)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(max(dur * 0.1, 1) if dur else 1), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=640:-2", str(dest)], capture_output=True)
    return dest if dest.exists() else None


def srt_to_text(path: Path):
    text = path.read_text(errors="ignore")
    lines = [l.strip() for l in text.splitlines()
             if l.strip() and not l.strip().isdigit() and "-->" not in l]
    out = []
    for l in lines:  # auto-captions repeat lines a lot
        l = re.sub(r"<[^>]+>", "", l)
        if not out or out[-1] != l:
            out.append(l)
    return "\n".join(out)


_whisper = None
_whisper_lock = threading.Lock()


def transcribe(job_id, media: Path, srt_out: Path | None, prompt=None, model=None, purpose="job"):
    """Speech-to-text of at most TRANSCRIBE_MAX_MIN minutes of audio. Longer videos are sampled: equal pieces from
    the start, middle and end, which is enough to summarise and keeps it to a few minutes on the Pi.
    Writes a sidecar .srt only when it covered the whole video."""
    global _whisper
    from faster_whisper import WhisperModel
    update(job_id, stage="transcribing speech")
    dur = float(ffprobe(media).get("format", {}).get("duration") or 0)
    limit = TRANSCRIBE_MAX_MIN * 60
    # Decode with ffmpeg ourselves (faster-whisper's PyAV decoding breaks with newer PyAV releases)
    import numpy as np
    def decode(start, length):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(media), "-t", str(length), "-vn",
                               "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True).stdout
    # (offset in the decoded audio, time in the media) of each piece, to give segments their real times
    spans = []
    if dur and dur > limit:
        piece = limit / 3
        chunks = [(st, decode(st, piece)) for st in (0, dur / 2 - piece / 2, dur - piece)]
    else:
        chunks = [(0, decode(0, limit))]
    at = 0
    for st, chunk in chunks:
        spans.append((at, st))
        at += len(chunk) / 32000  # 16 kHz, 16-bit mono
    pcm = b"".join(c for _, c in chunks)
    if not pcm:
        return None, None
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    started = time.time()
    with _whisper_lock, heavy_slot("whisper", job_id):
        if _whisper is None:
            _whisper = WhisperModel(model or WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4,
                                    download_root=str(STATE / "models"))
        segments, info = _whisper.transcribe(audio, vad_filter=True, beam_size=1, initial_prompt=prompt, **WHISPER_FAST)
        segs = []
        for s in segments:
            check_cancel(job_id)
            segs.append(s)
            if dur:
                update(job_id, progress=round(min(s.end / min(dur, limit), 1) * 100, 1))
    text = "\n".join(s.text.strip() for s in segs)
    log_usage("whisper", purpose, job_id, amount=len(audio) / 16000, seconds=time.time() - started)
    def real(t):
        off, st = max((sp for sp in spans if sp[0] <= t), default=(0, 0))
        return round(st + t - off, 1)
    if job_id is not None:  # lets a search jump to the moment something is said
        update(job_id, segments=json.dumps([[real(s.start), s.text.strip()] for s in segs], ensure_ascii=False))
    if srt_out and segs and dur and dur <= limit:
        def ts(t):
            h, rem = divmod(t, 3600)
            m, s = divmod(rem, 60)
            return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"
        srt = "\n".join(f"{i}\n{ts(s.start)} --> {ts(s.end)}\n{s.text.strip()}\n" for i, s in enumerate(segs, 1))
        srt_out.with_name(f"{srt_out.stem}.{info.language}.srt").write_text(srt)
    note = "" if not dur or dur <= limit else \
        f"[transcript of {TRANSCRIBE_MAX_MIN} sampled minutes (start, middle, end) of a {int(dur // 60)}-minute video]"
    return text, (info.language, note)


# ---------------------------------------------------------------- filing into the library

def destination(a, ext, original_stem):
    title = safe_name(a.get("title") or original_stem)
    lib = a.get("library") or "Videos"
    if lib == "Movies" and a.get("year"):
        d = MEDIA / "Movies" / f"{title} ({a['year']})"
        return d, f"{title} ({a['year']})"
    if lib == "TV" and a.get("show") and a.get("episode") is not None:
        show = safe_name(a["show"])
        season = 1 if a.get("season") is None else int(a["season"])  # 0: the show's specials
        return MEDIA / "TV" / show / f"Season {season:02}", f"{show} - S{season:02}E{int(a['episode']):02}"
    if lib == "Music" and ext in AUDIO_EXT:
        return MEDIA / "Music" / safe_name(a.get("artist") or "Unknown Artist") / safe_name(a.get("album") or "Singles"), title
    if lib == "Downloads":
        return MEDIA / "Downloads", original_stem
    folder = a.get("folder") if a.get("folder") in VIDEO_FOLDERS else "Other"
    return MEDIA / "Videos" / folder, title


# One show, one place. The classifier files each video on its own, so a followed show's episodes went to 剧集 while
# its trailers, OP/ED and behind-the-scenes clips landed in 片段 / 音乐MV, and some extras became episodes with
# made-up numbers (B站 numbers a show's extras after its episodes: a PV was "第529集"). A show's web videos are kept
# together: episodes in their season, everything else of the show in Season 00 (Plex's Specials).
EXTRA_RE = re.compile(r"预告|花絮|片头曲|片尾曲|主题曲|插曲|导演剪辑|周年|纪念|定档|里程碑|前瞻|先导|彩蛋|幕后|动捕|宣传片|"
                      r"予告|特報|ノンクレジット|trailer|teaser|(?<![A-Za-z])(?:PV|OP|ED|MV|NCOP|NCED|CM|SP)(?![A-Za-z])", re.I)


def tv_shows():
    """{show: where its episodes came from (sources: "sub:12", "web"...)} for every show in 剧集."""
    out = {}
    for r in q("SELECT source, json_extract(analysis, '$.show') show FROM jobs WHERE status='done' "
               "AND json_extract(analysis, '$.library')='TV' AND json_extract(analysis, '$.show') IS NOT NULL"):
        out.setdefault(str(r["show"]), set()).add(r["source"])
    return out


def show_of(a, source, titles, shows):
    """The show a video belongs to: the one the classifier filed it under; for a followed uploader's video filed
    elsewhere, a show the same follow already has episodes of in 剧集, named in its title or tags."""
    if a.get("library") == "TV" and a.get("show"):
        return str(a["show"])
    if a.get("library") != "Videos" or not str(source or "").startswith("sub:"):
        return None
    text = " ".join([*titles, str(a.get("show") or ""), *map(str, a.get("tags") or [])])
    return next((s for s in sorted(shows, key=len, reverse=True) if len(s) >= 2 and source in shows[s] and s in text), None)


def place_in_show(a, source, titles, shows=None, taken=None):
    """File a web video with its show (changes `a`). It stays an episode when it is one: the classifier gave it a
    number, its title doesn't say trailer/OP/花絮..., and the number is in its title or description. Anything else of
    the show becomes its next special. `taken`: {show: special numbers handed out but not on disk yet}."""
    show = show_of(a, source, titles, tv_shows() if shows is None else shows)
    if not show:
        return a
    ep = a.get("episode")
    numbers = {int(x) for x in re.findall(r"\d+", " ".join([*titles, str(a.get("summary") or "")]))}
    episode = (a.get("library") == "TV" and ep is not None and a.get("season") != 0 and ep in numbers
               and not any(EXTRA_RE.search(t or "") for t in titles))
    a.update(library="TV", show=show, folder="Other")
    if episode:
        a["season"] = a.get("season") or 1
        return a
    used = {int(m.group(1)) for p in (MEDIA / "TV" / safe_name(show) / "Season 00").glob("*")
            if (m := re.search(r" - S00E(\d+)\b", p.name))}
    used |= {int(r["e"]) for r in q("SELECT json_extract(analysis, '$.episode') e FROM jobs WHERE status='done' AND "
                                    "json_extract(analysis, '$.show')=? AND json_extract(analysis, '$.season')=0", (show,))
             if r["e"] is not None}
    taken = {} if taken is None else taken
    n = max(used | taken.setdefault(show, set()), default=0) + 1
    taken[show].add(n)
    a.update(season=0, episode=n)
    return a


@once("shows_together")
def refile_shows(dry_run=False):
    """Once: put the web videos of shows where place_in_show puts new ones (oldest first, so specials are numbered
    in the order they came out), moving their files. Returns what was (or with dry_run, would be) moved."""
    shows, taken, done = tv_shows(), {}, []
    for r in q("SELECT * FROM jobs WHERE status='done' AND ref IS NULL AND COALESCE(kind, '') NOT IN ('torrent', 'file') "
               "ORDER BY COALESCE(json_extract(analysis, '$.published'), ''), id"):
        a = json.loads(r["analysis"] or "{}")
        if a.get("library") == "TV" and a.get("season") == 0:
            continue  # already a special
        b = place_in_show(dict(a), r["source"], [str(a.get("title") or ""), r["title"] or ""], shows, taken)
        if all(a.get(k) == b.get(k) for k in ("library", "show", "season", "episode")):
            continue
        moved = refile(r, b, dry_run)
        if moved:
            done.append(moved)
    if done and not dry_run:
        plex_refresh()
    print("\n".join(["shows_together:", *done]), flush=True)
    return done


def refile(r, b, dry_run=False):
    """Move finished job `r`'s video (and its subtitles, cover) to where analysis `b` files it, and store `b`.
    Returns "#id old -> new", or None when it has no single video to move."""
    files = json.loads(r["files"] or "[]")
    media = [Path(f) for f in files if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT]
    if len(media) != 1 or not media[0].exists():
        return None  # several videos from one page keep their numbered names
    main = media[0]
    dest_dir, base = destination(b, main.suffix.lower(), main.stem)
    line = f"#{r['id']} {main.relative_to(MEDIA)} -> {(dest_dir / base).relative_to(MEDIA)}{main.suffix}"
    if dry_run:
        return line
    moved = move_with_sidecars(main, dest_dir, base)
    new = {str(main.with_name(main.stem + p.name[len(moved[0].stem):])): str(p) for p in moved}
    files = [new.get(f, f) for f in files]
    thumb = new.get(r["thumb"], r["thumb"])
    for jid in [r["id"]] + [x["id"] for x in q("SELECT id FROM jobs WHERE ref=?", (r["id"],))]:
        update(jid, analysis=b, files=files, thumb=thumb)
    probe_of(str(moved[0]))  # so the list doesn't have to ffprobe it later
    return line


def move_with_sidecars(main: Path, dest_dir: Path, base: str):
    """Move a media file plus its subtitles/thumbnail (same stem) into dest_dir as base.*"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(dest_dir / f"{base}{main.suffix}")
    base = target.stem
    moved = [target]
    for side in main.parent.iterdir():
        if side == main or not side.name.startswith(main.stem + "."):
            continue
        rest = side.name[len(main.stem):]  # e.g. ".en.srt", ".jpg"
        if side.suffix.lower() in SUB_EXT | {".jpg", ".png", ".webp"}:
            shutil.move(str(side), str(dest_dir / f"{base}{rest}"))
            moved.append(dest_dir / f"{base}{rest}")
    shutil.move(str(main), str(target))
    return moved


def plex_refresh():
    """Ask Plex to rescan (its file watching doesn't see changes made through the mergerfs pool)."""
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if not token:
        return
    try:
        sections = requests.get("http://127.0.0.1:32400/library/sections", timeout=10,
                                headers={"X-Plex-Token": token, "Accept": "application/json"}).json()
        for d in sections["MediaContainer"].get("Directory", []):
            requests.get(f"http://127.0.0.1:32400/library/sections/{d['key']}/refresh", timeout=10,
                         headers={"X-Plex-Token": token})
    except Exception:
        traceback.print_exc()


def plex_text(a):
    text = a.get("summary") or ""
    if a.get("key_points"):
        text += "\n\n" + "\n".join("• " + p for p in a["key_points"])
    return text.strip()


def plex_set_metadata(items):
    """Write our summary/tags into Plex for videos in the Videos library (Plex has no metadata source for those;
    films and series get Plex's own), and the name and summary of 剧集 episodes Plex has nothing for (a new
    episode its database doesn't list yet shows as "Episode 195"). items: [(file path, analysis)]. Waits for the scan."""
    token = os.environ.get("PLEX_TOKEN", "").strip()
    videos, tv = str(MEDIA / "Videos"), str(MEDIA / "TV")
    items = [(str(f), a) for f, a in items if str(f).startswith(videos) and plex_text(a)
             or str(f).startswith(tv) and (plex_text(a) or a.get("episode_title"))]
    if not token or not items:
        return
    base, hdrs = "http://127.0.0.1:32400", {"X-Plex-Token": token, "Accept": "application/json"}
    for _ in range(20):  # the scan usually finishes within seconds
        time.sleep(6)
        try:
            sections = requests.get(f"{base}/library/sections", headers=hdrs, timeout=10).json()["MediaContainer"]["Directory"]
            by_file = {}  # file -> (section, item)
            for root, kind in ((videos, 1), (tv, 4)):
                if not any(f.startswith(root) for f, _ in items):
                    continue
                sec = next(d for d in sections if any(loc["path"] == root for loc in d.get("Location", [])))
                listed = requests.get(f"{base}/library/sections/{sec['key']}/all", params={"type": kind}, headers=hdrs,
                                      timeout=60).json()["MediaContainer"].get("Metadata", [])
                by_file.update({part["file"]: (sec["key"], v) for v in listed for m in v.get("Media", []) for part in m.get("Part", [])})
        except Exception:
            continue
        pending = []
        for f, a in items:
            if f not in by_file:
                pending.append((f, a))
                continue
            sec, v = by_file[f]
            if f.startswith(tv):
                if not str(v.get("guid", "")).startswith("local://"):
                    continue  # Plex knows this episode: its own name and summary
                params = {"type": 4, "id": v["ratingKey"]}
                if a.get("episode_title"):
                    params.update({"title.value": a["episode_title"], "title.locked": 1})
                if plex_text(a):
                    params.update({"summary.value": plex_text(a), "summary.locked": 1})
            else:
                params = {"type": 1, "id": v["ratingKey"], "summary.value": plex_text(a), "summary.locked": 1}
                if a.get("year"):
                    params.update({"year.value": a["year"], "year.locked": 1})
                for i, t in enumerate(a.get("tags", [])[:10]):
                    params[f"genre[{i}].tag.tag"] = t
                if a.get("tags"):
                    params["genre.locked"] = 1
            requests.put(f"{base}/library/sections/{sec}/all", params=params, headers=hdrs, timeout=10)
        items = pending
        if not items:
            return


@functools.lru_cache(maxsize=256)
def _cues(path, mtime):
    """(start seconds, text) of each cue of an .srt / .vtt file."""
    out = []
    text = Path(path).read_text(errors="ignore").replace("\r", "")
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d+)\s*-->", block)
        if m:
            t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + float("0." + m.group(4))
            line = re.sub(r"<[^>]+>", "", block[m.end():].split("\n", 1)[-1]).replace("\n", " ").strip()
            if line and (not out or out[-1][1] != line):
                out.append((round(t, 1), line))
    return out


def sub_lang(path, stem):
    """The language code in a subtitle file's name: "Talk.en-orig.srt" -> "en-orig"."""
    return Path(path).name[len(stem) + 1:].rsplit(".", 1)[0]


def base_lang(code):
    """The language of a sub_lang code: "en-orig" -> "en", B站's AI subtitles "ai-zh" -> "zh" (not a language "ai")."""
    return re.sub(r"^ai-", "", code).split("-")[0]


def srt_cues(path):
    """[(start, end, text)] of an .srt/.vtt, consecutive repeats merged and overlaps cut (auto captions roll: each
    cue starts before the last one ends)."""
    out = []
    text = Path(path).read_text(errors="ignore").replace("\r", "")
    stamp = r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d+)"
    for block in re.split(r"\n\s*\n", text):
        m = re.search(stamp + r"\s*-->\s*" + stamp, block)
        if not m:
            continue
        g = m.groups()
        a = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + float("0." + g[3])
        b = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + float("0." + g[7])
        line = re.sub(r"<[^>]+>", "", block[m.end():].split("\n", 1)[-1]).replace("\n", " ").strip()
        if not line:
            continue
        if out and out[-1][2] == line:
            out[-1][1] = b
            continue
        if out and out[-1][1] > a:
            out[-1][1] = a
        out.append([a, b, line])
    return out


def write_srt(path, cues):
    def ts(t):
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(m):02}:{int(sec):02},{int((sec % 1) * 1000):03}"
    Path(path).write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)))


def job_thumb(task):
    row = q("SELECT thumb FROM jobs WHERE id=?", (task["payload"]["job"],), one=True)
    if not row or not row["thumb"] or not Path(row["thumb"]).exists():
        raise ValueError("no cover")
    return Path(row["thumb"])


def keyframes(path, out_dir):
    """The video's keyframes (every few seconds; the encoder puts them at cuts too) as 224x224 JPEGs named by
    time. Only keyframes are decoded, so the Pi does an hour in about a minute and a half."""
    proc = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "info", "-nostats", "-skip_frame", "nokey", "-i", str(path),
                           "-an", "-fps_mode", "vfr", "-vf", "scale=224:224:flags=bicubic,showinfo", "-q:v", "4",
                           str(out_dir / "%06d.jpg")], capture_output=True, text=True)
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", proc.stderr)]
    files = sorted(out_dir.glob("*.jpg"))
    return [(t, f) for t, f in zip(times, files)]


# ---------------------------------------------------------------- watching in the browser

BROWSER_DIRECT = {".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}


_listings = {}  # folder -> (when read, its file names): the page lists the library every few seconds
LISTING_TTL = 30


def folder_names(folder, fresh=False):
    """The file names in a folder, read at most every LISTING_TTL seconds (listing folders through mergerfs is
    what made the video list slow: ~10 ms a folder, a hundred folders each time the page asked)."""
    hit = _listings.get(folder)
    if fresh or not hit or time.time() - hit[0] > LISTING_TTL:
        try:
            names = set(os.listdir(folder))
        except OSError:
            names = set()
        hit = _listings[folder] = (time.time(), names)
    return hit[1]


def playable(job):
    """Media files of a finished job the page can play, with their subtitle files (new subtitles show up within
    LISTING_TTL seconds)."""
    out = []
    for f in job["files"]:
        p = Path(f)
        if p.suffix.lower() not in VIDEO_EXT | AUDIO_EXT:
            continue
        names = folder_names(str(p.parent))
        if p.name not in names:  # just made, or really gone: look again
            names = folder_names(str(p.parent), fresh=True)
            if p.name not in names:
                continue
        prefix = p.stem + "."
        subs = [str(p.parent / n) for n in sorted(n for n in names if n.startswith(prefix))
                if n.lower().endswith((".srt", ".vtt"))]
        out.append({"path": f, "subs": subs})
    return out


_probes = {}
_mtimes = {}  # path -> (when looked, its modification time)


def probe_of(path):
    """(duration in seconds, video codec) of a media file. ffprobe takes a second or more per file on the Pi's
    disks, and the list shows a hundred of them, so results are kept in the database (by path + modification
    time) and survive restarts; the first page load after a restart used to take minutes."""
    seen = _mtimes.get(path)
    if not seen or time.time() - seen[0] > LISTING_TTL:  # (a stat through mergerfs costs a few ms)
        seen = _mtimes[path] = (time.time(), Path(path).stat().st_mtime)
    key = (path, seen[1])
    if key in _probes:
        return _probes[key]
    row = q("SELECT duration, vcodec FROM probes WHERE path=? AND mtime=?", key, one=True)
    if row:
        _probes[key] = (row["duration"], row["vcodec"])
        return _probes[key]
    info = ffprobe(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic") != 1), {})
    _probes[key] = (float(info.get("format", {}).get("duration") or 0), v.get("codec_name"))
    q("INSERT OR REPLACE INTO probes (path, mtime, duration, vcodec) VALUES (?,?,?,?)", (*key, *_probes[key]))
    return _probes[key]


def duration_of(path):
    return probe_of(path)[0]


def vcodec_of(path):
    return probe_of(path)[1]


def warm_probes():
    """Web start-up: probe any finished file not in the database yet, in the background."""
    for r in q("SELECT files FROM jobs WHERE status='done'"):
        for f in json.loads(r["files"] or "[]"):
            try:
                if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT and Path(f).exists():
                    probe_of(f)
            except Exception:
                pass


def media_token(jid, n, days=7):
    """Signed, expiring token for /play, /subs and /thumb URLs. iOS Safari plays video through a separate
    media process that doesn't reliably send the page's cookies, so these URLs carry their own permission."""
    exp = int(time.time() + days * 86400)
    sig = hmac.new(app.secret_key.encode(), f"{jid}:{n}:{exp}".encode(), "sha256").hexdigest()[:24]
    return f"{exp}.{sig}"


def token_ok(jid, n):
    try:
        exp, sig = request.args.get("t", "").split(".")
        good = hmac.new(app.secret_key.encode(), f"{jid}:{n}:{exp}".encode(), "sha256").hexdigest()[:24]
        return int(exp) > time.time() and hmac.compare_digest(sig, good)
    except ValueError:
        return False


def media_info(job, days=7):
    jid = job["id"]
    return [{"name": Path(m["path"]).stem, "audio": Path(m["path"]).suffix.lower() in AUDIO_EXT,
             "duration": duration_of(m["path"]), "vcodec": vcodec_of(m["path"]),
             "src": f"/play/{jid}/{i}?t={media_token(jid, i, days)}",
             "subsrc": [f"/subs/{jid}/{i}/{k}?t={media_token(jid, i, days)}" for k in range(len(m["subs"]))],
             "subs": [Path(x).name[len(Path(m["path"]).stem) + 1:].rsplit(".", 1)[0] or "字幕" for x in m["subs"]],
             "bilingual": f"/subs/{jid}/{i}/bi?t={media_token(jid, i, days)}" if bilingual_pair(m) else None}
            for i, m in enumerate(playable(job))]


def bilingual_pair(m):
    """(Chinese, original) subtitle files of a media item, if it has both: English, else another language a Chinese
    track was translated from."""
    stem = Path(m["path"]).stem
    by = {sub_lang(x, stem): x for x in m["subs"]}
    zh = next((by[k] for k in by if base_lang(k) == "zh"), None)
    other = by.get("en") or next((by[k] for k in by if base_lang(k) == "en"), None) \
        or next((by[k] for k in by if base_lang(k) not in ("zh", "und", "")), None)
    return (zh, other) if zh and other else None


def job_media(jid, n):
    if not (token_ok(jid, n) or web.visible(jid)):
        return None
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row:  # deleted meanwhile; a signed link outlives the job
        return None
    items = playable(job_dict(row))
    return items[n] if 0 <= n < len(items) else None


PLAY_CACHE = MEDIA / ".cache" / "play"


@contextlib.contextmanager
def remux_lock(out: Path):
    """The web process (someone pressed play) and a job process (just finished) may both convert the same file:
    a file lock keeps them from writing the same output at once (16 lock files, picked by the hash)."""
    (STATE / "locks").mkdir(parents=True, exist_ok=True)
    with open(STATE / "locks" / f"remux-{out.name[0]}.lock", "w") as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        yield


def remux_for_browser(path: Path):
    st = path.stat()
    out = PLAY_CACHE / f"{hashlib.sha1(f'{path}:{st.st_mtime}'.encode()).hexdigest()}.mp4"
    if out.exists():
        return out
    with remux_lock(out):
        if out.exists():
            return out
        PLAY_CACHE.mkdir(parents=True, exist_ok=True)
        audio_only = path.suffix.lower() in AUDIO_EXT
        tmp = out.with_suffix(".part.mp4")
        cmd = ["nice", "-n", "10", "ffmpeg", "-y", "-v", "error", "-i", str(path)]
        cmd += ["-vn", "-map", "0:a:0"] if audio_only else ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
        acodec = next((x.get("codec_name") for x in ffprobe(path).get("streams", []) if x.get("codec_type") == "audio"), None)
        cmd += ["-c:a", "copy"] if acodec in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(out)
    # Keep the cache from growing forever: drop the oldest beyond 50 GB
    files = sorted(PLAY_CACHE.glob("*.mp4"), key=lambda p: p.stat().st_atime)
    while files and sum(f.stat().st_size for f in files) > 50 << 30:
        files.pop(0).unlink(missing_ok=True)
    return out


# The other modules, imported last: they import this one too, and are only used at run time
from . import web  # noqa: E402
