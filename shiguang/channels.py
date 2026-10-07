"""Following uploaders (追更): listing their videos, weekly checks."""
import hashlib
import http.cookiejar
import json
import os
import re
import requests
import time
import traceback
import urllib.parse
from copy import copy
from .core import (COOKIES, STATE, UA, db_lock, link_key, q, update)


# ---------------------------------------------------------------- following channels (追更)
# A link to an uploader's page (a B站 space, a YouTube channel, a Pornhub model / pornstar / channel) follows it instead of downloading one video:
# its latest videos are queued right away (SUB_BACKFILL of them, or all with "缓存全部"), and the worker
# checks it again every SUB_INTERVAL seconds (a week) and queues whatever is new, like following a show.
SUB_INTERVAL = int(os.environ.get("SUB_INTERVAL", str(7 * 86400)))  # once a week
SUB_BACKFILL = int(os.environ.get("SUB_BACKFILL", "50"))
SUB_WORKERS = int(os.environ.get("SUB_WORKERS", "2"))  # download slots followed channels may use; the rest stay free for links you send
SUB_SEEN_MAX = 5000
SUB_RETRY = 3600  # a failed check (e.g. B站 risk control) is tried again an hour later, not next week
SUB_FILTER_SCAN = int(os.environ.get("SUB_FILTER_SCAN", "300"))  # with a filter, the first check looks this far back
# Adult sites: their uploaders are left out of 追更周报 (its AI headline covers everyone) and, in privacy mode, out of
# every list that names uploaders (web.hidden_subs)
ADULT_PLATFORMS = {"pornhub"}


def channel_of(url, _followed_short=False):
    """(platform, id, URL of the uploader's video list) when `url` is an uploader page, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "b23.tv" and not _followed_short:
        # Resolve only the known B站 shortener, once, before classifying the
        # resulting space URL. Other submitted URLs are never fetched here.
        try:
            resolved = requests.get(url.strip(), headers={"User-Agent": UA},
                                    allow_redirects=True, timeout=10).url
        except requests.RequestException:
            return None
        if resolved and resolved.strip() != url.strip():
            return channel_of(resolved, _followed_short=True)
    if host == "space.bilibili.com":
        m = re.match(r"/(\d+)", parts.path)
        if m:
            return "bilibili", m.group(1), f"https://space.bilibili.com/{m.group(1)}/upload/video"
    if host == "youtube.com":
        m = re.match(r"/(@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+)", parts.path)
        if m:
            path = urllib.parse.unquote(m.group(1))
            return "youtube", path, f"https://www.youtube.com/{urllib.parse.quote(path, safe='/@')}/videos"
    if host == "pornhub.com" or host.endswith(".pornhub.com"):  # a model / pornstar / channel / user page
        m = re.match(r"/(model|pornstar|channels|users)/([\w.-]+)", parts.path)
        if m:
            path = f"{m.group(1)}/{m.group(2).lower()}"
            return "pornhub", path, f"https://www.pornhub.com/{path}/videos" + ("/public" if m.group(1) == "users" else "")
    return None


def add_sub(url, owner, device=None, flt=None):
    """Follow an uploader, caching only the videos `flt` lets through (see clean_filter; None = all of them).
    Returns (subscription id, "new" | "duplicate")."""
    platform, cid, videos = channel_of(url)
    key = f"{platform}:{cid.lower()}"
    row = q("SELECT id FROM subs WHERE owner IS ? AND key=?", (owner, key), one=True)
    if row:
        if flt:  # followed already: the filter sent with it replaces the old one
            set_filter(row["id"], flt)
        return row["id"], "duplicate"
    with db_lock:
        cur = core.DB.execute("INSERT INTO subs (owner, platform, key, url, name, backfill, device, created, filter) "
                              "VALUES (?,?,?,?,?,?,?,?,?)",
                              (owner, platform, key, videos, cid.removeprefix("@").rsplit("/", 1)[-1], SUB_BACKFILL, device,
                               time.time(), json.dumps(flt, ensure_ascii=False) if flt else ""))
        core.DB.commit()
    return cur.lastrowid, "new"  # the worker fetches the list within a minute (checked IS NULL)


# ---------------------------------------------------------------- filters: which of an uploader's videos to cache
# A filter is a group: {"op": "and" | "or", "items": [...]}, each item a condition or another group (nested at most
# FILTER_DEPTH deep), so "(keyword A or keyword B) and after 2026-01-01" is
#   {"op": "and", "items": [{"op": "or", "items": [{"f": "title", "op": "has", "v": "A"}, {...}]},
#                           {"f": "date", "op": "after", "v": "2026-01-01"}]}
# Conditions:  title has / not  (keyword, case-insensitive)   ·   date after / before  (YYYY-MM-DD, both ends included)
#              len gt / lt  (minutes)
# A video whose list entry doesn't tell its date or length (Pornhub's) passes those conditions: they can't be judged.
FILTER_DEPTH = 3
FILTER_OPS = {"title": ("has", "not"), "date": ("after", "before"), "len": ("gt", "lt")}


def clean_filter(f, depth=1):
    """`f` as sent by the page, checked and tidied: empty conditions and groups dropped. None when nothing is left;
    ValueError when it's malformed."""
    if not isinstance(f, dict):
        raise ValueError("筛选条件格式不对")
    if "items" in f:
        if depth > FILTER_DEPTH or f.get("op") not in ("and", "or") or not isinstance(f["items"], list):
            raise ValueError("筛选条件格式不对")
        items = [c for c in (clean_filter(x, depth + 1) for x in f["items"][:50]) if c]
        return {"op": f["op"], "items": items} if items else None
    field, op, v = f.get("f"), f.get("op"), f.get("v")
    if op not in FILTER_OPS.get(field, ()):
        raise ValueError("筛选条件格式不对")
    if field == "title":
        v = str(v or "").strip()[:100]
        return {"f": field, "op": op, "v": v} if v else None
    if field == "date":
        v = str(v or "").strip()
        if not v:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError(f"日期要写成 2026-01-31 这样：{v}")
        return {"f": field, "op": op, "v": v}
    try:
        v = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError(f"时长要填分钟数：{v}")
    return {"f": field, "op": op, "v": v} if v is not None and v >= 0 else None


def matches(f, e):
    """Whether list entry `e` (title, and date / duration when the list has them) passes filter `f` (None: all do)."""
    if not f:
        return True
    if "items" in f:
        hits = (matches(x, e) for x in f["items"])
        return all(hits) if f["op"] == "and" else any(hits)
    if f["f"] == "title":
        has = f["v"].casefold() in (e.get("title") or "").casefold()
        return has if f["op"] == "has" else not has
    if f["f"] == "date":
        if not e.get("date"):
            return True
        return e["date"] >= f["v"] if f["op"] == "after" else e["date"] <= f["v"]
    if not e.get("duration"):
        return True
    return e["duration"] >= f["v"] * 60 if f["op"] == "gt" else e["duration"] <= f["v"] * 60


def set_filter(sub_id, flt):
    """Change what's cached of this uploader: the next check (within a minute) looks through the latest videos again,
    queues the ones that now pass and drops the queued ones that no longer do."""
    q("UPDATE subs SET filter=?, refilter=1, checked=NULL WHERE id=?",
      (json.dumps(flt, ensure_ascii=False) if flt else "", sub_id))


MIXIN_KEY = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
             12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
             57, 62, 11, 36, 20, 34, 44, 52]


BILI_LOGIN_COOKIES = {"SESSDATA", "bili_jct", "DedeUserID"}


def bili_cookie_names(cookies):
    return {c.name for c in cookies if c.domain.lstrip(".").endswith("bilibili.com")}


def bili_cookie_state():
    """Whether the shared cookie file has the login cookies a B站 listing needs.

    Cookie values deliberately never leave the server. This is only used to
    tell the administrator why a listing is being rejected.
    """
    if not COOKIES.exists():
        return "missing"
    try:
        jar = http.cookiejar.MozillaCookieJar(str(COOKIES))
        jar.load(ignore_discard=True, ignore_expires=True)
        names = bili_cookie_names(jar)
    except (OSError, http.cookiejar.LoadError):
        return "invalid"
    return "ready" if BILI_LOGIN_COOKIES <= names else "incomplete"


def save_bili_cookies(cookies):
    """Merge a newly authenticated B站 session into the shared cookie file."""
    fresh = [copy(c) for c in cookies if c.domain.lstrip(".").endswith("bilibili.com")]
    if not BILI_LOGIN_COOKIES <= bili_cookie_names(fresh):
        raise ValueError("登录没有返回完整的 B站 Cookie，请重新扫码")
    jar = http.cookiejar.MozillaCookieJar()
    if COOKIES.exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES))
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            jar = http.cookiejar.MozillaCookieJar()
    for c in list(jar):
        if c.domain.lstrip(".").endswith("bilibili.com"):
            jar.clear(c.domain, c.path, c.name)
    for c in fresh:
        jar.set_cookie(c)
    COOKIES.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIES.with_name(f".{COOKIES.name}.{os.getpid()}")
    try:
        jar.filename = str(tmp)
        jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, COOKIES)
    finally:
        tmp.unlink(missing_ok=True)


def bili_session(mid):
    """A B站 space session with real browser cookies and a fresh WBI key.

    A logged-in cookies.txt is optional, but must be used here too: recent B站
    risk control rejects the listing API when it sees only SESSDATA, or only a
    newly invented buvid pair.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"https://space.bilibili.com/{mid}/upload/video",
                      "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9"})
    if COOKIES.exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES))
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                if c.domain.lstrip(".").endswith("bilibili.com"):
                    s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except (OSError, http.cookiejar.LoadError):
            pass
    # Warm the same space page first.  This supplies the browser-side cookies
    # and makes each retry a genuinely new browser session, rather than six
    # copies of a request B站 already decided to reject.
    s.get(f"https://space.bilibili.com/{mid}/upload/video", timeout=20)
    s.get("https://www.bilibili.com/", timeout=15)
    spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15).json()["data"]
    if not any(c.name == "buvid3" for c in s.cookies):
        s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
    if not any(c.name == "buvid4" for c in s.cookies):
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
    s = bili_session(mid)
    card = s.get("https://api.bilibili.com/x/web-interface/card", params={"mid": mid}, timeout=15).json()["data"]
    out, total, pn = [], 0, 1
    while limit is None or len(out) < limit:
        params = {"mid": mid, "ps": 30, "pn": pn, "order": "pubdate", "order_avoided": "true", "platform": "web", "web_location": 1550101,
                  # canvas/WebGL fingerprint fields the web page sends; without them the API answers -352
                  "dm_img_list": "[]", "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
                  "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBIRCBHcmFwaGljcyBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApR29vZ2xlIEluYy4gKEludGVsKQ",
                  "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
        for attempt in range(6):
            if attempt:
                # 412 is bound to the fingerprint/session.  Reuse would only
                # repeat it, so start again and re-fetch the rotating WBI key.
                s = bili_session(mid)
            r = s.get("https://api.bilibili.com/x/space/wbi/arc/search", params=bili_signed(s, params), timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"code": r.status_code}
            if body.get("code") == 0:
                break
            if body.get("code") not in (-412, -352, 412) and r.status_code < 500:
                raise RuntimeError(f"B站列表获取失败（{body.get('code')} {body.get('message') or ''}）")
            time.sleep(min(30, 2 ** (attempt + 1)))
        else:
            raise RuntimeError(f"B站列表获取失败（{body.get('code')} {body.get('message') or ''}）")
        data = body["data"]
        total = data["page"]["count"]
        vlist = data["list"]["vlist"] or []
        out += [{"url": f"https://www.bilibili.com/video/{v['bvid']}", "title": v["title"],
                 "date": time.strftime("%Y-%m-%d", time.localtime(v["created"])) if v.get("created") else None,
                 "duration": clock_seconds(v.get("length"))} for v in vlist]
        if not vlist or pn * 30 >= total:
            break
        pn += 1
        time.sleep(1)
    return {"name": card["card"]["name"], "avatar": card["card"]["face"], "total": total,
            "entries": out[:limit] if limit else out}


def clock_seconds(s):
    """ "1:02:03" / "12:34" -> seconds (None when it isn't one)."""
    try:
        sec = 0
        for part in str(s).split(":"):
            sec = sec * 60 + int(part)
        return sec or None
    except ValueError:
        return None


def entry_date(e):
    """A yt-dlp entry's upload day, YYYY-MM-DD, if the flat list has one."""
    if e.get("upload_date"):
        d = e["upload_date"]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    ts = e.get("timestamp") or e.get("release_timestamp")
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None


def list_ytdlp(url, limit):
    """An uploader's videos through yt-dlp's flat playlist (YouTube, Pornhub), newest first."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            # YouTube's list says "3 weeks ago": turned into an approximate date, for the date filters
            "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
    if limit:
        opts["playlistend"] = limit
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [{"url": re.sub(r"^http://", "https://", e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"),
                "title": e.get("title") or "", "date": entry_date(e), "duration": e.get("duration") or None}
               for e in info.get("entries") or [] if e and (e.get("url") or e.get("id"))]  # (Pornhub's have no id)
    avatar = next((t["url"] for t in info.get("thumbnails") or [] if t.get("id") == "avatar_uncropped"), "")
    return {"name": info.get("channel") or info.get("uploader") or "", "avatar": avatar,
            "total": info.get("playlist_count") or (None if limit else len(entries)), "entries": entries}


def list_channel(sub, limit):
    """The uploader's videos, newest first (at most `limit`; None = all)."""
    if sub["platform"] == "bilibili":
        return list_bilibili(sub["key"].split(":", 1)[1], limit)
    return list_ytdlp(sub["url"], limit)


AVATARS = STATE / "avatars"


def check_sub(sub_id, everything=False):
    """Queue the uploader's videos not seen yet that pass its filter. First check: the newest `backfill` (0 = all)
    of them, looking up to SUB_FILTER_SCAN back when there's a filter; later checks only look at the newest 30;
    everything=True goes through the whole list. After the filter changed (refilter) it's a first check again, and
    queued videos that no longer pass are dropped (they come back if a later filter lets them through).
    Returns how many were queued, or None if the list couldn't be fetched."""
    sub = q("SELECT * FROM subs WHERE id=?", (sub_id,), one=True)
    if not sub:
        return 0
    flt = json.loads(sub["filter"]) if sub["filter"] else None
    first = sub["seen"] in (None, "[]")  # no successful check yet
    wide = first or bool(sub["refilter"])
    limit = (None if everything or (wide and not sub["backfill"]) else
             (max(sub["backfill"], SUB_FILTER_SCAN) if flt else sub["backfill"]) if wide else 30)
    try:
        res = list_channel(sub, limit)
    except Exception as e:
        traceback.print_exc()
        q("UPDATE subs SET checked=?, error=? WHERE id=?", (time.time(), str(e)[:300], sub_id))
        return None
    seen = json.loads(sub["seen"] or "[]")
    passing = [e for e in res["entries"] if matches(flt, e)]
    if wide and not everything and sub["backfill"]:
        passing = passing[:sub["backfill"]]
    if sub["refilter"]:
        drop = {link_key(e["url"]) for e in res["entries"] if not matches(flt, e)}
        for r in q("SELECT id, key FROM jobs WHERE source=? AND status IN ('queued','linked')", (f"sub:{sub_id}",)):
            if r["key"] in drop:
                pipeline.cancel_job(r["id"])
        seen = [k for k in seen if k not in drop]
    seen_set = set(seen)
    new = [e for e in passing if link_key(e["url"]) not in seen_set]
    # oldest first, so the newest video gets the highest id and sits at the top of the page
    for e in reversed(new):
        jid, how = pipeline.add_job_ex(e["url"], source=f"sub:{sub_id}", owner=sub["owner"], device=sub["device"])
        if how == "new" and e["title"]:
            update(jid, title=e["title"])  # the list already has the title: show it while the video waits
        if how == "new" and (wide or everything):
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
    # (a filter changed while this check ran stays marked for the next one)
    q("UPDATE subs SET name=?, avatar=?, total=?, seen=?, checked=?, error='', backfill=?, refilter=0 WHERE id=? AND filter IS ?",
      (res["name"] or sub["name"], avatar, total or len(res["entries"]), json.dumps(seen), time.time(),
       0 if everything else sub["backfill"], sub_id, sub["filter"]))
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
