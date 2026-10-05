#!/usr/bin/env python3
"""拾光 worker for the Mac mini: claims tasks on the Pi's task board and does them on Apple silicon.

The board (and everything else: database, files) lives on the Pi. This claims what it can do, keeps the claim
alive with heartbeats while working, and hands the result back; the Pi writes it into the library. Pull, not push:
the Pi never needs to reach the Mac, and a Mac that's asleep or off simply stops claiming (its tasks go back on the
board and the Pi does them itself, slowly). It can also publish tasks (`task publish ...`); while the Pi can't be
reached those wait in a local outbox.

    mac_worker.py                       run (what the LaunchAgent does)
    mac_worker.py publish KIND TARGET [--force] [--priority N]
                                        e.g. publish transcribe job:446:0 --force   (do that video again)
    mac_worker.py board                 what's on the board

Speech-to-text: Whisper large-v3-turbo on the GPU (mlx-whisper), ~25x realtime on an M2 Pro. Pictures (covers,
keyframes): the Pi's Chinese-CLIP model (same vectors as the Pi's own) ~30 ms each, text in them with macOS Vision.
While a game is in front it takes no tasks (and gives back a long one it's in the middle of).

AI requests (summaries, chapters, translations, sorting...): two more claim loops answer them with this Mac's Claude
Code (`claude -p`, logged in with the Claude subscription), so they don't go to the paid DeepSeek API. They keep going
while a game is in front (it's only waiting on the network). When the subscription's limits are used up they say
they're paused until it resets, and the Pi asks DeepSeek meanwhile.
"""
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import requests

HERE = Path(__file__).resolve().parent
PI = os.environ.get("SHIGUANG_URL", "http://192.168.3.200:8088").rstrip("/")
TOKEN = (HERE / "token").read_text().strip()
WHISPER = os.environ.get("WHISPER_REPO", "mlx-community/whisper-large-v3-turbo")
FFMPEG = shutil.which("ffmpeg") or "/usr/local/Homebrew/bin/ffmpeg"  # launchd's PATH has no Homebrew
OUTBOX = HERE / "outbox.jsonl"
UNLOAD_AFTER = 600  # free the model's ~2 GB of memory after this long without work
NAME = "mac-" + socket.gethostname().split(".")[0]
CAPS = ["transcribe", "cover", "frames", "note_media", "gpu"]  # the task kinds this does, and that it has a GPU
CLIP = HERE / "models" / "clip" / "vision.onnx"  # the Pi's image model (same file: same vectors), copied over
CLIP_MODEL = "chinese-clip-vit-base-patch16-int8"  # its name on the Pi (search.CLIP_MODEL): sent with every vector
CLIP_MEAN, CLIP_STD = (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)
SAME_SHOT = 0.85  # a keyframe at least this alike to the last one kept is the same shot (talk shows: a handful)
GAME_APPS = HERE / "game-apps.txt"
CLAUDE = Path(os.environ.get("CLAUDE_BIN") or Path.home() / ".local" / "bin" / "claude")
CLAUDE_DIR = HERE / "claude-cwd"  # an empty folder to run it in: no project files for it to pick up
CLAUDE_CAPS = ["llm", "claude"]
CLAUDE_LOOPS = 2
DROP = Path.home() / "拾光投递"  # files put here become 随记 (photos, videos, sound; .txt/.md as text), e-books go on the 书架
DROPPED = DROP / "已投递"
CONFIG = HERE / "config.json"  # {"account": "yufan"}: whose 随记 / 书架 the drop folder fills
BOOK_EXT = {".epub", ".pdf", ".mobi", ".azw3", ".azw"}  # (.txt stays a 随记's text)
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".mp4", ".mov", ".m4v", ".m4a", ".mp3", ".wav", ".aac"}  # more apps (names or bundle ids, one a line) that count as playing
# Whisper's well-known inventions on silence and music (credits of the subtitle volunteers it was trained on)
JUNK = ("字幕由", "Amara.org", "请不吝点赞", "订阅 转发", "打赏支持", "明镜与点点", "Thank you for watching",
        "Thanks for watching", "Subtitles by", "ご視聴ありがとうございました")

http = requests.Session()
http.headers["X-Compute-Token"] = TOKEN
http.trust_env = False  # the Pi is on the LAN: never through a proxy from the environment


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def call(path, **body):
    r = http.post(PI + path, json={"worker": NAME, **body}, timeout=60)
    r.raise_for_status()
    return r.json()


# ---- publishing (with an outbox for when the Pi can't be reached)

def publish(kind, target, priority=20, force=False):
    task = {"kind": kind, "target": target, "priority": priority, "force": force}
    try:
        return call("/api/tasks/publish", **task)["id"]
    except requests.HTTPError as e:  # the Pi said no (unknown target, kind not allowed): don't keep it
        raise SystemExit(f"not published: {e.response.text.strip()}")
    except requests.RequestException:
        with open(OUTBOX, "a") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
        log("Pi unreachable: kept in the outbox", kind, target)
        return None


def flush_outbox():
    if not OUTBOX.exists():
        return
    left = []
    for line in OUTBOX.read_text().splitlines():
        try:
            call("/api/tasks/publish", **json.loads(line))
        except requests.HTTPError:
            log("dropped from the outbox (refused):", line)
        except requests.RequestException:
            left.append(line)
    if left:
        OUTBOX.write_text("\n".join(left) + "\n")
    else:
        OUTBOX.unlink()


# ---- the drop folder

_sizes = {}


def check_drop():
    """Send what's been put in ~/拾光投递 (once its size stopped changing) to the Pi as a 随记, then move it to
    已投递. One note per file; a .txt or .md next to a picture with the same name becomes its text. E-books go onto
    the account's 书架 instead."""
    if not CONFIG.exists():
        return
    account = json.loads(CONFIG.read_text()).get("account")
    DROP.mkdir(exist_ok=True)
    DROPPED.mkdir(exist_ok=True)
    for f in sorted(DROP.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        size = f.stat().st_size
        if _sizes.get(f) != size:  # still being copied in: look again next round
            _sizes[f] = size
            continue
        if f.suffix.lower() in BOOK_EXT:
            with open(f, "rb") as fh:
                r = http.post(PI + "/api/tasks/ingest-book", timeout=(10, 1800), data={"account": account},
                              files=[("file", (f.name, fh))])
            if r.status_code != 200:
                log("drop folder: book not sent", f.name, r.text[:200])
                continue
            f.rename(DROPPED / f.name)
            _sizes.pop(f, None)
            log("drop folder: book sent", f.name)
            continue
        text_file = next((f.with_suffix(x) for x in (".txt", ".md") if f.with_suffix(x).exists()), None)
        if f.suffix.lower() in (".txt", ".md"):
            if any(f.with_suffix(x).exists() for x in MEDIA_EXT):
                continue  # goes with the picture of the same name
            text, files = f.read_text(errors="ignore"), []
        elif f.suffix.lower() in MEDIA_EXT:
            text, files = (text_file.read_text(errors="ignore") if text_file else ""), [f]
        else:
            continue
        with open(files[0], "rb") if files else open(os.devnull, "rb") as fh:
            r = http.post(PI + "/api/tasks/ingest", timeout=(10, 1800),
                          data={"account": account, "text": text, "created": f.stat().st_mtime},
                          files=[("media", (files[0].name, fh))] if files else None)
        if r.status_code != 200:
            log("drop folder: not sent", f.name, r.text[:200])
            continue
        for done in [f] + ([text_file] if text_file and files else []):
            done.rename(DROPPED / done.name)
        _sizes.pop(f, None)
        log("drop folder: sent", f.name)


# ---- what this Mac can do

def decode(path):
    """Any audio file -> 16 kHz mono float32, what Whisper wants."""
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def transcribe(audio, lang=None):
    import mlx_whisper
    common = dict(path_or_hf_repo=WHISPER, temperature=0.0, condition_on_previous_text=False, verbose=None)
    # The language: the Pi's (from the video's metadata) when it knows, else a vote over five 30 s windows across
    # the video (the first 30 s alone is often music or a title card, and Whisper set to the wrong language
    # *translates*). Chinese gets a prompt that keeps it in simplified characters.
    if not lang:
        votes = []
        for at in (0.1, 0.3, 0.5, 0.7, 0.9):
            start = int(len(audio) * at)
            votes.append(mlx_whisper.transcribe(audio[start:start + 30 * 16000], **common).get("language"))
        lang = max(set(votes), key=votes.count)
    out = mlx_whisper.transcribe(audio, language=lang,
                                 initial_prompt="以下是普通话的句子，用简体中文。" if lang == "zh" else None, **common)
    segs = []
    for s in out["segments"]:
        text = s["text"].strip()
        if not text or any(j in text for j in JUNK):
            continue
        # silence or music that Whisper "heard" anyway, and stuck-record repetition
        if (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.8) or s.get("compression_ratio", 0) > 2.6:
            continue
        segs.append([round(s["start"], 2), round(s["end"], 2), text])
    if lang == "zh":
        from opencc import OpenCC
        cc = OpenCC("t2s")
        segs = [[a, b, cc.convert(t)] for a, b, t in segs]
    return lang, segs


def do_transcribe(task):
    with tempfile.NamedTemporaryFile(suffix=".mka") as f:
        with http.get(f"{PI}/api/tasks/{task['id']}/audio", params={"worker": NAME}, stream=True, timeout=(10, 300)) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        f.flush()
        audio = decode(f.name)
    started = time.time()
    lang, segs = transcribe(audio, task["payload"].get("language"))
    return {"language": lang, "segments": segs, "audio_seconds": round(len(audio) / 16000, 1),
            "seconds": round(time.time() - started, 1)}


_clip = None


def clip_vectors(images):
    """CLIP vectors (unit length) of PIL images; ~30 ms each on the M2 Pro's CPU (CoreML can't run the int8 model)."""
    global _clip
    import onnxruntime as ort
    from PIL import Image
    if _clip is None:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 6
        _clip = ort.InferenceSession(str(CLIP), so, providers=["CPUExecutionProvider"])
    px = np.stack([((np.asarray(im.convert("RGB").resize((224, 224), Image.BICUBIC), np.float32) / 255
                     - np.array(CLIP_MEAN, np.float32)) / np.array(CLIP_STD, np.float32)).transpose(2, 0, 1) for im in images])
    v = np.concatenate([_clip.run(None, {"pixel_values": px[i:i + 16]})[0] for i in range(0, len(px), 16)])
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def b64(v):
    import base64
    return base64.b64encode(np.asarray(v, np.float16).tobytes()).decode()


def read_text(path):
    """Text in a picture, with macOS's own recogniser (Vision; Chinese and English)."""
    import Vision
    from Foundation import NSURL
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    handler.performRequests_error_([req], None)
    lines = []
    for o in req.results() or []:
        c = o.topCandidates_(1)[0]
        if c.confidence() >= 0.5 and len(c.string().strip()) >= 2:
            lines.append(c.string().strip())
    return lines


def fetch(task, what, suffix):
    f = tempfile.NamedTemporaryFile(suffix=suffix)
    with http.get(f"{PI}/api/tasks/{task['id']}/{what}", params={"worker": NAME}, stream=True, timeout=(10, 600)) as r:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    f.flush()
    return f


def do_cover(task):
    from PIL import Image, ImageOps
    started = time.time()
    with fetch(task, "cover", ".jpg") as f:
        with Image.open(f.name) as im:
            v = clip_vectors([ImageOps.exif_transpose(im)])[0]
        lines = read_text(f.name)
    return {"vector": b64(v), "lines": lines, "seconds": round(time.time() - started, 2), "model": CLIP_MODEL}


def do_frames(task):
    """What's on screen, keyframe by keyframe (the Pi sends them small); a frame much like the last one kept is
    skipped, so a talk show leaves a handful and a travel vlog many."""
    import tarfile
    from PIL import Image
    started = time.time()
    with fetch(task, "keyframes", ".tar") as f, tarfile.open(f.name) as tar:
        members = sorted((float(m.name[:-4]), m) for m in tar.getmembers() if m.name.endswith(".jpg"))
        kept, prev = [], None
        for i in range(0, len(members), 64):
            if gaming():
                raise Busy("a game started")
            batch = members[i:i + 64]
            vecs = clip_vectors([Image.open(tar.extractfile(m)) for _, m in batch])
            for (t, _), v in zip(batch, vecs):
                if prev is None or float(v @ prev) < SAME_SHOT:
                    kept.append([round(t, 2), b64(v)])
                    prev = v
    return {"frames": kept, "keyframes": len(members), "seconds": round(time.time() - started, 1), "model": CLIP_MODEL}


def open_picture(path):
    """A PIL image of any photo, HEIC too (macOS's sips converts what Pillow can't read)."""
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        jpg = str(path) + ".jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", jpg], capture_output=True, check=True)
        with Image.open(jpg) as im:
            return ImageOps.exif_transpose(im).convert("RGB")


def do_note_media(task):
    """A 随记's new attachments: photos (what's in them, text on them), videos (what's said, the first frame),
    voice (what's said). The Pi adds the rest (poster file, where it was taken)."""
    from urllib.parse import quote
    items = {}
    for f in task["payload"].get("files", []):
        it = {}
        with fetch(task, f"note-file?file={quote(f['file'])}", Path(f["file"]).suffix) as tmp:
            if f["kind"] == "image":
                it["vector"], it["model"] = b64(clip_vectors([open_picture(tmp.name)])[0]), CLIP_MODEL
                it["ocr"] = "\n".join(read_text(tmp.name))
            else:
                audio = decode(tmp.name)
                it["duration"] = round(len(audio) / 16000, 1)
                if f["kind"] == "video":
                    frame = tmp.name + ".png"
                    subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(min(1.0, it["duration"] / 2)), "-i", tmp.name,
                                    "-frames:v", "1", frame], capture_output=True)
                    if Path(frame).exists():
                        it["vector"], it["model"] = b64(clip_vectors([open_picture(frame)])[0]), CLIP_MODEL
                        Path(frame).unlink()
                if len(audio) > 16000:
                    lang, segs = transcribe(audio)
                    it["transcript"] = "\n".join(t for _, _, t in segs)
        items[f["file"]] = it
    return {"items": items}


HANDLERS = {"transcribe": do_transcribe, "cover": do_cover, "frames": do_frames, "note_media": do_note_media}


class Busy(Exception):
    """The Mac is wanted for something else (a game): give the task back."""


def gaming():
    """Is a game in front? (macOS turns on Game Mode for those.) An app counts as a game if it says so
    (LSApplicationCategoryType ...games), comes from Steam or PlayCover, or is listed in game-apps.txt.
    Asked of `lsappinfo` each time: NSWorkspace in a process without an event loop keeps reporting the app that
    was in front when it first asked (the worker stayed "paused for a game" after the game had closed)."""
    try:
        import plistlib
        out = subprocess.run(["lsappinfo", "info", "-only", "bundlepath", "-only", "name", "-only", "bundleid",
                              subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()],
                             capture_output=True, text=True, timeout=5).stdout
        info = dict(re.findall(r'"(\w+)"="([^"]*)"', out))
        path, name, bundle_id = info.get("LSBundlePath", ""), info.get("LSDisplayName", ""), info.get("CFBundleIdentifier", "")
        category = ""
        plist = Path(path) / "Contents" / "Info.plist"
        if plist.exists():
            with open(plist, "rb") as f:
                category = str(plistlib.load(f).get("LSApplicationCategoryType", ""))
        listed = set(GAME_APPS.read_text().split()) if GAME_APPS.exists() else set()
        return "games" in category or "/steamapps/" in path or "PlayCover" in path or name in listed or bundle_id in listed
    except Exception:
        return False


def unload():
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        ModelHolder.model_path = None
        mx.clear_cache()
    except Exception:
        traceback.print_exc()


# ---- AI requests from the Pi, answered by Claude Code on this Mac

class ClaudeLimit(Exception):
    def __init__(self, text, until):
        super().__init__(text)
        self.until = until


def ask_claude(p, beat):
    """One request (system prompt, user text, the JSON keys wanted) through `claude -p`: no tools, no project
    context (--safe-mode), nothing kept; the answer validated against a schema with those keys."""
    schema = {"type": "object", "properties": {k: {} for k in p["fields"]}, "required": list(p["fields"])}
    cmd = [str(CLAUDE), "-p", "--safe-mode", "--tools", "", "--no-session-persistence", "--output-format", "json",
           "--model", p.get("model") or "sonnet", "--effort", p.get("effort") or "medium",
           "--system-prompt", p["system"], "--json-schema", json.dumps(schema)]
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    proxy = config.get("proxy", "")  # Anthropic isn't reachable directly from here
    env = {**os.environ, **({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy} if proxy else {})}
    CLAUDE_DIR.mkdir(exist_ok=True)
    started = time.time()
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          cwd=CLAUDE_DIR, env=env) as proc:
        stop = threading.Event()
        threading.Thread(target=lambda: [beat() for _ in iter(lambda: stop.wait(60), True)], daemon=True).start()
        try:
            stdout, stderr = proc.communicate(p["user"], timeout=600)
        finally:
            stop.set()
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude said (exit {proc.returncode}): {(stderr or stdout).strip()[-300:]}")
    if out.get("is_error") or out.get("subtype") != "success":
        text = str(out.get("result") or out.get("subtype") or stderr)
        if out.get("api_error_status") == 429 or re.search(r"limit", text, re.I):
            m = re.search(r"\|(\d{10})", text)  # "...limit reached|<when it resets>"
            raise ClaudeLimit(text[:200], int(m.group(1)) if m else time.time() + 1800)
        raise RuntimeError(f"claude: {text[:300]}")
    answer = out.get("structured_output")
    if not isinstance(answer, dict):
        answer = json.loads(re.sub(r"^```(json)?|```$", "", (out.get("result") or "").strip()))
    u = out.get("usage") or {}
    return {"out": answer, "model": next(iter(out.get("modelUsage") or {}), p.get("model")),
            "tokens_in": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
            "cache_read": u.get("cache_read_input_tokens") or 0, "tokens_out": u.get("output_tokens") or 0,
            "seconds": round(time.time() - started, 1)}


def claude_loop(n):
    name = f"{NAME}-claude-{n}"
    limited_until = 0
    while True:
        if not CLAUDE.exists():
            log("no Claude Code at", CLAUDE, ": AI requests stay with the Pi")
            return
        try:
            if time.time() < limited_until:
                call("/api/tasks/claim", worker=name, caps=CLAUDE_CAPS,
                     paused=f"Claude 额度用完，{time.strftime('%H:%M', time.localtime(limited_until))} 恢复")
                time.sleep(60)
                continue
            task = call("/api/tasks/claim", worker=name, caps=CLAUDE_CAPS, wait=25)["task"]
        except requests.RequestException:
            time.sleep(5)  # the Pi restarting (a deploy): back soon, and someone may be waiting on a search
            continue
        if not task:
            continue
        p, tid = task["payload"], task["id"]
        try:
            result = ask_claude(p, lambda: call(f"/api/tasks/{tid}/heartbeat", worker=name))
            call(f"/api/tasks/{tid}/done", worker=name, result=result)
            log("claude", tid, p.get("purpose"), result["model"], f"{result['seconds']}s",
                f"{result['tokens_in']} in / {result['tokens_out']} out")
        except ClaudeLimit as e:
            limited_until = e.until
            log("claude: limits used up until", time.strftime("%m-%d %H:%M", time.localtime(e.until)), e)
            try:
                call(f"/api/tasks/{tid}/fail", worker=name, error=f"limit: {e}", retry=False)
            except requests.RequestException:
                pass
        except Exception as e:  # the Pi asks DeepSeek instead
            traceback.print_exc()
            try:
                call(f"/api/tasks/{tid}/fail", worker=name, error=str(e)[:500], retry=False)
            except requests.RequestException:
                pass


# ---- the claim loop

def work(task):
    """Do one claimed task, keeping the claim alive meanwhile; report done or failed."""
    stop = threading.Event()

    def beat():
        while not stop.wait(60):
            try:
                if not call(f"/api/tasks/{task['id']}/heartbeat")["ok"]:
                    log("lost the claim on", task["id"])
            except requests.RequestException:
                pass
    threading.Thread(target=beat, daemon=True).start()
    p = task["payload"]
    log("start", task["id"], task["kind"], task["target"], p.get("title", ""), f"{(p.get('duration') or 0) / 60:.0f} min")
    started = time.time()
    try:
        result = HANDLERS[task["kind"]](task)
    except Busy as e:
        stop.set()
        log("gave back", task["id"], e)
        call(f"/api/tasks/{task['id']}/release")
        return
    except requests.RequestException as e:  # the network, not the task: back on the board soon
        stop.set()
        traceback.print_exc()
        call(f"/api/tasks/{task['id']}/fail", error=f"network: {e}", retry=True)
        return
    except Exception as e:  # the task itself (unreadable audio...): failed for good
        stop.set()
        traceback.print_exc()
        call(f"/api/tasks/{task['id']}/fail", error=str(e), retry=False)
        return
    stop.set()
    call(f"/api/tasks/{task['id']}/done", result=result)
    extra = (f"{result['language']}, {len(result['segments'])} lines" if task["kind"] == "transcribe" else
             f"{len(result['frames'])} of {result['keyframes']} frames kept" if task["kind"] == "frames" else
             f"{len(result['lines'])} lines of text" if task["kind"] == "cover" else "")
    log("done", task["id"], task["kind"], f"{time.time() - started:.0f}s", extra)


def drop_loop():
    """Its own thread: a dropped file goes out within ~20 s even while a long task runs."""
    while True:
        try:
            if not gaming():
                check_drop()
        except Exception:
            traceback.print_exc()
        time.sleep(10)


def run():
    log("worker", NAME, CAPS, "->", PI, "model", WHISPER)
    threading.Thread(target=drop_loop, daemon=True).start()
    for n in range(1, CLAUDE_LOOPS + 1):
        threading.Thread(target=claude_loop, args=(n,), daemon=True).start()
    last_work, loaded, paused_for_game = time.time(), False, False
    while True:
        if gaming():  # playing: leave the Mac alone; tell the Pi we're only paused, so it doesn't start on our tasks
            if not paused_for_game:
                log("game in front: not taking tasks")
                unload()
                paused_for_game, loaded = True, False
            try:
                call("/api/tasks/claim", caps=CAPS, paused="游戏中")
            except requests.RequestException:
                pass
            time.sleep(30)
            continue
        if paused_for_game:
            log("game closed: taking tasks again")
            paused_for_game = False
        try:
            flush_outbox()
            # waits on the Pi up to 25 s for a task to come in, so new work starts right away
            task = call("/api/tasks/claim", caps=CAPS, wait=25)["task"]
        except requests.RequestException as e:
            log("Pi unreachable:", e)
            time.sleep(60)
            continue
        if not task:
            if loaded and time.time() - last_work > UNLOAD_AFTER:
                unload()
                loaded = False
                log("model unloaded")
            continue
        try:
            work(task)
            loaded = True
        except Exception:
            traceback.print_exc()
            time.sleep(30)
        last_work = time.time()


def main():
    args = sys.argv[1:]
    if not args:
        return run()
    if args[0] == "publish" and len(args) >= 3:
        prio = int(args[args.index("--priority") + 1]) if "--priority" in args else 20
        tid = publish(args[1], args[2], prio, force="--force" in args)
        print(f"published #{tid}" if tid else "kept in the outbox; sent when the Pi is back")
    elif args[0] == "board":
        b = http.get(PI + "/api/tasks", timeout=30).json()
        for w in b["workers"]:
            print(f"{w['name']:24} {'online ' if w['online'] else 'offline'} {','.join(w['caps'])}")
        for k, v in b["kinds"].items():
            if v["queued"] + v["running"] + v["done"] + v["failed"]:
                print(f"{v['label']:8} queued {v['queued']:4}  running {v['running']}  done {v['done']:4}  failed {v['failed']}"
                      + (f"  {v['hours']} h left" if v["hours"] else ""))
        for t in b["running"]:
            print(f"running #{t['id']} {t['label']} by {t['worker']}: {t['title']}" + (f" {t['pct']}%" if t["pct"] is not None else ""))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
