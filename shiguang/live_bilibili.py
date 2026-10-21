"""B站 live lookup and isolated publishing credentials; no credentials are sent to the browser."""
import hashlib
import json
import os
import re
import requests
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from .core import COOKIES, STATE, UA


TV_KEY = "4409e2ce8ffd12b8"
TV_SECRET = "59b43e04ad6965f34319062b478f83dd"
ACCOUNTS = STATE / "live-accounts"
BILIUP = os.environ.get("LIVE_BILIUP_BIN") or shutil.which("biliup") or str(STATE / "tools" / "biliup")


def account_path(owner):
    return ACCOUNTS / (hashlib.sha256(owner.encode()).hexdigest() + ".json")


def client(owner=None, recording=False):
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://live.bilibili.com/"})
    path = account_path(owner) if owner else None
    if path and path.exists():
        info = json.loads(path.read_text())
        for c in info.get("cookie_info", {}).get("cookies", []):
            session.cookies.set(c["name"], str(c["value"]), domain=".bilibili.com", path="/")
    elif recording and COOKIES.exists():
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(str(COOKIES))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
    return session


def api(session, url, **params):
    response = session.get(url, params=params, timeout=15)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise ValueError(f"B站接口暂不可用（{body.get('code', '未知')}）")
    return body.get("data") or {}


def room_reference(value):
    value = str(value or "").strip()
    if value.isdigit() and 0 < int(value) < 10**12:
        return "room", int(value)
    shared = re.search(r"https?://[^\s<>\"']+", value)
    if shared:
        value = shared[0].rstrip("，。！；）】)")
    elif value.startswith(("b23.tv/", "www.b23.tv/")):
        value = "https://" + value
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("http", "https") or url.username or url.password or url.port not in (None, 80, 443):
        raise ValueError("请粘贴 B站直播间、UP 主页或 b23.tv 分享链接")
    if url.hostname in ("b23.tv", "www.b23.tv") and re.fullmatch(r"/[0-9A-Za-z]{1,128}/?", url.path):
        return "short", value
    if url.hostname == "live.bilibili.com" and re.fullmatch(r"/(?:blanc/|h5/)?\d+/?", url.path):
        return "room", int(url.path.strip("/").split("/")[-1])
    if url.hostname == "space.bilibili.com" and re.fullmatch(r"/\d+(?:/.*)?", url.path):
        return "uid", int(url.path.split("/")[1])
    if url.hostname in ("www.bilibili.com", "m.bilibili.com", "bilibili.com"):
        video = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av\d+)/?", url.path)
        if video:
            return "video", video[1]
    raise ValueError("请粘贴 B站直播间、UP 主页或 b23.tv 分享链接")


def resolve_reference(value):
    """Resolve the public shortener without sending posting cookies or following arbitrary redirects."""
    kind, number = room_reference(value)
    if kind != "short":
        return kind, number
    with requests.Session() as session:
        for _ in range(5):
            response = session.get(number, headers={"User-Agent": UA}, timeout=10, allow_redirects=False, stream=True)
            try:
                if response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get("Location"):
                    raise ValueError("短链接未返回有效的 B站跳转地址，请复制 UP 主页或直播间链接重试")
                target = urllib.parse.urljoin(number, response.headers["Location"])
            finally:
                response.close()
            kind, number = room_reference(target)
            if kind != "short":
                return kind, number
    raise ValueError("短链接跳转次数过多，请复制 UP 主页或直播间链接重试")


def room_info(value, owner=None, lookup_name=False):
    kind, number = resolve_reference(value)
    session = client(owner, recording=True)
    if kind == "video":
        params = {"bvid": number} if number.startswith("BV") else {"aid": number[2:]}
        data = api(session, "https://api.bilibili.com/x/web-interface/view", **params)
        number = int((data.get("owner") or {}).get("mid") or 0)
        if not number:
            raise ValueError("没有识别到这个视频的 UP 主，请复制 UP 主页链接")
        kind = "uid"
    if kind == "uid":
        data = api(session, "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld", mid=number)
        number = int(data.get("roomid") or 0)
        if not number:
            raise ValueError("这个 UP 主尚未开通直播间")
    data = api(session, "https://api.live.bilibili.com/room/v1/Room/get_info", room_id=number)
    room = int(data.get("room_id") or number)
    uid = int(data.get("uid") or 0)
    name = str(data.get("uname") or "")
    if lookup_name and not name and uid:
        try:
            anchor = api(session, "https://api.live.bilibili.com/live_user/v1/Master/info", uid=uid)
            name = str((anchor.get("info") or {}).get("uname") or "")
        except (ValueError, requests.RequestException):
            pass  # Room tracking still works when the optional profile lookup is unavailable.
    return {"room_id": room, "url": f"https://live.bilibili.com/{room}",
            "title": str(data.get("title") or "直播"), "live": int(data.get("live_status") or 0) == 1,
            "uid": uid, "name": name, "live_start": str(data.get('live_time') or '')}


def stream_url(room, owner):
    data = api(client(owner, recording=True), "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
               room_id=room, protocol="0,1", format="0,1", codec="0", qn=10000, platform="web", ptype=8)
    streams = ((data.get("playurl_info") or {}).get("playurl") or {}).get("stream") or []
    urls = []
    for stream in streams:
        for fmt in stream.get("format", []):
            for codec in fmt.get("codec", []):
                if codec.get("codec_name") not in ("avc", "h264", None):
                    continue
                for entry in codec.get("url_info", []):
                    url = str(entry.get("host") or "") + str(codec.get("base_url") or "") + str(entry.get("extra") or "")
                    host = urllib.parse.urlsplit(url).hostname or ""
                    if (urllib.parse.urlsplit(url).scheme in ("http", "https")
                            and any(host == d or host.endswith("." + d) for d in ("bilivideo.com", "bilivideo.cn", "biliapi.net"))):
                        urls.append((fmt.get("format_name") != "ts", url))
    if not urls:
        raise ValueError("直播没有返回可用的 H.264 直播流，请检查登录或直播状态")
    return sorted(urls)[0][1]


def tv_request(action, **params):
    values = {"appkey": TV_KEY, "local_id": "0", "ts": str(int(time.time())), **params}
    encoded = urllib.parse.urlencode(sorted(values.items()))
    values["sign"] = hashlib.md5((encoded + TV_SECRET).encode()).hexdigest()
    response = requests.post("https://passport.bilibili.com/x/passport-tv-login/qrcode/" + action,
                             data=values, headers={"User-Agent": UA}, timeout=15)
    response.raise_for_status()
    return response.json()


def save_account(owner, info):
    cookies = (info.get("cookie_info") or {}).get("cookies") or []
    if not {"SESSDATA", "bili_jct", "DedeUserID"} <= {c.get("name") for c in cookies}:
        raise ValueError("B站返回的登录信息不完整，请重新扫码")
    token = info.get("token_info") or {}
    if not token.get("access_token") or not token.get("mid"):
        raise ValueError("B站没有返回投稿凭据，请重新扫码")
    path = account_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp-" + os.urandom(6).hex())
    try:
        with open(temp, "x", opener=lambda p, f: os.open(p, f, 0o600)) as file:
            json.dump({**info, "platform": "BiliTV"}, file)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return str(token["mid"])


def ready():
    return Path(BILIUP).is_file() and os.access(BILIUP, os.X_OK)


def original_source(clip, source):
    origin = json.loads(dict(clip).get('origin') or '{}')
    return {**dict(source), **{k: origin[k] for k in ('name', 'room_id', 'url', 'uid') if origin.get(k)}}


def title_name(source):
    config = source.get('settings') or '{}'
    config = json.loads(config) if isinstance(config, str) else config
    return str(config.get('title_name') or source['name']).strip()


def publication_title(clip, source):
    source = original_source(clip, source)
    title = ' '.join(str(clip['title']).split())
    name = title_name(source)
    if any(n and n in title[:80] for n in (name, source['name'])):
        return title[:80]
    prefix = name + '：'
    if len(prefix) >= 80:
        raise ValueError('主播名称过长，请在追踪设置中填写标题称呼')
    return prefix + title[:80-len(prefix)]


def publication_description(clip, source):
    """Reserve space for attribution even after the editor replaces the summary."""
    source = original_source(clip, source)
    room_url = f"https://live.bilibili.com/{int(source['room_id'])}"
    attribution = f"原主播：{source['name']}\n原直播间：{room_url}"
    marker = f"拾光切片 #{clip['id']}-v{clip['version']}"
    summary = str(clip['description'] or '').strip()
    # The persisted summary stays editable; attribution is always appended at submission.
    return f"{summary[:max(0, 1800-len(attribution)-len(marker)-3)]}\n\n{attribution}\n{marker}".strip()


def publish(clip, source, beat):
    """One invocation only. An ambiguous failure must be reconciled, never automatically submitted again."""
    source = original_source(clip, source)
    if not ready():
        raise ValueError("投稿组件尚未安装")
    credentials = account_path(clip["owner"])
    if not credentials.exists():
        raise ValueError("请先扫码绑定投稿账号")
    info = api(client(clip["owner"]), "https://api.bilibili.com/x/web-interface/nav")
    if not info.get("isLogin"):
        raise ValueError("投稿账号登录已失效，请重新扫码")
    account = json.loads(credentials.read_text())
    if str(info.get("mid")) != str(account["token_info"]["mid"]):
        raise ValueError("投稿账号不匹配，请重新扫码")
    settings = json.loads(source["settings"] or "{}")
    description = publication_description(clip, source)
    from .live_cover import ensure_cover
    cover = ensure_cover(clip, source)
    tags = json.loads(clip["tags"] or "[]") or ["直播切片", source["name"][:20]]
    command = [BILIUP, "-u", str(credentials), "upload", "--submit", "web", "--copyright", "2",
               "--source", f"https://live.bilibili.com/{int(source['room_id'])}", "--tid", str(settings.get("tid", 138)),
               "--title", publication_title(clip, source), "--cover", str(cover), "--desc", description, "--tag", ",".join(tags[:10]),
               "--limit", "2", clip["file"]]
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log, cwd=str(credentials.parent))
        started = time.time()
        try:
            while proc.poll() is None:
                if time.time() - started > 1800 or not beat({"phase": "uploading"}):
                    raise TimeoutError("投稿结果需要核实")
                time.sleep(3)
            log.seek(max(0, log.tell() - 200_000))
            output = log.read().decode(errors="replace")
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            raise
    match = re.search(r'["\']bvid["\']\s*:\s*["\'](BV[0-9A-Za-z]{10})', output)
    aid = re.search(r'["\']aid["\']\s*:\s*(\d+)', output)
    if proc.returncode:
        # The CLI may have submitted before losing its response. Do not expose its credential-bearing logs.
        raise RuntimeError("投稿未返回成功结果，请先去创作中心核实，再决定是否重试")
    return {"bvid": match[1] if match else "", "aid": aid[1] if aid else ""}
