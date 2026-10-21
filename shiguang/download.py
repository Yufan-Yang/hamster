"""Getting the file: yt-dlp, a headless browser for pages with an embedded player, aria2 for files and torrents."""
import errno
import http.cookiejar
import json
import math
import os
import re
import requests
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from .core import (CHROMIUM, COOKIES, Cancelled, FORMAT_SORT, STATE, UA, VIDEO_EXT, AUDIO_EXT, cancel_requested, check_cancel, heavy_slot, human, safe_name, update)


# ---------------------------------------------------------------- download: yt-dlp

def ytdlp_opts(job_id, workdir, extra=None):
    last = [0.0]
    phase = (extra or {}).get("_stage")

    def hook(d):
        check_cancel(job_id)
        # yt-dlp reports many times a second; the page polls every few seconds. Every write lands on the SD card
        if d["status"] == "downloading" and time.time() - last[0] >= 3:
            last[0] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            fragments = d.get("fragment_count") or 0
            pct = done * 100 / total if total else (d.get("fragment_index") or 0) * 100 / fragments if fragments else 0
            spd = d.get("speed") or 0
            update(job_id, progress=round(min(100, max(0, pct)), 1), speed=f"{human(spd)}/s" if spd else "",
                   **({"stage": phase} if phase else {}))

    def pp_hook(d):
        # After 100% yt-dlp may still spend minutes in ffmpeg (a big HLS video: many minutes on the Pi):
        # show that it's merging / repairing the file instead of sitting at 100%
        if d["status"] != "started":
            return
        name = d.get("postprocessor") or ""
        stage = "merging video and audio" if name == "Merger" else "fixing up the file" if name.startswith("Fixup") else None
        if stage:
            update(job_id, stage=stage, progress=0, speed="")

    opts = {
        "outtmpl": str(workdir / "%(title).150B [%(id)s].%(ext)s"),
        "format_sort": FORMAT_SORT.split(","),
        "merge_output_format": "mp4/mkv",
        "writesubtitles": True, "writeautomaticsub": True,
        # Only original-language tracks; "en.*" would also pull every auto-translation and trip YouTube's rate limit
        # ai-zh / ai-en: B站's own AI subtitles (only offered with a logged-in cookies.txt); saves speech-to-text
        "subtitleslangs": ["en", "en-orig", "en-US", "en-GB", "zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh", "ja", ".*-orig",
                           "ai-zh", "ai-en"],
        "subtitlesformat": "srt/best",
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "sleep_interval_subtitles": 2,  # many subtitle requests in a row trip YouTube's 429
        "retries": 10, "fragment_retries": 20, "file_access_retries": 5, "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "ignoreerrors": False,
        "postprocessors": [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    if extra:
        opts.update({key: value for key, value in extra.items() if key != "_stage"})
    return opts


# YouTube sometimes answers one proxy exit IP with "Sign in to confirm you're not a bot" (or 429) while
# the other exit still works. Xray has a SOCKS inbound per exit (127.0.0.1:10820 → proxy-a, 10821 → proxy-b),
# so on that error yt-dlp tries again through each one directly.
BOT_CHECK = re.compile(r"confirm you.?re not a bot|HTTP Error 429|Too Many Requests", re.I)
EXITS = os.environ.get("YTDLP_EXITS", "socks5h://127.0.0.1:10821 socks5h://127.0.0.1:10820").replace(",", " ").split()


class DownloadFailure(RuntimeError):
    """An actionable failure; only temporary network failures get an automatic retry."""
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class UnsupportedStream(DownloadFailure):
    """This ffmpeg build cannot read this stream; another downloader may still work."""


def terminal_download_reason(error):
    text = str(error)
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT):
        return "存储空间不足，请清理下载磁盘后重试。"
    if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return "下载目录不可写，请修复目录权限或只读磁盘后重试。"
    for pattern, reason in (
        (r"no space left|disk quota|磁盘空间不足", "存储空间不足，请清理下载磁盘后重试。"),
        (r"permission denied|read-only file system|operation not permitted", "下载目录不可写，请修复目录权限或只读磁盘后重试。"),
        (r"\bDRM\b|widevine|playready|SAMPLE-AES|ContentProtection", "视频受 DRM 保护，通用下载器无法处理，请使用网站提供的官方下载方式。"),
        (r"login required|log.?in to|sign.?in to|authentication required|private video|members.only|premium.only|paywall|(?:HTTP Error 401|401 Client Error)",
         "网站要求登录或访问权限，请确认账号权限并更新合法登录凭据后重试。"),
        (r"Cloudflare 人机验证", CF_BLOCKED),
    ):
        if re.search(pattern, text, re.I) and not (BOT_CHECK.search(text) and "sign" in pattern):
            return reason
    return None


def can_fallback(error):
    if isinstance(error, Cancelled) or terminal_download_reason(error):
        return False
    return bool(re.search(r"unsupported|unable to (?:extract|download)|failed to download|no video|"
                          r"requested format|no formats|extractor|HTTP (?:Error|error|status)|"
                          r"timed? out|timeout|connection|remote end closed|fragment|invalid data|"
                          r"server returned|produced no files|机器人", str(error), re.I))


def failure_detail(error):
    # A signed media URL or proxy password must not end up in the job's error card.
    text = re.sub(r"https?://[^\s<>\"']+|socks5h?://[^\s<>\"']+", "[链接]", str(error))
    return " ".join(text.split())[-240:]


def ytdlp_probe(url, extra=None):
    """Return yt-dlp info if it can download this URL itself, else None. info["_net"] holds the proxy
    setting that worked, for the download. Raises when the site blocks us as a bot through every exit."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    if extra:
        opts.update(extra)
    info, err = None, None
    for net in [{}] + [{"proxy": p} for p in EXITS]:
        try:
            with yt_dlp.YoutubeDL({**opts, **net}) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as e:
            if isinstance(e, Cancelled):
                raise
            if reason := terminal_download_reason(e):
                raise DownloadFailure(reason) from None
            err = e
            if not BOT_CHECK.search(str(e)):
                return None  # not something yt-dlp handles: try the page sniffer
    if info is None and err is not None:
        raise RuntimeError(f"网站把下载当成了机器人（每个代理出口都试过了）：{str(err)[:300]}")
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
    elif not (info.get("formats") or info.get("url")):
        return None
    info["_net"] = net
    return info


def ytdlp_download(job_id, url, workdir, extra=None):
    """Download with yt-dlp; when the site takes us for a bot, try again through the other proxy exits."""
    nets = [extra or {}] + [{**(extra or {}), "proxy": p} for p in EXITS if p != (extra or {}).get("proxy")]
    for i, net in enumerate(nets):
        try:
            return _ytdlp_download(job_id, url, workdir, net, drop_subs_on_error=i == len(nets) - 1)
        except Exception as e:
            check_cancel(job_id)
            if isinstance(e, Cancelled):
                raise
            if i == len(nets) - 1 or not BOT_CHECK.search(str(e)) or "youtube" not in url and "youtu.be" not in url:
                raise


def _ytdlp_download(job_id, url, workdir, extra=None, drop_subs_on_error=True):
    import yt_dlp
    update(job_id, stage="downloading video")
    try:
        with yt_dlp.YoutubeDL(ytdlp_opts(job_id, workdir, extra)) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "subtitles" not in str(e):
            raise
        if BOT_CHECK.search(str(e)) and not drop_subs_on_error:
            raise  # subtitles rate-limited (429): try the next proxy exit before going without them (= speech-to-text)
        # Subtitles are nice to have; don't fail the download over them
        with yt_dlp.YoutubeDL(ytdlp_opts(job_id, workdir, {**(extra or {}), "writesubtitles": False,
                                                             "writeautomaticsub": False})) as ydl:
            info = ydl.extract_info(url, download=True)
    if info.get("_type") == "playlist":
        info = next(e for e in info["entries"] if e)
    meta = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "upload_date", "duration", "description",
                                     "extractor_key", "webpage_url", "tags", "categories", "view_count",
                                     "artist", "track", "album", "series", "season_number", "episode_number")}
    if meta.get("description") and len(meta["description"]) > 5000:
        meta["description"] = meta["description"][:5000] + " …[description cut at 5000 chars]"
    meta["_timeline"] = timeline(info, url)
    return meta


# ---------------------------------------------------------------- what the site shows along the progress bar

def timeline(info, url):
    """Marks and heat along the video as its site shows them: chapters (YouTube's from the description), the most
    replayed parts (YouTube's heatmap), Pornhub's action tags ("Doggystyle" at 11:54) and how much each 5 seconds is
    watched there. {"markers": [{"t", "title"}], "heat": {"step": seconds, "values": [0-100...]}}; parts missing
    when the site has none."""
    out = {}
    chapters = [{"t": round(float(c.get("start_time") or 0), 1), "title": str(c.get("title") or "")[:40]}
                for c in info.get("chapters") or [] if c.get("title")]
    if chapters:
        out["markers"] = chapters
    heat = info.get("heatmap") or []
    if heat and heat[0].get("end_time"):
        step = float(heat[0]["end_time"]) - float(heat[0].get("start_time") or 0)
        out["heat"] = {"step": round(step, 2), "values": _scale([float(h.get("value") or 0) for h in heat])}
    if re.search(r"(^|\.)pornhub\.(com|org)", urllib.parse.urlsplit(url).netloc):
        try:
            out.update(pornhub_timeline(url))
        except Exception as e:  # nice to have
            print("pornhub timeline:", e, flush=True)
    return out


def _scale(values):
    top = max(values) if values else 0
    return [round(v / top * 100) for v in values] if top > 0 else []


def pornhub_timeline(url):
    """Pornhub's player data (flashvars): actionTags "Doggystyle:714,Cowgirl:300" (name:second) and hotspots (views
    of each 5 seconds). yt-dlp doesn't read either."""
    html = requests.get(url, timeout=30, headers={"User-Agent": UA},
                        cookies={"accessAgeDisclaimerPH": "1", "age_verified": "1", "platform": "pc"}).text
    out = {}
    tags = re.search(r'"actionTags"\s*:\s*"([^"]*)"', html)
    marks = []
    for part in (tags.group(1).split(",") if tags else []):
        name, _, sec = part.rpartition(":")
        if name.strip() and sec.strip().isdigit():
            marks.append({"t": float(sec), "title": name.strip()[:40]})
    if marks:
        out["markers"] = sorted(marks, key=lambda m: m["t"])
    spots = re.search(r'"hotspots"\s*:\s*\[([0-9,\s"]*)\]', html)
    values = [float(v.strip().strip('"')) for v in spots.group(1).split(",") if v.strip().strip('"')] if spots else []
    if len(values) > 3:
        out["heat"] = {"step": 5, "values": _scale(values)}
    return out


# ---------------------------------------------------------------- download: embedded player sniffing

PLAYER_CONFIG = re.compile(r'"video"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"')  # DPlayer-style configs
MEDIA_IN_PAGE = re.compile(r'(?:https?:)?[\w:/.\-%]*?/[\w/.\-%]+\.(?:m3u8|mpd|mp4)(?:\?[^"\'\s<>\\]*)?')


def media_in_page(html_text, base):
    """Media URLs written into the page (player configs, inline JSON), for players that only fetch the
    stream after an ad or a click. Returns (player config URLs, other URLs), in page order."""
    import html as htmllib
    text = htmllib.unescape(html_text).replace("\\/", "/")
    def norm(u):
        return urllib.parse.urljoin(base, u.replace("\\u0026", "&").replace("&amp;", "&"))
    players = [norm(u) for u in PLAYER_CONFIG.findall(text)]
    others = [norm(u) for u in MEDIA_IN_PAGE.findall(text)]
    return players, others


def lazy_dplayer_media(html_text, base, session):
    """Resolve the public play-url API used by this DPlayer placeholder plugin."""
    if '/usr/plugins/DPlayer/assets/player.js' not in html_text:
        return []

    class Players(HTMLParser):
        def __init__(self):
            super().__init__()
            self.players = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            video = attrs.get('data-video-id') or ''
            index = attrs.get('data-player-index') or '0'
            if ('dplayer' in (attrs.get('class') or '').split() and
                    re.fullmatch(r'[0-9]+', video) and re.fullmatch(r'[0-9]+', index)):
                self.players.append((video, index))

    parser = Players()
    parser.feed(html_text)
    media = []
    for video, index in list(dict.fromkeys(parser.players))[:10]:
        endpoint = urllib.parse.urljoin(base, f'/play-url/{video}.json?i={index}')
        try:
            response = session.get(endpoint, headers={'Referer': base, 'User-Agent': UA}, timeout=15)
            response.raise_for_status()
            data = response.json()
            data = data.get('data') if isinstance(data, dict) and data.get('code') == 0 else None
            url = data.get('url') if isinstance(data, dict) else None
            if isinstance(url, str) and url.strip():
                url = urllib.parse.urljoin(response.url, url)
                if urllib.parse.urlsplit(url).scheme in ('http', 'https'):
                    media.append(url)
        except (requests.RequestException, ValueError):
            continue
    return dedupe_media(media)


AD_HOSTS = re.compile(r"doubleclick|googlesyndication|googleads|adservice|adsystem|imasdk|moatads|criteo|taboola")
MEDIA_SEGMENT = re.compile(r"(?:^|[/_.-])(?:init|segment|chunk|frag)(?:[-_.]?\d*|[-_.][^/]*)\.(?:mp4|m4s|ts)$", re.I)


def usable_media_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not parsed.username and not parsed.password and not re.search(r"[\r\n]", url)
                and not AD_HOSTS.search(parsed.hostname)
                and not re.search(r"(?:^|/)(?:ads?|advert|preroll)(?:/|[_.-])", parsed.path, re.I)
                and not parsed.path.lower().endswith((".ts", ".m4s", ".aac"))
                and not MEDIA_SEGMENT.search(parsed.path))
    except ValueError:
        return False


def dedupe_media(urls):
    seen, out = set(), []
    for u in urls:
        key = urllib.parse.urlsplit(u)._replace(query="", fragment="").geturl()
        if key not in seen and usable_media_url(u):
            seen.add(key)
            out.append(u)
    return out


def write_cookie_file(path, cookies):
    """cookies: [{"domain", "path", "secure", "expires", "name", "value"}] -> Netscape cookies.txt for yt-dlp"""
    with open(path, "w") as f:
        os.chmod(path, 0o600)
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            exp = c.get("expires") or 0
            f.write("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"] or "/",
                               "TRUE" if c["secure"] else "FALSE", str(int(exp)) if exp and exp > 0 else "",
                               c["name"], c["value"] or ""]) + "\n")


CF_BLOCKED = ("这个网站开了 Cloudflare 人机验证，拾光的下载器和浏览器都过不去（自己的浏览器里能打开，是因为验证认得真人）。"
              "这个网站暂时下不了，重试也没用。")
CF_TITLE = re.compile(r"^(Just a moment|Attention Required|请稍候|請稍候)", re.I)


def activate_player_placeholders(page):
    """Click visible player controls, scrolling lazy players into view first."""
    clicked = False
    for frame in page.frames:
        try:
            controls = frame.query_selector_all(
                '.dplayer[data-video-id], .dplayer-start, .vjs-big-play-button, .jw-icon-display')
            for control in controls[:10]:
                try:
                    if control.is_visible():
                        control.click(timeout=3000)
                        clicked = True
                except Exception:
                    pass
        except Exception:
            pass
    return clicked


def sniff_page(job_id, url):
    """Find the video on a page yt-dlp doesn't know. Fast path: the player config in the page's HTML
    (no browser). Otherwise open it in headless Chromium, start playback and catch the media requests.
    A page stuck behind Cloudflare's bot challenge raises CF_BLOCKED instead of finding nothing."""
    check_cancel(job_id)
    update(job_id, stage="识别页面中的公开播放器", progress=0, speed="")
    challenged = False
    try:
        with requests.Session() as session:
            r = session.get(url, headers={"User-Agent": UA}, timeout=30)
            check_cancel(job_id)
            challenged = r.headers.get("cf-mitigated") == "challenge"
            players, inline = media_in_page(r.text, r.url)
            explicit = dedupe_media(players) or lazy_dplayer_media(r.text, r.url, session)
            # A manifest in inline player code is enough; arbitrary MP4s may be thumbnails/ads.
            media = explicit or dedupe_media([u for u in inline if re.search(r"\.(m3u8|mpd)(\?|$)", u)])
            if challenged:
                media = []
            if media:
                import html as htmllib
                m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
                title = htmllib.unescape(m.group(1)).strip() if m else ""
                cookie_file = STATE / f"cookies-{job_id}.txt"
                write_cookie_file(cookie_file, [{"domain": c.domain, "path": c.path, "secure": c.secure,
                                                 "expires": c.expires, "name": c.name, "value": c.value} for c in session.cookies])
                return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file),
                        "media_groups": [[u] for u in explicit[:10]] if explicit else [media[:3]]}
    except requests.RequestException:
        pass
    check_cancel(job_id)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise DownloadFailure(CF_BLOCKED if challenged else "需要浏览器识别播放器，但未安装 Playwright；请安装下载服务的浏览器依赖。") from None
    found = {}  # url -> (rank, size)

    def on_response(r):
        u, ct = r.url, (r.headers.get("content-type") or "").lower()
        if not usable_media_url(u) or r.status >= 400:
            return
        if re.search(r"\.(m3u8|mpd)(\?|$)", u) or "mpegurl" in ct or "dash+xml" in ct:
            rank = 0 if re.search(r"master|playlist|index", u) else 1
            found.setdefault(u, (rank, 0))
        elif ct.startswith("video/") or re.search(r"\.(mp4|webm|mkv|mov|flv)(\?|$)", u):
            size = int(r.headers.get("content-length") or 0) if str(r.headers.get("content-length") or "0").isdigit() else 0
            if ct.startswith("video/mp2t") or u.split("?")[0].endswith(".ts"):
                return  # HLS segments; the playlist is what we want
            found[u] = (2, max(size, found.get(u, (2, 0))[1]))

    with heavy_slot("browser", job_id), sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=True,
                                    args=["--no-sandbox", "--mute-audio", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.set_default_timeout(20000)  # nothing in here may hang the job
        page.on("dialog", lambda d: d.dismiss())  # alert()/confirm() would block every evaluate
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        check_cancel(job_id)
        play_js = "document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })"
        for _ in range(2):
            check_cancel(job_id)
            for frame in page.frames:
                try:
                    frame.evaluate(play_js)
                except Exception:
                    pass
            # Many players only load the stream after a click on the player
            try:
                if not activate_player_placeholders(page):
                    player = None
                    area = 0
                    for v in page.query_selector_all("video, iframe"):
                        b = v.bounding_box()
                        if b and b["width"] > 200 and b["width"] * b["height"] > area:
                            player, area = v, b["width"] * b["height"]
                    if player:
                        player.click(timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(6000)
            check_cancel(job_id)
            if found:
                break
        for frame in page.frames:
            try:
                for src in frame.evaluate("Array.from(document.querySelectorAll('video, video source'))"
                                          ".map(v => v.currentSrc || v.src).filter(s => s && !s.startsWith('blob:'))"):
                    found.setdefault(urllib.parse.urljoin(frame.url, src), (2, 0))
            except Exception:
                pass
        players, in_page = [], []
        # Search the rendered page inside the browser: some pages grow to tens of MB once their scripts run,
        # and copying all of that out to Python takes minutes on the Pi
        find_js = r"""() => {
            const t = document.documentElement.outerHTML;
            const re = /"video"\s*:\s*\{\s*"url"\s*:\s*"[^"]+"|(?:https?:)?[\w:\/.\-%]*?\/[\w\/.\-%]+\.(?:m3u8|mpd|mp4)(?:\?[^"'\s<>\\]*)?/g;
            return (t.match(re) || []).slice(0, 200).join("\n");
        }"""
        for frame in page.frames:
            try:
                p_urls, o_urls = media_in_page(frame.evaluate(find_js), frame.url)
                players += p_urls
                in_page += o_urls
            except Exception:
                pass
        title = page.title()
        challenged = bool(CF_TITLE.match(title.strip())) or (challenged and not found)
        cookies = ctx.cookies()
        browser.close()

    # A page can hold several videos (one player each). Player configs are the most reliable source;
    # otherwise take the best stream the browser actually requested, then anything else in the page.
    explicit = dedupe_media(players)
    media = explicit
    if not media:
        ranked = [u for u, _ in sorted(found.items(), key=lambda kv: (kv[1][0], -kv[1][1]))]
        manifests = [u for u in in_page if re.search(r"\.(m3u8|mpd)(\?|$)", u)]
        media = dedupe_media(ranked + manifests + in_page)[:3]
    if not media:
        if challenged:
            raise RuntimeError(CF_BLOCKED)
        return None
    cookie_file = STATE / f"cookies-{job_id}.txt"
    write_cookie_file(cookie_file, cookies)
    return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file),
            "media_groups": [[u] for u in explicit[:10]] if explicit else [media[:3]]}


# ---------------------------------------------------------------- bounded download fallback

def stream_kind(url):
    path = urllib.parse.urlsplit(url).path.lower()
    return next((ext for ext in ("mp4", "m3u8", "mpd") if path.endswith("." + ext)), None)


@lru_cache(maxsize=1)
def ffmpeg_demuxers():
    result = subprocess.run(["ffmpeg", "-hide_banner", "-demuxers"], capture_output=True, text=True, timeout=10, check=True)
    return frozenset(name for line in re.findall(r"^\s*D\s+(\S+)", result.stdout, re.M) for name in line.split(","))


def media_cookies(cookiefile, url):
    """Restrict browser cookies before handing them to another HTTP client."""
    jar = http.cookiejar.MozillaCookieJar()
    if cookiefile and Path(cookiefile).is_file():
        jar.load(cookiefile, ignore_discard=True, ignore_expires=True)
    parsed = urllib.parse.urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path or "/"
    selected = []
    for c in jar:
        domain = c.domain.lstrip(".").lower()
        domain_ok = host == domain or (c.domain_initial_dot and host.endswith("." + domain))
        cookie_path = c.path or "/"
        path_ok = path == cookie_path or (path.startswith(cookie_path) and
                  (cookie_path.endswith("/") or path[len(cookie_path):].startswith("/")))
        if (domain_ok and path_ok and (not c.secure or parsed.scheme == "https")
                and (not c.expires or c.expires > time.time())
                and not any(ch in c.name + c.value + c.domain + cookie_path for ch in "\r\n;")):
            selected.append(c)
    return selected


def ffmpeg_cookies(cookiefile, url):
    parsed = urllib.parse.urlsplit(url)
    # Older libavformat matches against the HTTP Host header, including a non-default
    # port. Keep the standard domain entry and a narrowly scoped compatibility entry;
    # never use an unscoped Cookie header (which would be forwarded to other hosts).
    domains = [parsed.hostname]
    if parsed.port and parsed.port != (443 if parsed.scheme == "https" else 80):
        domains.append(parsed.netloc)
    return "\n".join(f"{c.name}={c.value}; path={c.path or '/'}; domain={domain};" +
                     (" secure;" if c.secure else "") for c in media_cookies(cookiefile, url) for domain in domains)


def check_manifest(url, referer, cookiefile):
    """Inspect only a bounded text manifest; don't attempt DRM or endless live recording."""
    if stream_kind(url) not in ("m3u8", "mpd"):
        return
    jar = requests.cookies.RequestsCookieJar()
    for c in media_cookies(cookiefile, url):
        jar.set_cookie(c)
    with requests.get(url, headers={"Referer": referer, "User-Agent": UA}, cookies=jar,
                      timeout=20, stream=True) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise DownloadFailure("播放清单过大，通用下载器无法可靠解析，请提供该视频的官方下载链接。")
        text = content.decode("utf-8", "replace")
        if reason := terminal_download_reason(text):
            raise DownloadFailure(reason)
        if (stream_kind(url) == "m3u8" and "#EXTINF:" in text and "#EXT-X-ENDLIST" not in text
                or stream_kind(url) == "mpd" and re.search(r'\btype=["\']dynamic["\']', text)):
            raise DownloadFailure("识别到尚未结束的直播流，请使用直播录制入口，或等待回放生成后重试。")


def ffmpeg_download(job_id, url, workdir, referer, cookiefile=None, timeout=2700):
    """Download one public MP4/HLS/DASH stream without yt-dlp; validate before exposing it."""
    check_cancel(job_id)
    if not usable_media_url(url) or not stream_kind(url):
        raise DownloadFailure("独立下载仅支持公开的 MP4、M3U8 或 MPD 链接。")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise DownloadFailure("缺少 ffmpeg 或 ffprobe，请安装下载服务的媒体依赖。")
    check_manifest(url, referer, cookiefile)
    check_cancel(job_id)
    if stream_kind(url) == "mpd" and "dash" not in ffmpeg_demuxers():
        raise UnsupportedStream("当前 FFmpeg 未启用 DASH（MPD）读取支持；请安装带 DASH/libxml2 支持的 FFmpeg。")
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "video.part.mkv"
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-xerror",
               "-rw_timeout", "30000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
               "-user_agent", UA, "-referer", referer]
    cookies = ffmpeg_cookies(cookiefile, url)
    if cookies:
        command += ["-cookies", cookies]
    command += ["-i", url, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "matroska", str(output)]
    proc = None
    started, last = time.monotonic(), 0
    last_size, smoothed_speed = 0, 0.0
    try:
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            while proc.poll() is None:
                check_cancel(job_id)
                now = time.monotonic()
                if now - started > timeout:
                    raise DownloadFailure("独立流下载超过 45 分钟，已停止；请检查网络或改用网站的官方下载链接。", retryable=True)
                if now - last >= 3:
                    size = output.stat().st_size if output.exists() else 0
                    rate = max(0, size - last_size) / max(.01, now - (last or started))
                    smoothed_speed = .35 * rate + .65 * smoothed_speed if smoothed_speed else rate
                    update(job_id, stage=f"独立下载视频流 · 已接收 {human(size)}", progress=0,
                           speed=f"{human(smoothed_speed)}/s" if smoothed_speed else "等待视频分段")
                    last = now
                    last_size = size
                time.sleep(0.25)
            check_cancel(job_id)
            if proc.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4096))
                detail = errors.read().decode("utf-8", "replace")
                if reason := terminal_download_reason(detail):
                    raise DownloadFailure(reason)
                raise RuntimeError("独立流下载失败：" + failure_detail(detail))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                               capture_output=True, text=True, timeout=30, check=True)
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration") or 0)
        if (not output.is_file() or output.stat().st_size == 0 or not math.isfinite(duration) or duration <= 0
                or not any(s.get("codec_type") == "video" for s in info.get("streams", []))):
            raise RuntimeError("独立下载产生的文件没有可验证的视频画面或时长")
        check_cancel(job_id)
        output.rename(workdir / "video.mkv")
        return {"duration": duration, "source": "sniffed-ffmpeg"}
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        output.unlink(missing_ok=True)


def fast_stream_download(job_id, url, workdir, referer, cookiefile=None):
    """Read HLS/DASH fragments concurrently; retain FFmpeg for unsupported streams."""
    if stream_kind(url) in ("m3u8", "mpd"):
        check_cancel(job_id)
        check_manifest(url, referer, cookiefile)
        native = workdir / ".fragments"
        native.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        def deadline(_progress):
            check_cancel(job_id)
            if time.monotonic() - started > 2700:
                raise DownloadFailure("备用视频流下载超过 45 分钟，已停止，请检查网络后重试。", retryable=True)

        extra = {"cookiefile": cookiefile, "http_headers": {"Referer": referer, "User-Agent": UA},
                 "outtmpl": str(native / "video.%(ext)s"), "concurrent_fragment_downloads": 8,
                 "external_downloader": {"m3u8": "native", "dash": "native"},
                 "skip_unavailable_fragments": False, "socket_timeout": 20,
                 "retries": 2, "fragment_retries": 2, "file_access_retries": 1,
                 "writesubtitles": False, "writeautomaticsub": False, "writethumbnail": False,
                 "postprocessors": [], "_stage": "并发下载备用视频流（8 路）"}
        try:
            update(job_id, stage=extra["_stage"], progress=0, speed="")
            # Retain the standard byte/speed hook and add the overall/cancellation limit.
            extra["progress_hooks"] = ytdlp_opts(job_id, native, extra)["progress_hooks"] + [deadline]
            meta = ytdlp_download(job_id, url, native, extra)
            check_cancel(job_id)
            files = [p for p in native.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT
                     and ".part." not in p.name]
            if len(files) != 1:
                raise RuntimeError("并发下载未产生完整的视频文件")
            files[0].replace(workdir / ("video" + files[0].suffix))
            return {**meta, "source": "sniffed-parallel"}
        except Exception as error:
            check_cancel(job_id)
            if isinstance(error, Cancelled):
                raise
            if reason := terminal_download_reason(error):
                raise DownloadFailure(reason) from None
            if isinstance(error, DownloadFailure):
                raise
            update(job_id, stage="分段下载器不兼容，切换独立视频流下载", progress=0, speed="")
        finally:
            shutil.rmtree(native, ignore_errors=True)
    return ffmpeg_download(job_id, url, workdir, referer, cookiefile)


def page_video_download(job_id, url, workdir, info=None, primary_error=None):
    """Return (metadata, successful directory), never mixing failed attempts into the library."""
    errors = [("网站下载器", primary_error)] if primary_error else []
    primary = workdir / ".ytdlp"
    if info:
        if not primary.exists():
            primary.mkdir()
            # Preserve resumes made by older releases, while isolating them from fallback outputs.
            for old in workdir.iterdir():
                if not old.name.startswith("."):
                    shutil.move(str(old), str(primary / old.name))
        try:
            check_cancel(job_id)
            meta = ytdlp_download(job_id, url, primary, info.get("_net"))
            if not any(p.is_file() and p.suffix.lower() in VIDEO_EXT | AUDIO_EXT and ".part." not in p.name
                       for p in primary.rglob("*")):
                raise RuntimeError("Download finished but produced no files")
            meta["source"] = "ytdlp"
            return meta, primary
        except Exception as error:
            check_cancel(job_id)
            if isinstance(error, Cancelled):
                raise
            if reason := terminal_download_reason(error):
                raise DownloadFailure(reason) from None
            if not can_fallback(error):
                raise
            errors.append(("网站下载器", error))
    check_cancel(job_id)
    update(job_id, stage="网站下载失败，正在识别备用视频流" if errors else "识别页面中的公开播放器", progress=0, speed="")
    found = None
    fallback = workdir / ".fallback"
    try:
        found = ({"media_urls": [url], "title": (info or {}).get("title") or "视频", "cookiefile": None}
                 if usable_media_url(url) and stream_kind(url) else sniff_page(job_id, url))
        check_cancel(job_id)
        if not found:
            raise DownloadFailure("未找到公开可下载的视频。请确认链接能直接播放，或提供视频详情页/官方下载链接。")
        title = found.get("title") or (info or {}).get("title") or "视频"
        update(job_id, title=title)
        shutil.rmtree(fallback, ignore_errors=True)
        fallback.mkdir()
        results = []
        groups = found.get("media_groups") or [found.get("media_urls", [])]
        for group_index, group in enumerate(groups[:10], 1):
            candidates = dedupe_media(group)[:3]
            for candidate_index, media_url in enumerate(candidates, 1):
                check_cancel(job_id)
                attempt = fallback / f"{group_index}-{candidate_index}"
                attempt.mkdir()
                update(job_id, stage=f"尝试视频 {group_index}/{len(groups[:10])} 的备用流 {candidate_index}/{len(candidates)}",
                       progress=0, speed="")
                try:
                    independent = bool(stream_kind(media_url))
                    if independent:
                        try:
                            meta = fast_stream_download(job_id, media_url, attempt, url, found.get("cookiefile"))
                            for video in attempt.glob("video.*"):
                                if video.suffix.lower() in VIDEO_EXT:
                                    video.rename(attempt / f"{safe_name(title, 60)} {group_index:02}{video.suffix}")
                        except UnsupportedStream as error:
                            errors.append(("独立流下载", error))
                            independent = False
                    if not independent:
                        extra = {"cookiefile": found.get("cookiefile"), "http_headers": {"Referer": url, "User-Agent": UA},
                                 "outtmpl": str(attempt / f"{safe_name(title, 60)} {group_index:02}.%(ext)s"),
                                 "retries": 1, "fragment_retries": 1, "file_access_retries": 1}
                        meta = ytdlp_download(job_id, media_url, attempt, extra)
                        meta["source"] = "sniffed"
                    check_cancel(job_id)
                    if not any(p.is_file() and p.suffix.lower() in VIDEO_EXT | AUDIO_EXT
                               and ".part." not in p.name for p in attempt.rglob("*")):
                        raise RuntimeError("下载未产生有效媒体文件")
                    results.append(meta)
                    break  # remaining URLs are quality/signature alternatives, not extra videos
                except Exception as error:
                    shutil.rmtree(attempt, ignore_errors=True)
                    check_cancel(job_id)
                    if isinstance(error, Cancelled):
                        raise
                    if reason := terminal_download_reason(error):
                        raise DownloadFailure(reason) from None
                    if isinstance(error, DownloadFailure) and not error.retryable:
                        raise
                    errors.append((f"备用流 {group_index}.{candidate_index}", error))
            else:
                raise RuntimeError("当前播放器的备用流均不可用")
        if not results:
            raise DownloadFailure("未找到受支持的公开视频流，请提供视频详情页或官方下载链接。")
        return {**results[0], "title": title, "webpage_url": url}, fallback
    except Exception as error:
        shutil.rmtree(fallback, ignore_errors=True)
        check_cancel(job_id)
        if isinstance(error, Cancelled):
            raise
        errors.append(("页面/视频流识别", error))
        reason = terminal_download_reason(error)
        details = "；".join(f"{step}：{failure_detail(e)}" for step, e in errors[-5:])
        retryable = (not reason and not (isinstance(error, DownloadFailure) and not error.retryable)
                     and any(re.search(r"timed? out|timeout|connection|429|50[0234]", str(e), re.I) for _, e in errors))
        raise DownloadFailure((reason or "常规下载与备用流均未成功，请检查网络、链接有效期或提供官方下载链接。") +
                              "\n" + details, retryable=retryable) from None
    finally:
        if found and found.get("cookiefile"):
            Path(found["cookiefile"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- download: aria2 (direct files + torrents)

ARIA_PROGRESS = re.compile(r"\[#\w+ ([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?(?:DL:([\d.]+\w+))?")


def aria2(job_id, args, as_bt=False):
    cmd = ["aria2c", "--continue=true", "--max-tries=10", "--retry-wait=10", "--console-log-level=warn", "--summary-interval=3", "--show-console-readout=true",
           "--enable-color=false", "--file-allocation=falloc", "--auto-file-renaming=false",
           "--allow-overwrite=true"] + args
    if as_bt:
        # Torrents run as `bt`, which the firewall keeps off the proxy
        cmd = ["sudo", "-n", "-u", "bt"] + cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, cancelled, last = [], False, 0.0
    for line in proc.stdout:
        tail = (tail + [line.strip()])[-15:]
        m = ARIA_PROGRESS.search(line)
        if m and time.time() - last >= 3:  # aria2 prints every second: write every 3 s at most
            last = time.time()
            update(job_id, progress=float(m.group(3)), speed=(m.group(4) + "/s") if m.group(4) else "")
        if cancel_requested(job_id):
            cancelled = True
            proc.terminate()
    proc.wait()
    if cancelled:
        raise Cancelled()
    if proc.returncode != 0:
        raise RuntimeError("aria2 failed: " + " | ".join(t for t in tail if t)[-500:])


def head_is_file(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
        if r.status_code >= 400 or r.status_code == 405:
            r = requests.get(url, stream=True, allow_redirects=True, timeout=15, headers={"User-Agent": UA})
            r.close()
    except requests.RequestException:
        return False
    ct = r.headers.get("content-type", "").lower()
    cd = r.headers.get("content-disposition", "").lower()
    if "attachment" in cd:
        return True
    if ct.startswith(("video/", "audio/")) or ct in ("application/octet-stream", "application/x-bittorrent",
                                                     "application/zip", "application/x-iso9660-image"):
        return True
    return bool(re.search(r"\.(mp4|mkv|avi|mov|zip|rar|7z|iso|dmg|exe|pdf|mp3|flac|torrent)$",
                          urllib.parse.urlparse(r.url).path, re.I))
