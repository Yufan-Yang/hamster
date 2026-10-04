"""Search: subtitles and text on pictures, what pictures look like (Chinese-CLIP), forgiving note search, re-uploads."""
import functools
import json
import re
import requests
import subprocess
import threading
import time
import traceback

from flask import g
from pathlib import Path
from .core import (STATE, log_usage, q)


def subtitle_hits(d, row, term, limit=30):
    """Where `term` is said: [{part, t, text}] from the subtitle files, else from speech-to-text segments."""
    term, hits = term.lower(), []
    for n, m in enumerate(library.playable(d)):
        for sub in m["subs"][:1]:  # the first track: others are usually the same lines in another language
            try:
                cues = library._cues(sub, Path(sub).stat().st_mtime)
            except OSError:
                continue
            hits += [{"part": n, "t": t, "text": x} for t, x in cues if term in x.lower()]
    if not hits and row["segments"]:
        hits = [{"part": 0, "t": t, "text": x} for t, x in json.loads(row["segments"]) if term in x.lower()]
    return hits[:limit]


def titleish(d):
    a = d.get("analysis") or {}
    return " ".join(str(x or "") for x in (d.get("title"), a.get("title"), a.get("show"), a.get("creator")))


def match_fields(d, r):
    a = d.get("analysis") or {}
    return [("简介", " ".join(str(a.get(k) or "") for k in ("summary", "brief"))),
            ("要点", " · ".join(map(str, a.get("key_points") or []))),
            ("标签", " · ".join(map(str, a.get("tags") or []))),
            ("字幕", r["transcript"] or ""),
            ("链接", d.get("url") or ""),
            ("文件", " ".join(map(str, d.get("files") or [])))]


def snippet(text, term, before=16, after=60):
    """A bit of `text` around `term`, starting just before it: cards show only two lines, so the hit has to be
    near the start or it's cut off (and the result looks unrelated)."""
    i = text.lower().find(term.lower())
    if i < 0:
        return ""
    start = max(0, i - before)
    return ("…" if start else "") + text[start:i + len(term) + after].replace("\n", " ") + "…"


# ---------------------------------------------------------------- search index: pictures, subtitles, idle work
#
# Everything slow happens ahead of time, in idle time, so a search is only a lookup:
#   seg  – timed lines: subtitle cues (downloaded, or made here with speech-to-text), text read off covers
#   vec  – CLIP vectors (Chinese-CLIP): covers, note photos, a video frame every FRAME_EVERY seconds
# A search runs LIKE over seg and one matrix product over vec (the query's text vector against every picture).
# No LLM is involved: it can't see pictures, and the local models are free and keep diaries private.

CLIP_DIR = STATE / "models" / "clip"
CLIP_REPO = "https://huggingface.co/Xenova/chinese-clip-vit-base-patch16/resolve/main/"
# How clearly above its own baseline a picture must match, by what it is. Measured on 2,545 frames: things that
# are in them (卡车 大桥 地图 士兵 沙漠 ...) top out at 0.055-0.13 with all of the top 6 right; things that aren't
# (猫 钢琴 篮球 雪山 ...) stay below 0.05. Covers are designed graphics, photos were calibrated on the 随记 photos.
CLIP_MARGINS = {"画面": 0.055, "封面": 0.065, "照片": 0.07}
CLIP_MEAN, CLIP_STD = (0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)
# everyday words: a picture's average likeness to these is its baseline (some pictures resemble everything a bit)
CLIP_ANCHORS = ["人", "男人", "女人", "孩子", "一群人", "人脸", "文字", "屏幕", "电脑", "手机", "房间", "桌子", "街道", "城市",
                "建筑", "天空", "大海", "山", "树", "花", "草地", "食物", "饮料", "汽车", "动物", "狗", "猫", "鸟", "衣服",
                "书", "地图", "图表", "舞台", "室内", "室外", "夜晚", "运动", "乐器", "会议", "照片", "画", "卡通", "风景",
                "海报", "演讲", "厨房", "办公室", "商店", "交通工具", "游戏"]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
_models = {}
_model_lock = threading.Lock()


def clip_files():
    """Download Chinese-CLIP (ONNX, int8) once and split it into its text half (for queries, kept in the web
    process) and its image half (for indexing), so neither has to load the other."""
    if (CLIP_DIR / "text.onnx").exists() and (CLIP_DIR / "vision.onnx").exists():
        return
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    for name, src in (("tokenizer.json", "tokenizer.json"), ("model.onnx", "onnx/model_quantized.onnx")):
        if not (CLIP_DIR / name).exists():
            part = CLIP_DIR / (name + ".part")
            with requests.get(CLIP_REPO + src, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            part.rename(CLIP_DIR / name)
    import onnx.utils
    whole = str(CLIP_DIR / "model.onnx")
    onnx.utils.extract_model(whole, str(CLIP_DIR / "text.onnx"), ["input_ids", "attention_mask"], ["text_embeds"])
    onnx.utils.extract_model(whole, str(CLIP_DIR / "vision.onnx"), ["pixel_values"], ["image_embeds"])
    (CLIP_DIR / "model.onnx").unlink()


def _onnx(name):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    return ort.InferenceSession(str(CLIP_DIR / name), so)


@functools.lru_cache(maxsize=512)
def clip_text(text):
    import numpy as np
    with _model_lock:
        if "text" not in _models:
            clip_files()
            from tokenizers import Tokenizer
            _models["tok"] = Tokenizer.from_file(str(CLIP_DIR / "tokenizer.json"))
            _models["text"] = _onnx("text.onnx")
        ids = np.array([_models["tok"].encode(text).ids[:52]], np.int64)
        v = _models["text"].run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0]
    return v / np.linalg.norm(v)


def clip_anchors():
    if "anchors" not in _models:
        import numpy as np
        path = CLIP_DIR / "anchors.npy"
        if not path.exists():
            np.save(path, np.stack([clip_text(w) for w in CLIP_ANCHORS]))
        _models["anchors"] = np.load(path)
    return _models["anchors"]


def clip_image(path, t=None):
    """CLIP vector of a picture, or of the frame `t` seconds into a video; None if it can't be read."""
    import numpy as np
    pixels = None
    if t is None and Path(path).suffix.lower() in IMAGE_EXT:
        try:  # Pillow follows the photo's EXIF rotation
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                pixels = np.asarray(ImageOps.exif_transpose(im).convert("RGB").resize((224, 224), Image.BICUBIC))
        except Exception:
            pixels = None
    if pixels is None:
        raw = subprocess.run(["ffmpeg", "-v", "error", *(["-ss", str(t)] if t is not None else []), "-i", str(path),
                              "-frames:v", "1", "-vf", "scale=224:224:flags=bicubic", "-f", "rawvideo", "-pix_fmt", "rgb24",
                              "-"], capture_output=True, timeout=120).stdout
        if len(raw) != 224 * 224 * 3:
            return None
        pixels = np.frombuffer(raw, np.uint8).reshape(224, 224, 3)
    a = (pixels.astype(np.float32) / 255 - np.array(CLIP_MEAN, np.float32)) / np.array(CLIP_STD, np.float32)
    with _model_lock:
        if "vision" not in _models:
            clip_files()
            _models["vision"] = _onnx("vision.onnx")
        v = _models["vision"].run(None, {"pixel_values": a.transpose(2, 0, 1)[None]})[0][0]
    return v / np.linalg.norm(v)


# The image model the vectors come from (the Pi's int8 Chinese-CLIP; the Mac uses a copy of the same file). Vectors
# of different models can't be compared: search only uses this model's, and a new model means re-indexing
# (see reindex_old_model).
CLIP_MODEL = "chinese-clip-vit-base-patch16-int8"


def store_vec(kind, ref, part, t, src, v, model=CLIP_MODEL):
    import numpy as np
    q("INSERT INTO vec (kind, ref, part, t, src, base, v, model) VALUES (?,?,?,?,?,?,?,?)",
      (kind, ref, part, t, src, float((clip_anchors() @ v).mean()), v.astype(np.float32).tobytes(), model))


def ocr_text(path):
    """Lines of text read off a picture (RapidOCR: PaddleOCR's models on onnxruntime)."""
    if "ocr" not in _models:
        from rapidocr_onnxruntime import RapidOCR
        _models["ocr"] = RapidOCR()
    started = time.time()
    found, _ = _models["ocr"](str(path))
    lines = [str(text).strip() for _, text, score in (found or []) if float(score) >= 0.6 and len(str(text).strip()) >= 2]
    log_usage("ocr", "picture", amount=1, seconds=time.time() - started)
    return lines


_vecs = {"key": None, "rows": 0}


def vec_matrix():
    """All picture vectors, cached in this process; new rows are appended, a removal reloads everything."""
    import numpy as np
    top = q("SELECT COALESCE(MAX(id), 0) m, COUNT(*) n FROM vec WHERE model=?", (CLIP_MODEL,), one=True)
    if _vecs["key"] == (top["m"], top["n"]):
        return _vecs
    after = _vecs["last"] if _vecs["key"] and top["n"] - _vecs["rows"] == top["m"] - _vecs["last"] else 0
    rows = q("SELECT id, kind, ref, part, t, src, base, v FROM vec WHERE id > ? AND model=? ORDER BY id", (after, CLIP_MODEL))
    meta = [(r["kind"], r["ref"], r["part"], r["t"], r["src"]) for r in rows]
    # the bar to clear: the picture's baseline plus the margin for its kind
    bar = np.array([r["base"] + CLIP_MARGINS[r["src"].split(":")[0]] for r in rows], np.float32)
    # half precision: a frame every few seconds of 150 hours of video is ~100k vectors, 100 MB instead of 200
    m = np.frombuffer(b"".join(r["v"] for r in rows), np.float32).reshape(len(rows), -1).astype(np.float16) \
        if rows else np.zeros((0, 512), np.float16)
    if after:
        meta, bar, m = _vecs["meta"] + meta, np.concatenate([_vecs["bar"], bar]), np.concatenate([_vecs["m"], m])
    _vecs.update(key=(top["m"], top["n"]), rows=top["n"], last=top["m"], meta=meta, bar=bar, m=m)
    return _vecs


def visual_hits(term, kind, allowed):
    """{ref: [(margin, part, t, src) best first]} for pictures that clearly look like `term`."""
    # Chinese-CLIP understands Chinese; English words and single characters mostly match noise
    if len(term) < 2 or not re.search(r"[\u4e00-\u9fff]", term):
        return {}
    try:
        V = vec_matrix()
        if not len(V["meta"]):
            return {}
        import numpy as np
        qv = clip_text(term).astype(np.float32)
        sims = np.concatenate([V["m"][i:i + 20000].astype(np.float32) @ qv for i in range(0, len(V["m"]), 20000)])
        margin = sims - V["bar"]  # >= 0: clearly looks like it
        idx = np.where(margin >= 0)[0]
    except Exception:
        traceback.print_exc()
        return {}
    out = {}
    for i in idx[np.argsort(-margin[idx])]:
        k, ref, part, t, src = V["meta"][i]
        if k == kind and ref in allowed:
            out.setdefault(ref, []).append((float(margin[i]), part, t, src))
    return out


def forget_index(jid):
    q("DELETE FROM seg WHERE kind='job' AND ref=?", (jid,))
    q("DELETE FROM vec WHERE kind='job' AND ref=?", (jid,))
    q("DELETE FROM tasks WHERE target=? OR target LIKE ?", (f"job:{jid}", f"job:{jid}:%"))


def vec_from(b64):
    import base64
    import numpy as np
    v = np.frombuffer(base64.b64decode(b64), np.float16).astype(np.float32)
    return v / np.linalg.norm(v)


def vec_to(v):
    import base64
    import numpy as np
    return base64.b64encode(np.asarray(v, np.float16).tobytes()).decode()


# ---- the same content twice: a re-upload, or a clip of a longer video

MINHASH_N = 128
_mh = {}


def minhash(text):
    """(MinHash signature, number of distinct 6-character shingles) of what's said, punctuation and spaces dropped."""
    import numpy as np
    import zlib
    t = re.sub(r"[\W_]+", "", text.casefold())
    sh = {zlib.crc32(t[i:i + 6].encode()) for i in range(max(len(t) - 5, 0))}
    if not sh:
        return None, 0
    if "ab" not in _mh:
        rng = np.random.default_rng(42)  # the same permutations everywhere, every time
        _mh["ab"] = (rng.integers(1, (1 << 31) - 1, MINHASH_N, dtype=np.uint64), rng.integers(0, (1 << 31) - 1, MINHASH_N, dtype=np.uint64))
    a, b = _mh["ab"]
    x = np.fromiter(sh, np.uint64)
    sig = ((x[:, None] * a[None, :] + b[None, :]) % np.uint64((1 << 31) - 1)).min(axis=0)
    return sig.astype(np.uint32), len(sh)


def job_frames(jid):
    import numpy as np
    rows = q("SELECT v FROM vec WHERE kind='job' AND ref=? AND src='画面' ORDER BY part, t", (jid,))
    return np.stack([np.frombuffer(r["v"], np.float32) for r in rows]) if rows else None


def _within(a, b, k):
    """Whether the edit distance between a and b is at most k."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > k:
            return False
        prev = cur
    return prev[-1] <= k


@functools.lru_cache(maxsize=20000)
def _pinyin(ch):
    try:
        from pypinyin import lazy_pinyin
        return lazy_pinyin(ch)[0]
    except Exception:  # pypinyin not installed: no pinyin search
        return ""


def _note_find(hay, low, tok):
    """Where `tok` (one search word, casefolded) is in a note: as typed, with a typo or a letter more or less
    (Yannan ~ yanan), or as the pinyin / pinyin initials of Chinese (yanan, ya → 延安). The matched text, or None."""
    i = low.find(tok)
    if i >= 0:
        return hay[i:i + len(tok)]
    if not re.fullmatch(r"[a-z0-9]+", tok):
        return None
    if len(tok) >= 4:
        k = 1 if len(tok) < 8 else 2
        for m in re.finditer(r"[a-z0-9]+", low):
            if _within(tok, m.group(), k):
                return hay[m.start():m.end()]
    if not tok.isalpha() or len(tok) < 2:
        return None
    for run in re.finditer(r"[\u4e00-\u9fff]+", hay):
        py = [_pinyin(c) for c in run.group()]
        for a in range(len(py)):
            full = initials = ""
            for b in range(a, len(py)):
                full += py[b]
                initials += py[b][:1]
                if full == tok or initials == tok or (len(tok) >= 4 and _within(tok, full, 1)) \
                        or (b > a and full.startswith(tok) and len(tok) > len(full) - len(py[b])):
                    return run.group()[a:b + 1]
                if len(full) > len(tok) + 1 and len(initials) >= len(tok):
                    break
    return None


def note_marks(r, term):
    """The matched bits of text when every word of `term` is in the note (text or what's said in it), else None."""
    hay = r["text"] + "\n" + "\n".join(m.get("transcript", "") + "\n" + m.get("ocr", "") + "\n" + m.get("place", "")
                                       for m in json.loads(r["media"]))
    low, marks = hay.casefold(), []
    for tok in term.casefold().split():
        m = _note_find(hay, low, tok)
        if m is None:
            return None
        marks.append(m)
    return marks


def notes_search(term):
    """[(note, matched text bits, photos that look like it)]: by text, what's said or written in it, and by look."""
    rows = q("SELECT * FROM notes WHERE owner=? ORDER BY created DESC, id DESC", (g.owner,))
    looks = visual_hits(term, "note", {r["id"] for r in rows})
    out = []
    for r in rows:
        marks = note_marks(r, term)
        files = [m["file"] for m in json.loads(r["media"])]
        seen = [files.index(src[3:]) for _, _, _, src in looks.get(r["id"], []) if src[3:] in files]
        if marks is not None or seen:
            out.append((r, marks or [], seen))
    return out


def point_times(jid):
    """When each of the summary's key points is talked about: the 45-second stretch of subtitles that shares the
    most (rarer) two-character pieces with the point. Free (no AI), and good enough to jump close to it;
    points that match nothing well get no time."""
    import math
    row = q("SELECT analysis, ref FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    points = a.get("key_points") or []
    source = row["ref"] or jid  # an entry linked to another account's download shares its subtitles
    lines = q("SELECT part, t, text FROM seg WHERE kind='job' AND ref=? AND src IN ('字幕','译文') AND t IS NOT NULL "
              "ORDER BY src='译文' DESC, part, t", (source,))
    if not points or not lines:
        return []
    zh_first = any(r["text"] and re.search(r"[\u4e00-\u9fff]", r["text"]) for r in lines[:50])
    lines = [r for r in lines if not zh_first or re.search(r"[\u4e00-\u9fff]", r["text"] or "")]

    def grams(text):
        t = re.sub(r"[\W_]+", "", text.casefold())
        return {t[i:i + 2] for i in range(len(t) - 1)}
    windows = []
    for i, r in enumerate(lines):
        text, j = [], i
        while j < len(lines) and lines[j]["part"] == r["part"] and lines[j]["t"] - r["t"] < 45:
            text.append(lines[j]["text"])
            j += 1
        windows.append((r["part"], r["t"], grams(" ".join(text))))
    df = {}
    for _, _, gr in windows:
        for x in gr:
            df[x] = df.get(x, 0) + 1
    idf = {x: math.log(len(windows) / n) for x, n in df.items()}
    out = []
    for p in points:
        pg = grams(p)
        total = sum(idf.get(x, 0) for x in pg) or 1
        best = max(windows, key=lambda w: sum(idf.get(x, 0) for x in pg & w[2]))
        score = sum(idf.get(x, 0) for x in pg & best[2]) / total
        out.append({"part": best[0], "t": round(best[1], 1)} if score >= 0.3 else None)
    return out


# The other modules, imported last: they import this one too, and are only used at run time
from . import library  # noqa: E402
