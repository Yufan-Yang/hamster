"""随记: notes with photos, videos and voice; where they were taken."""
import json
import re
import secrets
import time

from flask import g
from pathlib import Path
from .core import (AUDIO_EXT, NOTES_DIR, STATE, VIDEO_EXT, ffprobe, q)


# ---------------------------------------------------------------- 随记: everyday notes

NOTE_KINDS = {"image": "image/", "video": "video/", "audio": "audio/"}


def note_kind(f):
    kind = next((k for k, p in NOTE_KINDS.items() if (f.mimetype or "").startswith(p)), None)
    if kind:
        return kind
    ext = Path(f.filename or "").suffix.lower()
    return "image" if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"} else \
        "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else None


def note_row(nid):
    """The note if it's this browser's / account's own: notes are private, the admin doesn't see other people's."""
    row = q("SELECT * FROM notes WHERE id=?", (nid,), one=True)
    return row if row and row["owner"] == g.owner else None


# ---- where a photo or video was taken: its GPS, named offline from GeoNames' cities (nothing is sent anywhere)

GEO_DIR = STATE / "models" / "geo"
COUNTRY_ZH = {"CN": "", "HK": "香港", "MO": "澳门", "TW": "台湾", "US": "美国", "CA": "加拿大", "JP": "日本", "KR": "韩国",
              "GB": "英国", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙", "PT": "葡萄牙", "NL": "荷兰",
              "CH": "瑞士", "AT": "奥地利", "TH": "泰国", "SG": "新加坡", "MY": "马来西亚", "ID": "印度尼西亚", "VN": "越南",
              "PH": "菲律宾", "AU": "澳大利亚", "NZ": "新西兰", "IN": "印度", "RU": "俄罗斯", "AE": "阿联酋", "TR": "土耳其",
              "EG": "埃及", "MX": "墨西哥", "BR": "巴西", "IS": "冰岛", "NO": "挪威", "SE": "瑞典", "FI": "芬兰", "DK": "丹麦"}
_places = {}


def place_name(lat, lon):
    """The biggest town within ~30 km (else the nearest one): "上海", "温莎 · 加拿大". (GeoNames lists city
    districts too; the nearest alone gave "黄浦" for the Bund and "Yoyogi" for Tokyo.)"""
    import numpy as np
    if "names" not in _places:
        import zipfile
        from opencc import OpenCC
        cc = OpenCC("t2s")
        rows = []
        with zipfile.ZipFile(GEO_DIR / "cities15000.zip") as z:
            for line in z.read("cities15000.txt").decode().splitlines():
                f = line.split("\t")
                # the Chinese name most of its Chinese alternatives agree on (深圳/深圳市 over an old name 宝安)
                alts = [cc.convert(a).removesuffix("市") for a in f[3].split(",") if re.fullmatch(r"[\u4e00-\u9fff]{1,8}", a)]
                zh = max(set(alts), key=lambda a: (alts.count(a), -len(a))) if alts else None
                if f[7] == "PPLX":  # a section of a city
                    continue
                rows.append((float(f[4]), float(f[5]), zh or f[1], f[8],
                             int(f[14] or 0)))
        _places["xy"] = np.radians(np.array([(r[0], r[1]) for r in rows]))
        _places["names"] = [(r[2], r[3]) for r in rows]
        _places["pop"] = np.array([r[4] for r in rows], np.float64)
    la, lo = np.radians(lat), np.radians(lon)
    xy = _places["xy"]
    d = (xy[:, 0] - la) ** 2 + ((xy[:, 1] - lo) * np.cos(la)) ** 2  # flat-earth distance: fine at this scale
    nearest = int(np.argmin(d))
    # the biggest town nearby in the same country (Windsor stays in Canada, not Detroit across the river)
    near = [i for i in np.where(d < (30 / 6371) ** 2)[0] if _places["names"][i][1] == _places["names"][nearest][1]]
    best = max(near, key=lambda i: _places["pop"][i]) if near else nearest
    name, country = _places["names"][best]
    zh = COUNTRY_ZH.get(country, country)
    return f"{name} · {zh}" if zh and zh != name else name


def photo_gps(path):
    """(lat, lon) from a photo's EXIF, or None (most pictures sent from a phone's browser have it stripped)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            gps = im.getexif().get_ifd(0x8825)
        if not gps or 2 not in gps or 4 not in gps:
            return None
        def deg(v, ref):
            x = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -x if ref in ("S", "W") else x
        return deg(gps[2], gps.get(1, "N")), deg(gps[4], gps.get(3, "E"))
    except Exception:
        return None


def video_gps(path):
    """(lat, lon) from a phone video's metadata (ISO 6709, e.g. "+31.2304+121.4737+004.000/"), or None."""
    tags = ffprobe(path).get("format", {}).get("tags", {})
    loc = next((v for k, v in tags.items() if "location" in k.lower()), "")
    m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", loc)
    return (float(m.group(1)), float(m.group(2))) if m else None


def note_dict(r, marks=None, seen=None):
    media = json.loads(r["media"])
    d = {"id": r["id"], "text": r["text"], "created": r["created"], "updated": r["updated"], "device": r["device"],
         "pending": bool(r["pending"]), "tags": json.loads(r["tags"] or "[]"),
         "media": [{"kind": m["kind"], "duration": m.get("duration"), "poster": bool(m.get("poster")),
                    "transcript": m.get("tidy") or m.get("transcript", ""), "place": m.get("place")} for m in media],
         "place": next((m["place"] for m in media if m.get("place")), None)}
    if marks:
        d["marks"] = marks
        missing = [k for k in marks if k not in r["text"]]
        if missing:  # found in what's said in a voice note / video, or written on a photo: show where
            d["match"], d["match_where"] = next(((search.snippet(m.get(k, ""), missing[0]), label) for m in media
                                                 for k, label in (("tidy", "语音"), ("transcript", "语音"), ("ocr", "图中文字"))
                                                 if missing[0] in m.get(k, "")), ("", ""))
    if seen:
        d["seen"] = seen  # photos that look like the search
        if not marks:
            d["match"], d["match_where"] = "照片看起来像", "照片"
    return d


def save_note_files(nid, files):
    NOTES_DIR.mkdir(mode=0o700, exist_ok=True)
    out = []
    for f in files:
        kind = note_kind(f)
        if not kind:
            continue
        ext = (Path(f.filename or "").suffix.lower() or {"image": ".jpg", "video": ".mp4", "audio": ".m4a"}[kind])[:8]
        name = f"{nid}-{secrets.token_hex(4)}{ext}"
        f.save(NOTES_DIR / name)
        out.append({"kind": kind, "file": name, "todo": True})
    return out


def note_time(v):
    """A note date from the page (unix seconds), or None when missing or absurd."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    return t if 0 < t < time.time() + 366 * 86400 else None


# The other modules, imported last: they import this one too, and are only used at run time
from . import search  # noqa: E402
