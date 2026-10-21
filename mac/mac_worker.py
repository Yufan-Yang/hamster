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

When nobody has touched the Mac for a while (IDLE_AFTER) a second loop takes the picture tasks (covers, keyframes:
CLIP on the CPU) while the first keeps Whisper busy on the GPU; one more would only make the Pi cut keyframes for two
videos at once. Back to one loop as soon as someone uses the Mac (the picture task in hand is finished first).

AI requests use Codex (the ChatGPT subscription) first; the Pi falls back to DeepSeek if Codex is
unavailable, fails or runs out of quota. Two loops keep going during gaming; two extra loops join
when LLM_BACKLOG requests are waiting. Whisper, CLIP and Vision keep their existing local paths.
"""
import base64
import json
import plistlib
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
CAPS = ["transcribe", "live_transcribe", "live_render", "cover", "frames", "note_media", "gpu"]
PIC_CAPS = ["cover", "frames", "gpu"]  # the second loop's, while the Mac is idle (CPU: runs beside Whisper)
IDLE_AFTER = 600  # seconds without keyboard / mouse before the Mac counts as idle
CLIP = HERE / "models" / "clip" / "vision.onnx"  # the Pi's image model (same file: same vectors), copied over
CLIP_MODEL = "chinese-clip-vit-base-patch16-int8"  # its name on the Pi (search.CLIP_MODEL): sent with every vector
CLIP_MEAN, CLIP_STD = (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)
SAME_SHOT = 0.85  # a keyframe at least this alike to the last one kept is the same shot (talk shows: a handful)
GAME_APPS = HERE / "game-apps.txt"
AI_DIR = HERE / "codex-cwd"  # an empty folder to run it in: no project files for it to pick up
AI_CAPS = ["llm", "codex", "shortcut"]  # (and signing iOS shortcuts: quick, and keeps going while gaming)
AI_LOOPS = 2
AI_EXTRA = 2  # more loops, only while LLM_BACKLOG or more AI requests are waiting
LLM_BACKLOG = 6
CODEX = Path(os.environ.get("CODEX_BIN") or shutil.which("codex") or "/usr/local/Homebrew/bin/codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-6.1-sol")  # empty: the account's default model
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


me = threading.local()  # which of this Mac's workers the current thread is (each loop claims under its own name)


def whoami():
    return getattr(me, "name", NAME)


def call(path, **body):
    r = http.post(PI + path, json={"worker": whoami(), **body}, timeout=60)
    r.raise_for_status()
    return r.json()


def task_heartbeat(task, **body):
    result = call(f"/api/tasks/{task['id']}/heartbeat", claim_token=task.get("claim_token"), **body)
    if not result.get("ok"):
        raise RuntimeError("task lease was lost")
    return result


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
        with http.get(f"{PI}/api/tasks/{task['id']}/audio", params={"worker": whoami()}, stream=True, timeout=(10, 300)) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        f.flush()
        audio = decode(f.name)
    started = time.time()
    lang, segs = transcribe(audio, task["payload"].get("language"))
    return {"language": lang, "segments": segs, "audio_seconds": round(len(audio) / 16000, 1),
            "seconds": round(time.time() - started, 1)}


_clip, _clip_lock = None, threading.Lock()


def clip_vectors(images):
    """CLIP vectors (unit length) of PIL images; ~30 ms each on the M2 Pro's CPU (CoreML can't run the int8 model)."""
    global _clip
    import onnxruntime as ort
    from PIL import Image
    with _clip_lock:  # (two loops may want it first at once; running it is thread-safe)
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
    with http.get(f"{PI}/api/tasks/{task['id']}/{what}", params={"worker": whoami()}, stream=True, timeout=(10, 600)) as r:
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


def do_live_render(task):
    p = task["payload"]
    with fetch(task, "live-source", ".mkv") as original, tempfile.NamedTemporaryFile(suffix=".mp4") as rendered:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", str(p["seek"]), "-i", original.name,
                        "-t", str(p["duration"]), "-map", "0:v:0", "-map", "0:a:0?",
                        "-vf", "scale='min(1280,iw)':-2", "-c:v", "h264_videotoolbox", "-b:v", "3500k",
                        "-c:a", "aac", "-movflags", "+faststart", rendered.name], check=True, timeout=1800)
        with open(rendered.name, "rb") as video:
            response = http.post(f"{PI}/api/tasks/{task['id']}/live-result", params={"worker": whoami(), "claim_token": task.get("claim_token")},
                                 files={"video": ("clip.mp4", video, "video/mp4")}, timeout=(10, 600))
            response.raise_for_status()
    return {"rendered": True}


HANDLERS = {"transcribe": do_transcribe, "live_transcribe": do_transcribe, "live_render": do_live_render,
            "cover": do_cover, "frames": do_frames, "note_media": do_note_media}


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


def idle():
    """Nobody at the Mac: no keyboard / mouse for IDLE_AFTER, and no game in front."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
        ns = int(re.search(r'"HIDIdleTime" = (\d+)', out).group(1))
    except Exception:
        return False
    return ns / 1e9 >= IDLE_AFTER and not gaming()


_backlog = [0, 0.0]  # AI requests waiting, when last asked


def llm_backlog():
    if time.time() - _backlog[1] > 30:
        try:
            _backlog[0] = http.get(PI + "/api/tasks", timeout=30).json()["kinds"].get("llm", {}).get("queued", 0)
        except (requests.RequestException, ValueError, KeyError):
            _backlog[0] = 0
        _backlog[1] = time.time()
    return _backlog[0]


def unload():
    try:
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        ModelHolder.model = None
        ModelHolder.model_path = None
        mx.clear_cache()
    except Exception:
        traceback.print_exc()


# ---- AI requests from the Pi, answered by Codex on this Mac

class UsageLimit(Exception):
    def __init__(self, text, until):
        super().__init__(text)
        self.until = until


def proxy_env():
    """Use the router unless config.json explicitly selects an application proxy."""
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    proxy = (config.get("proxy") or "").strip()
    if proxy:
        env.update({k: proxy for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")})
    return env


def run_cli(cmd, text, beat, timeout=600, cwd=None):
    """Run an AI command line in the empty folder with `text` on stdin, heartbeats to the Pi every minute meanwhile.
    Killed when it takes longer than `timeout` (the Pi stops waiting at some point anyway)."""
    beat()
    AI_DIR.mkdir(exist_ok=True)
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          cwd=cwd or AI_DIR, env=proxy_env()) as proc:
        stop = threading.Event()
        lost = []
        def keep():
            while not stop.wait(60):
                try:
                    beat()
                except Exception as exc:
                    lost.append(exc)
                    proc.kill()
                    return
        keeper = threading.Thread(target=keep, daemon=True)
        keeper.start()
        try:
            stdout, stderr = proc.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(f"{Path(cmd[0]).name}: no answer in {timeout}s")
        finally:
            stop.set()
        if lost:
            raise RuntimeError("task heartbeat failed; discarded the unfinished model result") from lost[0]
        beat()
    return proc.returncode, stdout, stderr


def ask_codex(p, beat, *, cwd=None, timeout=300, model=None):
    """A request through `codex exec` (the ChatGPT subscription): read-only sandbox in the empty folder, no
    user config or rules, nothing kept. The system prompt goes in as developer instructions. No --output-schema: its
    strict mode wants a type for every key, and the prompts already ask for exactly these keys as JSON."""
    model = CODEX_MODEL if model is None else model
    cmd = [str(CODEX), "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-s", "read-only", "--json", "--color", "never",
           "-c", f"model_reasoning_effort={json.dumps(p.get('effort') or 'medium')}",
           "-c", f"developer_instructions={json.dumps(p['system'], ensure_ascii=False)}",
           *(["-m", model] if model else []), "-"]
    started = time.time()
    code, stdout, stderr = run_cli(cmd, p["user"], beat, timeout=timeout, cwd=cwd)
    text, usage, error = None, {}, None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = ev["item"].get("text")
        elif ev.get("type") == "turn.completed":
            usage = ev.get("usage") or {}
        elif ev.get("type") in ("error", "turn.failed"):
            error = str(ev.get("message") or (ev.get("error") or {}).get("message") or ev)
    if code != 0 or error or text is None:
        error = error or (stderr or stdout).strip()[-300:] or f"exit {code}"
        if re.search(r"usage limit|rate limit|429|quota", error, re.I):
            raise UsageLimit(f"codex: {error[:200]}", time.time() + 1800)
        raise RuntimeError(f"codex: {error[:300]}")
    answer = json.loads(re.sub(r"^```(json)?|```$", "", text.strip()))
    if not isinstance(answer, dict):
        raise RuntimeError(f"codex: not a JSON object: {text[:200]}")
    return {"out": answer, "model": model or "codex", "engine": "codex",
            "tokens_in": usage.get("input_tokens") or 0, "cache_read": usage.get("cached_input_tokens") or 0,
            "tokens_out": (usage.get("output_tokens") or 0), "seconds": round(time.time() - started, 1)}


def answer_llm(p, beat, limits):
    """Ask Codex unless its quota is exhausted; failures go back to the Pi for DeepSeek."""
    errors = []
    for name, ask, there in (("codex", ask_codex, CODEX.exists()),):
        if not there or time.time() < limits.get(name, 0):
            continue
        try:
            return ask(p, beat)
        except UsageLimit as e:
            limits[name] = e.until
            log(f"{name}: limits used up until", time.strftime("%m-%d %H:%M", time.localtime(e.until)), e)
            errors.append(f"{name} limit: {e}")
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errors) or "no Codex to ask")


def text_value(text, attachments=None):
    v = {"string": text}
    if attachments:
        v["attachmentsByRange"] = attachments
    return {"Value": v, "WFSerializationType": "WFTextTokenString"}


def sign_shortcut(p):
    """An account's iOS shortcut for outside: share a link -> POST {text, device, key} to the public address, show the
    answer. Built here with the key in it (nothing to fill in when installing) and signed with `shortcuts sign`."""
    device, request = "2C7B4D3E-5F60-4B2C-8D9E-8F7A6B5C4D32", "6E1C2B1A-0C55-4C7B-9A57-2E7B2E6B9A01"
    field = lambda k, v: {"WFItemType": 0, "WFKey": text_value(k), "WFValue": v}  # noqa: E731
    actions = [
        {"WFWorkflowActionIdentifier": "is.workflow.actions.getdevicedetails",
         "WFWorkflowActionParameters": {"UUID": device, "WFDeviceDetail": "Device Name"}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
         "WFWorkflowActionParameters": {
             "UUID": request, "ShowHeaders": False, "WFHTTPMethod": "POST", "WFHTTPBodyType": "JSON", "WFURL": p["url"],
             "WFJSONValues": {"WFSerializationType": "WFDictionaryFieldValue", "Value": {"WFDictionaryFieldValueItems": [
                 field("text", text_value("\ufffc", {"{0, 1}": {"Type": "ExtensionInput"}})),
                 field("device", text_value("\ufffc", {"{0, 1}": {"OutputName": "Device Details", "OutputUUID": device,
                                                                  "Type": "ActionOutput"}})),
                 field("key", text_value(p["key"]))]}}}},
        {"WFWorkflowActionIdentifier": "is.workflow.actions.notification",
         "WFWorkflowActionParameters": {"WFNotificationActionTitle": "拾光", "WFNotificationActionSound": False,
                                        "WFNotificationActionBody": text_value("已发送：\ufffc", {"{4, 1}": {
                                            "OutputName": "Contents of URL", "OutputUUID": request, "Type": "ActionOutput"}})}},
    ]
    shortcut = {
        "WFWorkflowActions": actions, "WFWorkflowClientVersion": "2607.0.2", "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 61440, "WFWorkflowIconStartColor": 4282601983},
        "WFWorkflowImportQuestions": [], "WFQuickActionSurfaces": [], "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFStringContentItem", "WFSafariWebPageContentItem",
                                              "WFRichTextContentItem", "WFArticleContentItem"],
        "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowOutputContentItemClasses": []}
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.shortcut", Path(tmp) / "out.shortcut"
        src.write_bytes(plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY))
        for attempt in range(4):  # Apple's signing server fails now and then
            r = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", src, "--output", out],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                return {"data": base64.b64encode(out.read_bytes()).decode()}
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"shortcuts sign: {(r.stderr or r.stdout).strip()[-300:]}")


def codex_loop(n, extra=False):
    name = me.name = f"{NAME}-codex-{n}"
    limits = {}  # "codex" -> until when its limits are used up
    while True:
        if extra and llm_backlog() < LLM_BACKLOG:
            time.sleep(30)
            continue
        if not CODEX.exists():
            log("no Codex at", CODEX, ": AI requests stay with the Pi")
            return
        try:
            usable = [k for k, there in (("codex", CODEX.exists()),)
                      if there and time.time() >= limits.get(k, 0)]
            if not usable:
                until = min(limits.values())
                call("/api/tasks/claim", worker=name, caps=AI_CAPS,
                     paused=f"Codex 额度用完，{time.strftime('%H:%M', time.localtime(until))} 恢复")
                time.sleep(60)
                continue
            task = call("/api/tasks/claim", worker=name, caps=AI_CAPS, wait=25)["task"]
        except requests.RequestException:
            time.sleep(5)  # the Pi restarting (a deploy): back soon, and someone may be waiting on a search
            continue
        if not task:
            continue
        p, tid = task["payload"], task["id"]
        if task["kind"] == "shortcut":
            try:
                call(f"/api/tasks/{tid}/done", worker=name, claim_token=task.get("claim_token"), result=sign_shortcut(p))
                log("signed shortcut", tid, p.get("title"))
            except Exception as e:
                traceback.print_exc()
                call(f"/api/tasks/{tid}/fail", worker=name, claim_token=task.get("claim_token"), error=str(e)[:500], retry=False)
            continue
        try:
            result = answer_llm(p, lambda: task_heartbeat(task, worker=name), limits)
            call(f"/api/tasks/{tid}/done", worker=name, claim_token=task.get("claim_token"), result=result)
            log(result["engine"], tid, p.get("purpose"), result["model"], f"{result['seconds']}s",
                f"{result['tokens_in']} in / {result['tokens_out']} out")
        except Exception as e:  # the Pi asks DeepSeek instead
            log("AI request", tid, p.get("purpose"), "-> DeepSeek:", str(e)[:300])
            try:
                call(f"/api/tasks/{tid}/fail", worker=name, claim_token=task.get("claim_token"), error=str(e)[:500], retry=False)
            except requests.RequestException:
                pass


# ---- 自修复: an error the Pi ran into (shiguang/heal.py), worked out and fixed with this Mac's Codex

REPO = Path(os.environ.get("SHIGUANG_REPO") or Path.home() / "vpn" / "grabber")  # the code, deploy.sh next to it
HEAL_DIR = HERE / "heal-worktree"  # a git worktree of it: the user's own checkout is never touched
HEAL_MODEL = os.environ.get("HEAL_MODEL", CODEX_MODEL)
HEAL_ROUNDS = 2  # a fix the smoke test rejects goes back to Codex once, with what failed
HEAL_PATHS = ("shiguang/", "grabber.py", "index.html", "static/", "tests/")
PI_SSH = os.environ.get("PI_SSH", "pi@192.168.3.200")
HEAL_SYSTEM = """You fix bugs in 拾光, a home media server on a Raspberry Pi. The code is in this folder: the shiguang/
package, grabber.py (entry), index.html (the page), tests/smoke.py (run before every deploy, on a copy of the real
database). An error happened in production; its details are below.

First read HEALING.md: causes seen before, and how to tell them apart. Below the error comes "Evidence from the Pi":
real output gathered just now (logs, open database files, disks, memory). Work out the cause from the code and that
evidence (you can read and search; you can't run anything). Be exact about what you know: quote the line of
evidence or code each claim rests on, and never name a cause (a failing disk, a power cut, the network, a site) that
nothing in the evidence or the code points to. When the evidence can't settle it, say which causes remain and what a
person should check to tell them apart, and give low confidence. If it's a bug in this code, fix it
with the smallest change that makes it right, in the style of the code around it (comments, naming, idiom). Don't
touch deploy.sh, mac/ or config; don't weaken or delete tests to make them pass; don't add features or refactor.
If it isn't a bug in the code (a site down, a broken file, the network, the disk), change nothing.

Your final answer: state "fixed" (you changed code), "diagnosed" (a bug you couldn't fix safely, or it needs a person),
or "not_a_bug"; diagnosis: 2-5 sentences in Simplified Chinese: the cause, and what you changed or what a person
should do; summary: one short line in Simplified Chinese; retry: true when the failed tasks should run again now
(after your fix, or when the cause has passed); confidence: high (the evidence or code shows it), medium (fits
everything seen, but not shown), low (a guess between causes); evidence: the few lines (log, code with file:line)
the diagnosis rests on."""
HEAL_SCHEMA = {"type": "object", "required": ["state", "diagnosis", "summary", "retry", "confidence", "evidence"], "properties": {
    "state": {"enum": ["fixed", "diagnosed", "not_a_bug"]}, "diagnosis": {"type": "string"},
    "summary": {"type": "string"}, "retry": {"type": "boolean"},
    "confidence": {"enum": ["high", "medium", "low"]}, "evidence": {"type": "string"}}}
CONFIDENCE = {"high": "高", "medium": "中", "low": "低"}
# Read-only looks at the Pi for the healer (it can't run anything itself): the services' logs around the error, which
# database files they have open (a deleted WAL shows here), who else has the database open, kernel errors, disks.
PI_EVIDENCE = r"""
echo "## deployed: $(cat /opt/grabber/REVISION 2>/dev/null)   up: $(uptime)"
echo "## services"; systemctl is-active grabber grabber-worker
echo "## web + worker log (no page requests), last 60 lines"
journalctl -u grabber -u grabber-worker -n 600 --no-pager -o short-iso 2>/dev/null | grep -v -E '"(GET|POST|HEAD) ' | tail -60
echo "## database files the services have open"
for s in grabber grabber-worker; do p=$(systemctl show $s -p MainPID --value); echo "$s ($p):"; sudo -n ls -l /proc/$p/fd 2>/dev/null | grep -o 'grabber\.db.*'; done
echo "## every process with the database open"; sudo -n fuser -v /mnt/disk1/.grabber/grabber.db* 2>&1 | tail -n +2
echo "## kernel errors, last day"; journalctl -k -p err --since -1d --no-pager 2>/dev/null | tail -15
echo "## disks"; cat /run/disk-health.json 2>/dev/null; echo; df -h / /mnt/disk1 /mnt/disk2 2>/dev/null
echo "## memory"; free -m
"""


def git(*args, cwd=REPO, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def deployed_revision():
    """The commit the Pi runs (deploy.sh writes it), "…-dirty" when it was deployed with uncommitted changes."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PI_SSH, "cat /opt/grabber/REVISION"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def pi_evidence():
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PI_SSH, PI_EVIDENCE],
                           capture_output=True, text=True, timeout=60)
        return (r.stdout + r.stderr)[-9000:].strip() or "(nothing came back)"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(couldn't reach the Pi: {e})"


def heal_prompt(p, failed="", evidence=""):
    ctx = json.dumps(p.get("context") or {}, ensure_ascii=False, indent=1)
    return (f"Where: {p['label']} ({p['kind']}){' — ' + p['about'] if p.get('about') else ''}\n"
            f"Happened {p.get('count', 1)} time(s).\nError: {p['error']}\n\nTraceback:\n{p.get('trace') or '(none: it failed on a remote worker)'}\n\n"
            f"Context:\n{ctx}\n"
            + (f"\nEvidence from the Pi (gathered now, read-only):\n{evidence}\n" if evidence else "")
            + (f"\nA fix was made for this before ({p['before']}), and it happened again after it.\n" if p.get("before") else "")
            + (f"\nYour fix so far is in the working tree, but the deploy rejected it:\n{failed}\nFix that.\n" if failed else ""))


def ask_healer(p, beat, can_edit, failed="", evidence=""):
    """Codex inspects the worktree read-only; the host validates and applies its proposed patch."""
    system = HEAL_SYSTEM + """
Return one JSON object with state, diagnosis, summary, retry, confidence, evidence and patch.
Do not edit files or run commands that change files or system state. Read/search the repository as needed.
patch: a unified diff suitable for git apply, or an empty string when no code change is needed.
Only propose changes within shiguang/, grabber.py, index.html, static/ and tests/.
Existing tests are immutable. Add a standalone stdlib unittest regression in
tests/heal_regression_<name>.py that reproduces this incident: it must fail with
an assertion on the original code and pass on your patch. Do not use network,
production data, credentials or external dependencies in this regression.
Only return fixed with high confidence and concrete evidence. An uncertain
diagnosis must not propose a patch or request retry.
""" + ("This invocation is diagnosis only: patch MUST be empty." if not can_edit else "")
    answer = ask_codex({"system": system, "user": heal_prompt(p, failed, evidence), "effort": "medium"},
                       beat, cwd=HEAL_DIR, timeout=1800, model=HEAL_MODEL)["out"]
    for key in HEAL_SCHEMA["required"]:
        if key not in answer:
            raise RuntimeError(f"codex healer: missing {key}")
    if answer["state"] not in ("fixed", "diagnosed", "not_a_bug") or not isinstance(answer["retry"], bool):
        raise RuntimeError("codex healer: invalid state or retry")
    if answer["confidence"] not in CONFIDENCE or any(
            not isinstance(answer[key], str) or not answer[key].strip()
            for key in ("diagnosis", "summary", "evidence")):
        raise RuntimeError("codex healer: missing diagnosis evidence or confidence")
    patch = answer.get("patch") or ""
    if not isinstance(patch, str):
        raise RuntimeError("codex healer: patch must be a string")
    if can_edit and patch.strip():
        if answer["state"] != "fixed" or answer["confidence"] != "high":
            raise RuntimeError("codex healer: automatic patches need high-confidence evidence")
        apply_heal_patch(patch)
    return answer


def apply_heal_patch(patch):
    """Validate every destination before applying a diff; never execute model-supplied code."""
    args = ["git", "apply", "--numstat", "-z", "-"]
    r = subprocess.run(args, input=patch, cwd=HEAL_DIR, capture_output=True, text=True)
    if r.returncode or not r.stdout:
        raise RuntimeError(f"codex healer: invalid patch: {r.stderr[:300]}")
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        added, removed, path = entry.split("\t", 2)
        allowed = any(path == p or p.endswith("/") and path.startswith(p) for p in HEAL_PATHS)
        if (added == "-" or removed == "-" or not allowed
                or Path(path).is_absolute() or ".." in Path(path).parts):
            raise RuntimeError(f"codex healer: patch outside allowed text files: {path}")
        if path.startswith("tests/") and (HEAL_DIR / path).exists():
            raise RuntimeError("codex healer: existing regression tests cannot be changed")
    if re.search(r"(?:new|old|deleted) file mode (?!100644|100755)\d+", patch):
        raise RuntimeError("codex healer: only regular source files are allowed")
    for flags in (["--check"], []):
        r = subprocess.run(["git", "apply", *flags, "-"], input=patch, cwd=HEAL_DIR, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"codex healer: patch rejected: {r.stderr[:300]}")


def heal_regressions(base, beat):
    """Require an actual assertion failure on the baseline and a passing fix."""
    names = [n for n in git("ls-files", "--others", "--exclude-standard", "tests/", cwd=HEAL_DIR).splitlines()
             if re.fullmatch(r"tests/heal_regression_[a-zA-Z0-9_]+\.py", n)]
    # The previous round may already have added the regression to the index.
    names += [n for n in git("diff", "--name-only", "--diff-filter=A", base, cwd=HEAL_DIR).splitlines()
              if re.fullmatch(r"tests/heal_regression_[a-zA-Z0-9_]+\.py", n)]
    names = sorted(set(names))
    if not names:
        raise RuntimeError("修复缺少可重现原问题的 heal_regression_ 回归测试")
    with tempfile.TemporaryDirectory(prefix="shiguang-heal-check-") as tmp:
        baseline = Path(tmp) / "baseline"
        baseline.mkdir()
        archived = subprocess.run(["git", "archive", base], cwd=REPO, capture_output=True, check=True)
        subprocess.run(["tar", "xf", "-", "-C", str(baseline)], input=archived.stdout, check=True)
        for name in names:
            dest = baseline / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HEAL_DIR / name, dest)
            for folder, expect_pass in ((baseline, False), (HEAL_DIR, True)):
                beat()
                data = Path(tmp) / ("candidate-data" if expect_pass else "baseline-data")
                data.mkdir(exist_ok=True)
                env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "SYSTEMROOT")}
                env.update(HOME=str(data), DB_PATH=str(data / "test.db"),
                           STATE_DIR=str(data / "state"), MEDIA_ROOT=str(data / "media"),
                           PYTHONPATH=str(folder), LLM_API_KEY="", COMPUTE_TOKEN="test-only")
                check = subprocess.run([sys.executable, name], cwd=folder, env=env,
                                       capture_output=True, text=True, timeout=90)
                output = check.stdout + check.stderr
                ran = re.search(r"Ran ([1-9]\d*) tests? in ", output)
                valid = check.returncode == 0 if expect_pass else (
                    check.returncode != 0 and "FAILED (failures=" in output and "errors=" not in output)
                if not ran or not valid:
                    label = "修复版本应通过" if expect_pass else "原版本应复现断言失败"
                    raise RuntimeError(f"{name}: {label}\n{output[-2500:]}")
    return names


def verify_heal_release(revision, started):
    """The model's claim of success is insufficient: read the deployment receipt."""
    command = "cat /opt/grabber/VERIFICATION.json"
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PI_SSH, command],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("无法读取上线验收记录")
    receipt = json.loads(result.stdout)
    required = {"source_hashes", "page", "task_api", "fresh_pi_cpu"}
    if (receipt.get("revision") != revision or receipt.get("verified_at", 0) < started
            or not required.issubset(receipt.get("checks", [])) or deployed_revision() != revision):
        raise RuntimeError("上线版本或验收记录与本次修复不一致")
    return receipt


def heal(task, beat):
    """Diagnose an incident and, when the Pi runs the committed code, fix it: Codex proposes a patch for a worktree, deploy.sh tests
    the fix on a copy of the database and puts it live (or leaves the Pi as it was); the fix is then committed onto
    the checkout's branch (or a heal/N branch when that has moved on)."""
    p = task["payload"]
    deployed = deployed_revision()
    known = bool(re.fullmatch(r"[a-f0-9]{40}", deployed)) and subprocess.run(
        ["git", "cat-file", "-e", f"{deployed}^{{commit}}"], cwd=REPO,
        capture_output=True).returncode == 0
    base = deployed if known else git("rev-parse", "HEAD")
    can_edit = known
    if HEAL_DIR.exists():
        git("worktree", "remove", "--force", str(HEAL_DIR), check=False)
        shutil.rmtree(HEAL_DIR, ignore_errors=True)
    git("worktree", "prune")
    git("worktree", "add", "--detach", str(HEAL_DIR), base)
    evidence = pi_evidence()
    try:
        failed, rounds = "", 0
        while True:
            rounds += 1
            try:
                answer = ask_healer(p, beat, can_edit, failed, evidence)
            except RuntimeError as exc:
                if rounds >= HEAL_ROUNDS:
                    return {"state": "diagnosed", "summary": "诊断或补丁未通过校验", "retry": False,
                            "diagnosis": str(exc)[:3000], "rounds": rounds,
                            "verification": {"status": "rejected", "base": base}}
                failed = str(exc)
                continue
            for line in git("status", "--porcelain", cwd=HEAL_DIR).splitlines():  # outside what a fix may touch: undone
                path = line[3:].split(" -> ")[-1]
                if not path.startswith(HEAL_PATHS):
                    git("checkout", "--", path, cwd=HEAL_DIR, check=False) if line[:2] != "??" else (HEAL_DIR / path).unlink(missing_ok=True)
            out = {"state": answer["state"], "summary": answer["summary"], "retry": bool(answer["retry"]), "rounds": rounds,
                   "diagnosis": f"{answer['diagnosis']}（把握：{CONFIDENCE.get(answer.get('confidence'), '低')}）",
                   "evidence": str(answer.get("evidence") or "")[:2000]}
            if not can_edit:
                out["diagnosis"] += "（找不到线上版本对应的源码快照，本次仅诊断。）"
                return out | {"state": "diagnosed" if answer["state"] == "fixed" else answer["state"], "retry": False}
            if not git("status", "--porcelain", cwd=HEAL_DIR):
                return out | {"state": "diagnosed" if answer["state"] == "fixed" else answer["state"], "retry": False}
            if answer["state"] != "fixed" or answer["confidence"] != "high":
                return out | {"state": "diagnosed", "retry": False,
                              "diagnosis": out["diagnosis"] + "（当前诊断不足以发布补丁，保留诊断结果。）",
                              "verification": {"status": "rejected", "base": base}}
            try:
                regressions = heal_regressions(base, beat)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                if rounds >= HEAL_ROUNDS:
                    return out | {"state": "diagnosed", "retry": False,
                                  "diagnosis": out["diagnosis"] + f"（回归门槛未通过：{str(exc)[:2000]}）",
                                  "verification": {"status": "rejected", "base": base}}
                failed = str(exc)
                continue
            beat()
            if deployed_revision() != base:
                return out | {"state": "diagnosed", "retry": False,
                              "diagnosis": out["diagnosis"] + "（线上版本已变化，停止发布过期补丁。）",
                              "verification": {"status": "rejected", "base": base}}
            git("add", "-A", "--", *HEAL_PATHS, cwd=HEAL_DIR)
            git("commit", "-q", "-m", f"自修复 #{p['incident']}: {answer['summary']}\n\n{answer['diagnosis']}\n\n"
                "Co-Authored-By: Codex <noreply@openai.com>", cwd=HEAL_DIR)
            env = dict(os.environ, PYFLAKES=os.environ.get("PYFLAKES") or str(Path(sys.executable).parent / "pyflakes"),
                       SHIGUANG_EXPECTED_REVISION=base)
            stop = threading.Event()
            threading.Thread(target=lambda: [beat() for _ in iter(lambda: stop.wait(60), True)], daemon=True).start()
            try:
                started = time.time()
                d = subprocess.run(["./deploy.sh", "--no-mac"], cwd=HEAL_DIR, capture_output=True, text=True, timeout=1200, env=env)
            finally:
                stop.set()
            if d.returncode == 0:
                sha = git("rev-parse", "HEAD", cwd=HEAL_DIR)
                receipt = verify_heal_release(sha, started)
                if not git("status", "--porcelain") and git("merge", "--ff-only", "-q", sha, check=False) == "" and git("rev-parse", "HEAD") == sha:
                    note = ""
                else:  # the checkout moved on meanwhile: the fix waits on its own branch
                    git("branch", "-f", f"heal/{p['incident']}", sha)
                    note = f"（修复已上线，提交在分支 heal/{p['incident']}，还没合并进当前分支。）"
                return out | {"state": "fixed", "commit": sha[:10], "diagnosis": out["diagnosis"] + note,
                              "verification": {"status": "passed", "base": base, "regressions": regressions, **receipt}}
            log("heal", p["incident"], "deploy rejected the fix, round", rounds)
            git("reset", "-q", "--soft", "HEAD~1", cwd=HEAL_DIR)  # the fix stays in the tree for the next round
            if rounds >= HEAL_ROUNDS:
                return out | {"state": "diagnosed", "retry": False, "diagnosis": out["diagnosis"] +
                              f"（{rounds} 轮发布或上线验收未通过，已停止自动修复，需要查看部署日志。）",
                              "verification": {"status": "rejected", "base": base, "regressions": regressions},
                              "log": (d.stdout + d.stderr)[-3000:]}
            failed = (d.stdout + d.stderr)[-4000:]
    finally:
        git("worktree", "remove", "--force", str(HEAL_DIR), check=False)


def heal_loop():
    """Its own thread, one incident at a time (they're rare; a deploy at a time)."""
    name = me.name = f"{NAME}-heal"
    if not CODEX.exists() or not (REPO / ".git").exists():
        log("no Codex or no code at", REPO, ": 自修复 stays off")
        return
    while True:
        try:
            task = call("/api/tasks/claim", caps=["heal"], wait=25)["task"]
        except requests.RequestException:
            time.sleep(60)
            continue
        if not task:
            continue
        tid, started = task["id"], time.time()
        log("heal", tid, task["payload"].get("title"))
        try:
            result = heal(task, lambda: task_heartbeat(task, worker=name))
            call(f"/api/tasks/{tid}/done", claim_token=task.get("claim_token"), result=result)
            log("healed", tid, result["state"], f"{time.time() - started:.0f}s", result["summary"])
        except Exception as e:
            traceback.print_exc()
            try:
                call(f"/api/tasks/{tid}/fail", claim_token=task.get("claim_token"), error=str(e)[:500], retry=False)
            except requests.RequestException:
                pass


# ---- the claim loop

def work(task):
    """Do one claimed task, keeping the claim alive meanwhile; report done or failed."""
    stop, name = threading.Event(), whoami()

    def beat():
        me.name = name  # (its own thread: claims under the same worker as the loop it's for)
        while not stop.wait(60):
            try:
                if not call(f"/api/tasks/{task['id']}/heartbeat", claim_token=task.get("claim_token"))["ok"]:
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
        call(f"/api/tasks/{task['id']}/release", claim_token=task.get("claim_token"))
        return
    except requests.RequestException as e:  # the network, not the task: back on the board soon
        stop.set()
        traceback.print_exc()
        call(f"/api/tasks/{task['id']}/fail", claim_token=task.get("claim_token"), error=f"network: {e}", retry=True)
        return
    except Exception as e:  # the task itself (unreadable audio...): failed for good
        stop.set()
        traceback.print_exc()
        call(f"/api/tasks/{task['id']}/fail", claim_token=task.get("claim_token"), error=str(e), retry=False, trace=traceback.format_exc()[-8000:])
        return
    stop.set()
    call(f"/api/tasks/{task['id']}/done", claim_token=task.get("claim_token"), result=result)
    extra = (f"{result['language']}, {len(result['segments'])} lines" if task["kind"] == "transcribe" else
             f"{len(result['frames'])} of {result['keyframes']} frames kept" if task["kind"] == "frames" else
             f"{len(result['lines'])} lines of text" if task["kind"] == "cover" else "")
    log("done", task["id"], task["kind"], f"{time.time() - started:.0f}s", extra)


def pic_loop():
    """Its own thread: picture tasks beside Whisper while nobody uses the Mac."""
    me.name = f"{NAME}-pic"
    while True:
        if not idle():
            time.sleep(30)
            continue
        try:
            task = call("/api/tasks/claim", caps=PIC_CAPS, wait=25)["task"]
        except requests.RequestException:
            time.sleep(60)
            continue
        if task:
            try:
                work(task)
            except Exception:
                traceback.print_exc()
                time.sleep(30)


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
    threading.Thread(target=pic_loop, daemon=True).start()
    threading.Thread(target=heal_loop, daemon=True).start()
    for n in range(1, AI_LOOPS + AI_EXTRA + 1):
        threading.Thread(target=codex_loop, args=(n, n > AI_LOOPS), daemon=True).start()
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
            # idle: pictures are the second loop's, so this one doesn't cut keyframes on the Pi beside it
            task = call("/api/tasks/claim", caps=[c for c in CAPS if c not in PIC_CAPS or c == "gpu"] if idle() else CAPS,
                        wait=25)["task"]
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
