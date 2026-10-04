"""Following uploaders (追更): listing their videos, weekly checks."""
import hashlib
import json
import os
import re
import requests
import time
import traceback
import urllib.parse
from .core import (COOKIES, STATE, UA, db_lock, link_key, q, update)


# ---------------------------------------------------------------- following channels (追更)
# A link to an uploader's page (a B站 space, a YouTube channel) follows it instead of downloading one video:
# its latest videos are queued right away (SUB_BACKFILL of them, or all with "缓存全部"), and the worker
# checks it again every SUB_INTERVAL seconds (a week) and queues whatever is new, like following a show.
SUB_INTERVAL = int(os.environ.get("SUB_INTERVAL", str(7 * 86400)))  # once a week
SUB_BACKFILL = int(os.environ.get("SUB_BACKFILL", "50"))
SUB_WORKERS = int(os.environ.get("SUB_WORKERS", "2"))  # download slots followed channels may use; the rest stay free for links you send
SUB_SEEN_MAX = 5000
SUB_RETRY = 3600  # a failed check (e.g. B站 risk control) is tried again an hour later, not next week


def channel_of(url):
    """(platform, id, URL of the uploader's video list) when `url` is an uploader page, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "space.bilibili.com":
        m = re.match(r"/(\d+)", parts.path)
        if m:
            return "bilibili", m.group(1), f"https://space.bilibili.com/{m.group(1)}/upload/video"
    if host == "youtube.com":
        m = re.match(r"/(@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)", parts.path)
        if m:
            path = urllib.parse.unquote(m.group(1))
            return "youtube", path, f"https://www.youtube.com/{urllib.parse.quote(path, safe='/@')}/videos"
    return None


def add_sub(url, owner, device=None):
    """Follow an uploader. Returns (subscription id, "new" | "duplicate")."""
    platform, cid, videos = channel_of(url)
    key = f"{platform}:{cid.lower()}"
    row = q("SELECT id FROM subs WHERE owner IS ? AND key=?", (owner, key), one=True)
    if row:
        return row["id"], "duplicate"
    with db_lock:
        cur = core.DB.execute("INSERT INTO subs (owner, platform, key, url, name, backfill, device, created) VALUES (?,?,?,?,?,?,?,?)",
                         (owner, platform, key, videos, cid.removeprefix("@"), SUB_BACKFILL, device, time.time()))
        core.DB.commit()
    return cur.lastrowid, "new"  # the worker fetches the list within a minute (checked IS NULL)


MIXIN_KEY = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
             12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
             57, 62, 11, 36, 20, 34, 44, 52]


def bili_session():
    """A B站 web session that passes its risk control without logging in: browser cookies (buvid3/4)
    and the WBI key that signs space API requests."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://space.bilibili.com/"})
    s.get("https://www.bilibili.com/", timeout=15)
    spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15).json()["data"]
    s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
    s.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
    img = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()["data"]["wbi_img"]
    raw = "".join(u.rsplit("/", 1)[1].split(".")[0] for u in (img["img_url"], img["sub_url"]))
    s.wbi_key = "".join(raw[i] for i in MIXIN_KEY)[:32]
    return s


def bili_signed(s, params):
    params = {**params, "wts": int(time.time())}
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(params.items())}
    return {**params, "w_rid": hashlib.md5((urllib.parse.urlencode(params) + s.wbi_key).encode()).hexdigest()}


def list_bilibili(mid, limit):
    s = bili_session()
    card = s.get("https://api.bilibili.com/x/web-interface/card", params={"mid": mid}, timeout=15).json()["data"]
    out, total, pn = [], 0, 1
    while limit is None or len(out) < limit:
        params = {"mid": mid, "ps": 30, "pn": pn, "order": "pubdate", "platform": "web", "web_location": 1550101,
                  # canvas/WebGL fingerprint fields the web page sends; without them the API answers -352
                  "dm_img_list": "[]", "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
                  "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBIRCBHcmFwaGljcyBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApR29vZ2xlIEluYy4gKEludGVsKQ",
                  "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
        for attempt in range(6):  # B站 answers about half of these with HTTP 412 (risk control); retrying gets through
            r = s.get("https://api.bilibili.com/x/space/wbi/arc/search", params=bili_signed(s, params), timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"code": r.status_code}
            if body.get("code") == 0:
                break
            time.sleep(3 + attempt * 4)
        else:
            raise RuntimeError(f"B站列表获取失败（{body.get('code')} {body.get('message') or ''}）")
        data = body["data"]
        total = data["page"]["count"]
        vlist = data["list"]["vlist"] or []
        out += [{"url": f"https://www.bilibili.com/video/{v['bvid']}", "title": v["title"]} for v in vlist]
        if not vlist or pn * 30 >= total:
            break
        pn += 1
        time.sleep(1)
    return {"name": card["card"]["name"], "avatar": card["card"]["face"], "total": total,
            "entries": out[:limit] if limit else out}


def list_youtube(url, limit):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    if limit:
        opts["playlistend"] = limit
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [{"url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}", "title": e.get("title") or ""}
               for e in info.get("entries") or [] if e and e.get("id")]
    avatar = next((t["url"] for t in info.get("thumbnails") or [] if t.get("id") == "avatar_uncropped"), "")
    return {"name": info.get("channel") or info.get("uploader") or "", "avatar": avatar,
            "total": info.get("playlist_count") or (None if limit else len(entries)), "entries": entries}


def list_channel(sub, limit):
    """The uploader's videos, newest first (at most `limit`; None = all)."""
    if sub["platform"] == "bilibili":
        return list_bilibili(sub["key"].split(":", 1)[1], limit)
    return list_youtube(sub["url"], limit)


AVATARS = STATE / "avatars"


def check_sub(sub_id, everything=False):
    """Queue the uploader's videos not seen yet. First check: the newest `backfill` (0 = all);
    later checks only look at the newest 30; everything=True goes through the whole list.
    Returns how many were queued, or None if the list couldn't be fetched."""
    sub = q("SELECT * FROM subs WHERE id=?", (sub_id,), one=True)
    if not sub:
        return 0
    first = sub["seen"] in (None, "[]")  # no successful check yet
    limit = None if everything or (first and not sub["backfill"]) else sub["backfill"] if first else 30
    try:
        res = list_channel(sub, limit)
    except Exception as e:
        traceback.print_exc()
        q("UPDATE subs SET checked=?, error=? WHERE id=?", (time.time(), str(e)[:300], sub_id))
        return None
    seen = json.loads(sub["seen"] or "[]")
    seen_set = set(seen)
    new = [e for e in res["entries"] if link_key(e["url"]) not in seen_set]
    # oldest first, so the newest video gets the highest id and sits at the top of the page
    for e in reversed(new):
        jid, how = pipeline.add_job_ex(e["url"], source=f"sub:{sub_id}", owner=sub["owner"], device=sub["device"])
        if how == "new" and e["title"]:
            update(jid, title=e["title"])  # the list already has the title: show it while the video waits
        if how == "new" and (first or everything):
            update(jid, backfill=1)  # older videos: their subtitles are made in idle time
    seen = (seen + [link_key(e["url"]) for e in reversed(new)])[-SUB_SEEN_MAX:]
    avatar = sub["avatar"] or ""
    if res["avatar"] and not (AVATARS / f"{sub_id}.jpg").exists():
        try:
            r = requests.get(res["avatar"], timeout=20, headers={"User-Agent": UA})
            if r.ok:
                AVATARS.mkdir(parents=True, exist_ok=True)
                (AVATARS / f"{sub_id}.jpg").write_bytes(r.content)
                avatar = str(AVATARS / f"{sub_id}.jpg")
        except requests.RequestException:
            pass
    total = res["total"] if res["total"] is not None else sub["total"]
    # with a list that's longer than what we fetched, `total` is still how many the uploader has
    q("UPDATE subs SET name=?, avatar=?, total=?, seen=?, checked=?, error='', backfill=? WHERE id=?",
      (res["name"] or sub["name"], avatar, total or len(res["entries"]), json.dumps(seen), time.time(),
       0 if everything else sub["backfill"], sub_id))
    if new:
        print(f"sub {sub_id} {res['name']}: queued {len(new)}")
    return len(new)


def sub_loop():
    """Worker: check followed channels that are due (new ones, refresh requests, and every SUB_INTERVAL)."""
    while True:
        try:
            for r in q("SELECT id, everything FROM subs WHERE checked IS NULL OR checked < ? OR (error != '' AND checked < ?) "
                       "ORDER BY checked IS NOT NULL, checked", (time.time() - SUB_INTERVAL, time.time() - SUB_RETRY)):
                if check_sub(r["id"], everything=bool(r["everything"])) is not None:
                    q("UPDATE subs SET everything=0 WHERE id=?", (r["id"],))
        except Exception:
            traceback.print_exc()
        time.sleep(30)


# The other modules, imported last: they import this one too, and are only used at run time
from . import core, pipeline  # noqa: E402
