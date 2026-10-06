"""Getting the file: yt-dlp, a headless browser for pages with an embedded player, aria2 for files and torrents."""
import os
import re
import requests
import subprocess
import time
import urllib.parse
from .core import (CHROMIUM, COOKIES, Cancelled, FORMAT_SORT, STATE, UA, cancel_requested, check_cancel, heavy_slot, human, update)


# ---------------------------------------------------------------- download: yt-dlp

def ytdlp_opts(job_id, workdir, extra=None):
    last = [0.0]

    def hook(d):
        check_cancel(job_id)
        # yt-dlp reports many times a second; the page polls every few seconds. Every write lands on the SD card
        if d["status"] == "downloading" and time.time() - last[0] >= 3:
            last[0] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = done * 100 / total if total else 0
            spd = d.get("speed") or 0
            update(job_id, progress=round(pct, 1), speed=f"{human(spd)}/s" if spd else "")

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
        opts.update(extra)
    return opts


# YouTube sometimes answers one proxy exit IP with "Sign in to confirm you're not a bot" (or 429) while
# the other exit still works. Xray has a SOCKS inbound per exit (127.0.0.1:10820 → proxy-a, 10821 → proxy-b),
# so on that error yt-dlp tries again through each one directly.
BOT_CHECK = re.compile(r"confirm you.?re not a bot|HTTP Error 429|Too Many Requests", re.I)
EXITS = os.environ.get("YTDLP_EXITS", "socks5h://127.0.0.1:10821 socks5h://127.0.0.1:10820").replace(",", " ").split()


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


AD_HOSTS = re.compile(r"doubleclick|googlesyndication|googleads|adservice|adsystem|imasdk|moatads|criteo|taboola")


def dedupe_media(urls):
    seen, out = set(), []
    for u in urls:
        key = urllib.parse.urlsplit(u)._replace(query="", fragment="").geturl()
        if key not in seen and not AD_HOSTS.search(u):
            seen.add(key)
            out.append(u)
    return out


def write_cookie_file(path, cookies):
    """cookies: [{"domain", "path", "secure", "expires", "name", "value"}] -> Netscape cookies.txt for yt-dlp"""
    with open(path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            exp = c.get("expires") or 0
            f.write("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"] or "/",
                               "TRUE" if c["secure"] else "FALSE", str(int(exp) if exp and exp > 0 else 0),
                               c["name"], c["value"] or ""]) + "\n")


def sniff_page(job_id, url):
    """Find the video on a page yt-dlp doesn't know. Fast path: the player config in the page's HTML
    (no browser). Otherwise open it in headless Chromium, start playback and catch the media requests."""
    update(job_id, stage="looking for the video on the page")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        players, _ = media_in_page(r.text, r.url)
        media = dedupe_media(players)
        if media:
            import html as htmllib
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            title = htmllib.unescape(m.group(1)).strip() if m else ""
            cookie_file = STATE / f"cookies-{job_id}.txt"
            write_cookie_file(cookie_file, [{"domain": c.domain, "path": c.path, "secure": c.secure,
                                             "expires": c.expires, "name": c.name, "value": c.value} for c in r.cookies])
            return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file)}
    except requests.RequestException:
        pass
    from playwright.sync_api import sync_playwright
    found = {}  # url -> (rank, size)

    def on_response(r):
        u, ct = r.url, (r.headers.get("content-type") or "").lower()
        if AD_HOSTS.search(u):
            return
        if re.search(r"\.(m3u8|mpd)(\?|$)", u) or "mpegurl" in ct or "dash+xml" in ct:
            rank = 0 if re.search(r"master|playlist|index", u) else 1
            found.setdefault(u, (rank, 0))
        elif ct.startswith("video/") or re.search(r"\.(mp4|webm|mkv|mov|flv)(\?|$)", u):
            size = int(r.headers.get("content-length") or 0)
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
        play_js = "document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })"
        for _ in range(2):
            for frame in page.frames:
                try:
                    frame.evaluate(play_js)
                except Exception:
                    pass
            # Many players only load the stream after a click on the player
            try:
                box = None
                for v in page.query_selector_all("video, iframe"):
                    b = v.bounding_box()
                    if b and b["width"] > 200 and (not box or b["width"] * b["height"] > box["width"] * box["height"]):
                        box = b
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    page.mouse.click(640, 400)
            except Exception:
                pass
            page.wait_for_timeout(6000)
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
        cookies = ctx.cookies()
        browser.close()

    # A page can hold several videos (one player each). Player configs are the most reliable source;
    # otherwise take the best stream the browser actually requested, then anything else in the page.
    media = dedupe_media(players)
    if not media:
        ranked = [u for u, _ in sorted(found.items(), key=lambda kv: (kv[1][0], -kv[1][1]))]
        manifests = [u for u in in_page if re.search(r"\.(m3u8|mpd)(\?|$)", u)]
        media = dedupe_media(ranked[:1] or manifests or in_page)
    if not media:
        return None
    cookie_file = STATE / f"cookies-{job_id}.txt"
    write_cookie_file(cookie_file, cookies)
    return {"media_urls": media[:10], "title": title, "cookiefile": str(cookie_file)}


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
