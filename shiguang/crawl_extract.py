"""Read video/catalogue metadata from HTML without executing scripts or making requests.

The crawler owns fetching, limits and persistence.  This module deliberately keeps
query strings intact: /stream?id=1 and /stream?id=2 can be different videos.
"""
import html
import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_MEDIA_EXT = re.compile(r"\.(?:m3u8|mpd|mp4|m4v|webm|mov|mkv|flv|avi|ogv)(?:$|[?#])", re.I)
_AD_HOST = re.compile(r"doubleclick|googlesyndication|googleads|adservice|adsystem|imasdk|moatads|criteo|taboola", re.I)
_AD_PATH = re.compile(r"(?:^|/)(?:ads?|advert|preroll)(?:/|[_.-])", re.I)
_SEGMENT = re.compile(r"(?:^|[/_.-])(?:init|segment|chunk|frag)(?:[-_.]?\d*|[-_.][^/]*)\.(?:mp4|m4s|ts)$", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_PAGE_KEYS = {"page", "p", "paged", "pagenum", "page_num", "pagenumber", "page_number", "pageno", "offset", "start"}
_CURSOR_KEYS = {"cursor", "after", "before", "next", "page_token", "continuation"}
_PAGE_CONTEXT = re.compile(r"(?:^|[\s_-])(?:pagination|paginate|pager|paging|pages|page-numbers|page-nav)(?:$|[\s_-])|分页|分頁", re.I)
_CHROME = re.compile(r"(?:^|[\s_-])(?:nav|navigation|menu|breadcrumb|footer|sidebar|social|share|related|recommend(?:ed|ations)?|tag-cloud|advert|ads)(?:$|[\s_-])", re.I)
_CARD = re.compile(r"(?:^|[\s_-])(?:video|movie|clip|post|entry|article|card|thumb(?:nail)?|title|headline|item)(?:$|[\s_-])", re.I)
_IGNORED_ROUTE = re.compile(r"/(?:tags?|categor(?:y|ies)|authors?|users?|login|logout|register|signup|about|contact|privacy|terms|search|feeds?)(?:/|$)", re.I)
_DETAIL_ROUTE = re.compile(r"/(?:videos?|movies?|clips?|posts?|articles?|archives?|watch|view|play|details?|episodes?)(?:/|\.|$)", re.I)
_STATIC_EXT = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|json|xml|txt|pdf|zip|gz|mp3|wav|aac|torrent)$", re.I)
_NEXT = re.compile(r"^(?:next(?:\s*page)?|older(?:\s*(?:posts|entries))?|load\s*more|more|下一[页頁]|下[页頁]|后[页頁]|後[页頁]|后一[页頁]|後一[页頁]|更多|继续|繼續|[>›»→]+)$", re.I)
_BACK = re.compile(r"(?:previous|\bprev\b|\bfirst\b|\blast\b|上一[页頁]|上[页頁]|前一[页頁]|前[页頁]|末[页頁]|尾[页頁]|最[后後]|首[页頁]|^[<‹«←]+$)", re.I)
_FIELD = re.compile(r'''(?:["'](?P<quoted>[\w-]+)["']|(?P<plain>[$\w-]+))\s*:\s*''')
_STRING = re.compile(r'''(?P<quote>["'])(?P<value>(?:\\.|[^\\])*?)(?P=quote)''', re.S)
_URL_FIELD = {"url", "src", "file", "source", "contenturl", "embedurl", "hls", "dash", "mp4", "m3u8", "playurl", "play_url"}
_JS_KEY = re.compile(r"[$\w-]+\s*:")
_DURATION_CONTEXT = re.compile(r'(?:^|[\s_-])(?:duration|runtime|playtime|video-time|video-length)(?:$|[\s_-])|时长|時長', re.I)
_DURATION_ATTRS = ('data-duration-seconds', 'data-duration', 'data-video-duration', 'duration')
_DURATION_FIELDS = {'duration', 'durationseconds', 'duration_seconds', 'video_duration'}


def parse_duration(value):
    """Finite positive seconds from an explicitly identified duration field."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 100:
                return None
            value = re.sub(r'^(?:duration|runtime|时长|時長)\s*[:：]?\s*', '', value, flags=re.I)
            iso = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?=\d)(?:(\d+(?:\.\d+)?)H)?'
                               r'(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', value, re.I)
            if iso and any(part is not None for part in iso.groups()):
                seconds = sum(float(part or 0) * multiplier for part, multiplier in zip(iso.groups(), (86400, 3600, 60, 1)))
            elif re.fullmatch(r'\d+:\d{2}(?::\d{2})?(?:\.\d+)?', value):
                parts = value.split(':')
                if float(parts[-1]) >= 60 or len(parts) == 3 and int(parts[1]) >= 60:
                    return None
                seconds = sum(float(part) * multiplier for part, multiplier in zip(reversed(parts), (1, 60, 3600)))
            elif re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)', value):
                seconds = float(value)
            else:
                return None
        else:
            seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except (ValueError, OverflowError):
        return None


def _space(value):
    return " ".join(str(value or "").split())


def _literal(value):
    """Decode common JSON/JS string escapes, without evaluating JavaScript."""
    value = html.unescape(str(value or "").strip())
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), value)
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), value)
    return value.replace("\\/", "/").replace("\\'", "'").replace('\\"', '"')


def _url(value, base):
    value = _literal(value)
    if not value or value.startswith(("#", "javascript:", "data:", "blob:", "mailto:", "tel:")):
        return ""
    try:
        absolute = urljoin(base, value)
        p = urlsplit(absolute)
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        if re.search(r"[\x00-\x20\x7f]", absolute):
            return ""
        # Validate malformed ports as well, and remove only the fragment.
        p.port
        return urlunsplit((p.scheme.lower(), p.netloc, p.path or "/", p.query, ""))
    except (ValueError, TypeError):
        return ""


def _usable(url):
    if not url:
        return False
    p = urlsplit(url)
    return not (_AD_HOST.search(p.hostname or "") or _AD_PATH.search(p.path) or _SEGMENT.search(p.path)
                or p.path.lower().endswith((".ts", ".m4s", ".aac")))


def _media(url):
    return bool(_usable(url) and _MEDIA_EXT.search(urlsplit(url).path))


def _platform(url):
    """Return a stable watch URL for an identifiable supported platform video."""
    if not url:
        return ""
    p = urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    query = dict(parse_qsl(p.query))
    if host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com", "youtu.be"):
        video = ""
        if host == "youtu.be":
            video = p.path.strip("/").split("/")[0]
        elif p.path.rstrip("/") == "/watch":
            video = query.get("v", "")
        else:
            match = re.match(r"/(?:embed|shorts|live)/([\w-]+)", p.path)
            video = match[1] if match else ""
        if re.fullmatch(r"[\w-]{6,64}", video):
            return "https://www.youtube.com/watch?" + urlencode({"v": video})
    if host in ("bilibili.com", "m.bilibili.com", "player.bilibili.com"):
        match = re.search(r"/video/((?:BV[0-9A-Za-z]+|av\d+))(?:/|$)", p.path)
        video = match[1] if match else query.get("bvid", "")
        if not video and query.get("aid", "").isdigit():
            video = "av" + query["aid"]
        if re.fullmatch(r"BV[0-9A-Za-z]+|av\d+", video):
            page = query.get("p") or query.get("page") or "1"
            return "https://www.bilibili.com/video/" + video + "/" + ("?p=" + page if page.isdigit() and int(page) > 1 else "")
    if host in ("vimeo.com", "player.vimeo.com"):
        match = re.fullmatch(r"/(?:video/)?(\d+)(?:/([a-zA-Z0-9]+))?/?", p.path)
        if match:
            token = match[2] or query.get("h")
            return "https://vimeo.com/" + match[1] + ("/" + token if token else "")
    if host in ("dailymotion.com", "dai.ly"):
        match = re.match(r"/(?:embed/)?video/([\w]+)", p.path) if host != "dai.ly" else re.match(r"/([\w]+)", p.path)
        if match:
            return "https://www.dailymotion.com/video/" + match[1]
    return ""


class _Node:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, attrs or {}, parent, []

    def text(self):
        parts, pending = [], list(reversed(self.children))
        while pending:
            child = pending.pop()
            if isinstance(child, str):
                parts.append(child)
            elif child.tag not in ("script", "style", "noscript"):
                pending.extend(reversed(child.children))
        return _space(" ".join(parts))

    def ancestors(self):
        node = self
        while node is not None:
            yield node
            node = node.parent

    def descendants(self):
        pending = list(reversed(self.children))
        while pending:
            child = pending.pop()
            if isinstance(child, _Node):
                yield child
                pending.extend(reversed(child.children))

    def context(self):
        return " ".join(self.attrs.get(k, "") for k in ("class", "id", "role", "aria-label"))


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        # Common optional end tags; malformed catalogue cards should not nest forever.
        if tag in ("a", "li", "option", "p"):
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == tag:
                    self.stack = self.stack[:i]
                    break
                if self.stack[i].tag in ("ul", "ol", "select", "article", "section"):
                    break
        node = _Node(tag, {k.lower(): v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _raw(node):
    return "".join(child for child in node.children if isinstance(child, str))


def _anchor_title(node):
    title = node.text() or node.attrs.get("title") or node.attrs.get("aria-label") or ""
    if not title:
        title = next((n.attrs.get("alt") or n.attrs.get("title") or "" for n in node.descendants() if n.tag == "img"), "")
    return _space(title)[:1000]


def _element_duration(node):
    for key in _DURATION_ATTRS:
        if (duration := parse_duration(node.attrs.get(key))) is not None:
            return duration
    marked = ('duration' in node.attrs.get('itemprop', '').lower().split()
              or bool(_DURATION_CONTEXT.search(node.context())))
    if marked:
        for value in (node.attrs.get('content'), node.attrs.get('datetime'), node.attrs.get('value'), node.text()):
            if (duration := parse_duration(value)) is not None:
                return duration
    if node.tag == 'time' and node.attrs.get('datetime', '').upper().startswith('P'):
        return parse_duration(node.attrs['datetime'])
    return None


def _near_duration(node, base, current):
    """Read the element or its single-video card, never an unrelated sibling."""
    if (duration := _element_duration(node)) is not None:
        return duration
    scopes = [node]
    for parent in list(node.ancestors())[1:9]:
        if parent.tag in ('body', 'html', 'main', 'document'):
            break
        if (parent.tag in ('article', 'li') or _CARD.search(parent.context())
                or 'videoobject' in parent.attrs.get('itemtype', '').lower()):
            scopes.append(parent)
            break
        if parent.tag == 'section':
            break
    for scope in scopes:
        children = []
        for child in scope.descendants():
            children.append(child)
            if len(children) > 200:
                break
        if len(children) > 200:
            continue
        targets = set()
        players = 0
        for child in [scope, *children]:
            attrs = child.attrs
            if child.tag == 'video':
                players += 1
            elif child.tag in ('iframe', 'embed', 'object'):
                target = _url(attrs.get('src') or attrs.get('data-src') or attrs.get('data'), base)
                players += bool(_media(target) or _platform(target))
            elif child.tag == 'script':
                if 'ld+json' in attrs.get('type', '').lower():
                    try:
                        players += sum(1 for _ in _video_objects(json.loads(_raw(child))))
                    except (ValueError, TypeError):
                        pass
                else:
                    players += len(_script_groups(_raw(child), base))
            elif child.tag != 'source' and re.search(r'(?:player|video)', child.context(), re.I):
                values = [_url(attrs.get(key), base) for key in ('data-src', 'data-url', 'data-video', 'data-video-url', 'data-m3u8', 'data-mp4')]
                players += (any(_media(value) or _platform(value) for value in values)
                            or 'dplayer' in attrs.get('class', '').lower().split() and attrs.get('data-video-id', '').isdigit())
            if child.tag == 'a':
                target = _url(attrs.get('href'), base)
                if target and (_media(target) or _platform(target) or _DETAIL_ROUTE.search(urlsplit(target).path)
                               or _detail_link(child, target, current)):
                    targets.add(target)
        if len(targets) > 1 or players > 1:
            continue
        for child in [scope, *children]:
            if (duration := _element_duration(child)) is not None:
                return duration
    return None


def _in_chrome(node):
    ancestors = list(node.ancestors())
    for item in ancestors:
        if item.tag in ("nav", "footer", "aside") or _CHROME.search(item.context()):
            return True
        if item.tag == "header" and not any(n.tag == "article" for n in item.ancestors()):
            return True
    return False


def _page_state(url, numeric_path=False):
    p = urlsplit(url)
    path, number, style = p.path.rstrip("/") or "/", None, ""
    match = re.search(r"/(?:page|paged)[/-]?(\d+)(?:\.html?)?$", path, re.I)
    if match:
        number, style, path = int(match[1]), "page", path[:match.start()] or "/"
    else:
        match = re.search(r"([_-])(\d+)(\.html?)$", path, re.I)
        if match:
            number, style, path = int(match[2]), "page", path[:match.start()] + match[3]
        elif numeric_path:
            match = re.search(r"/(\d+)(?:\.html?)?$", path)
            if match:
                number, style, path = int(match[1]), "page", path[:match.start()] or "/"
    path = re.sub(r"/index\.html?$", "/", path, flags=re.I)
    remaining = []
    for key, value in parse_qsl(p.query, keep_blank_values=True):
        low = key.lower()
        if low in _PAGE_KEYS and value.isdigit():
            number, style = int(value), "offset" if low in ("offset", "start") else "page"
        elif low in _CURSOR_KEYS:
            style = "cursor"
        elif not low.startswith("utm_") and low not in ("fbclid", "gclid"):
            remaining.append((key, value))
    return p.netloc.lower(), path.rstrip("/") or "/", sorted(remaining), number, style


def _pagination(node, target, current):
    rel = set(node.attrs.get("rel", "").lower().split())
    title = _anchor_title(node) if node.tag == "a" else ""
    label = _space(" ".join((title, node.attrs.get("title", ""), node.attrs.get("aria-label", ""))))
    context = " ".join(n.context() for n in node.ancestors())
    if rel.intersection({"prev", "previous", "first", "last"}) or _BACK.search(label) or re.search(r"(?:^|[\s_-])(?:prev|previous|last|first)(?:$|[\s_-])", node.context(), re.I):
        return False
    numeric = bool(re.fullmatch(r"\[?\d+\]?", title))
    next_title = title.strip(" \t\r\n><›»→«‹←[]")
    next_hint = ("next" in rel or bool(_NEXT.fullmatch(next_title) or _NEXT.fullmatch(node.attrs.get("aria-label", "")))
                 or bool(re.search(r"(?:^|[\s_-])next(?:$|[\s_-])", node.context(), re.I)))
    pager = bool(_PAGE_CONTEXT.search(context))
    if not (next_hint or numeric or pager):
        return False
    if urlsplit(target).netloc.lower() != urlsplit(current).netloc.lower() or target == current:
        return False
    before = _page_state(current, numeric_path=pager or numeric)
    after = _page_state(target, numeric_path=pager or numeric)
    if before[:3] != after[:3] or not after[4]:
        return False
    if after[3] is not None:
        start = before[3] if before[3] is not None else (0 if after[4] == "offset" else 1)
        return after[3] > start
    return next_hint and after[4] == "cursor"


def _detail_link(node, target, current):
    if _in_chrome(node) or urlsplit(target).netloc.lower() != urlsplit(current).netloc.lower():
        return False
    if target == current:
        return False
    p = urlsplit(target)
    identity_keys = {key.lower() for key, _ in parse_qsl(p.query)}.intersection({"id", "v", "video", "vid", "video_id", "viewkey", "bvid", "aid"})
    if (p.path == "/" or p.path == urlsplit(current).path) and not identity_keys:
        return False
    if _STATIC_EXT.search(p.path) or _IGNORED_ROUTE.search(p.path):
        return False
    if any(_PAGE_CONTEXT.search(n.context()) for n in node.ancestors()):
        return False
    title = _anchor_title(node)
    if not title or _NEXT.fullmatch(title) or _BACK.search(title) or title.isdigit():
        return False
    descendants = list(node.descendants())
    if any(n.tag in ("h1", "h2", "h3", "h4", "h5", "h6", "img") for n in descendants):
        return True
    if any(n.tag == "article" or _CARD.search(n.context()) for n in node.ancestors()):
        return True
    if _DETAIL_ROUTE.search(p.path) or re.search(r"\.(?:html?|php|aspx?)$", p.path, re.I):
        return True
    if identity_keys:
        return True
    return len(title) >= 3 and any(n.tag in ("main", "section") for n in node.ancestors())


def _js_frames(text):
    """Brace ranges plus immediate property names; strings/comments are skipped."""
    frames, stack, i = [], [], 0
    while i < len(text):
        c = text[i]
        if c in ('"', "'", "`"):
            start, quote = i, c
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            if stack and quote != "`":
                after = i
                while after < len(text) and text[after].isspace():
                    after += 1
                if after < len(text) and text[after] == ":":
                    key = _literal(text[start + 1:i - 1]).lower()
                    stack[-1]["keys"].add(key)
                    stack[-1]['properties'].append((key, after + 1))
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if c == "{":
            prefix = text[max(0, i - 80):i]
            field = re.search(r'''(?:["']([\w-]+)["']|([\w-]+))\s*:\s*$''', prefix)
            frame = {"start": i, "end": len(text), "keys": set(), 'properties': [], "parent": stack[-1] if stack else None,
                     "field": (field[1] or field[2]).lower() if field else ""}
            frames.append(frame)
            stack.append(frame)
        elif c == "}" and stack:
            stack.pop()["end"] = i + 1
        elif stack and (c.isalpha() or c in "_$"):
            match = _JS_KEY.match(text, i)
            if match:
                key = match[0][:-1].strip().lower()
                stack[-1]["keys"].add(key)
                stack[-1]['properties'].append((key, i + len(match[0])))
                i += len(match[0])
                continue
        i += 1
    return frames


def _script_groups(text, base):
    """Recognize data literals, grouping source/quality lists within one player."""
    frames = _js_frames(text)
    candidates, blocked = [], []
    for frame in frames:
        lineage, item = [], frame
        while item:
            lineage.append(item["field"])
            item = item["parent"]
        if any(x in {"ad", "ads", "advertising", "advertisement", "preroll", "vast"} for x in lineage):
            blocked.append((frame["start"], frame["end"]))
            continue
        keys = frame["keys"]
        if keys.intersection(_URL_FIELD) or keys.intersection({"sources", "quality"}) or frame["field"] == "video":
            candidates.append(frame)
    field_urls = []
    for match in _FIELD.finditer(text):
        key = (match["quoted"] or match["plain"]).lower()
        if key not in _URL_FIELD:
            continue
        string = _STRING.match(text, match.end())
        if not string:
            continue
        pos = match.start()
        if any(start <= pos < end for start, end in blocked):
            continue
        value = _url(string["value"], base)
        if _media(value) or _platform(value):
            field_urls.append((pos, value))
    groups, consumed = [], set()
    # Broad source/quality scopes first. Overlaps are combined by extract_page.
    for frame in candidates:
        nested = bool(frame["keys"].intersection({"sources", "quality"}) or frame["field"] == "video")
        values = []
        for pos, value in field_urls:
            if not frame["start"] <= pos < frame["end"]:
                continue
            if not nested and any(other is not frame and frame["start"] < other["start"] <= pos < other["end"] < frame["end"] for other in frames):
                continue
            values.append(value)
            consumed.add(value)
        if values:
            body = text[frame["start"]:frame["end"]]
            name = re.search(r'''(?:["'](?:title|name)["']|\b(?:title|name))\s*:\s*(["'])((?:\\.|[^\\])*?)\1''', body, re.S)
            duration = None
            for field, start in frame['properties']:
                if field not in _DURATION_FIELDS:
                    continue
                while start < frame['end'] and text[start].isspace():
                    start += 1
                literal = _STRING.match(text, start)
                number = re.match(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?=\s*[,}\]])', text[start:frame['end']])
                value = _literal(literal['value']) if literal else float(number[0]) if number else None
                if (duration := parse_duration(value)) is not None:
                    break
            groups.append((list(dict.fromkeys(values)), _space(_literal(name[2])) if name else "", duration))
    # A standalone string assignment is common for small players. Unrelated
    # literals remain distinct rather than incorrectly making one giant video.
    for string in _STRING.finditer(text):
        if any(start <= string.start() < end for start, end in blocked):
            continue
        value = _url(string["value"], base)
        if value not in consumed and (_media(value) or _platform(value)):
            groups.append(([value], "", None))
            consumed.add(value)
    return groups


def _video_objects(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            kinds = item.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if isinstance(kinds, list) and any(str(k).rsplit("/", 1)[-1].lower() == "videoobject" for k in kinds):
                yield item
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))


def _values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, str)]
    return []


def extract_page(html_text, url, listing=True):
    """Extract metadata, distinct videos, next catalogue pages and detail links.

    ``aliases`` contains exact alternative media/watch URLs for the same video;
    the containing page is only an alias for a detail page with a single video.
    ``lazy_players`` contains public DPlayer API URLs for the crawler to fetch.
    All returned lists preserve document order and suppress exact duplicates.
    """
    current = _url(url, url)
    if not current:
        raise ValueError("The page URL must be an absolute HTTP(S) URL")
    document = _Document()
    document.feed(str(html_text or ""))
    document.close()
    base = current
    for node in document.nodes:
        if node.tag == "base" and node.attrs.get("href"):
            base = _url(node.attrs["href"], current) or current
            break
    meta = {}
    canonical = current
    title = ""
    for node in document.nodes:
        if node.tag == "title" and not title:
            title = node.text()
        if node.tag == "meta":
            key = (node.attrs.get("property") or node.attrs.get("name") or "").lower()
            if (not key and 'duration' in node.attrs.get('itemprop', '').lower().split()
                    and node.parent.tag in ('head', 'document')):
                key = 'duration'
            if key and node.attrs.get("content"):
                meta.setdefault(key, _space(node.attrs["content"]))
        if node.tag == "link" and "canonical" in node.attrs.get("rel", "").lower().split():
            canonical = _url(node.attrs.get("href"), base) or canonical
    title = meta.get("og:title") or meta.get("twitter:title") or title
    if not title:
        title = next((n.text() for n in document.nodes if n.tag == "h1" and n.text()), "")
    title = title[:1000]
    description = (meta.get("description") or meta.get("og:description") or meta.get("twitter:description") or "")[:5000]
    result = {"title": title, "description": description, "canonical": canonical,
              "videos": [], "pages": [], "links": [], "lazy_players": []}
    groups, page_seen, links, lazy_seen, link_durations = [], set(), {}, set(), {}

    def add(values, name="", desc="", aliases=(), allow_page=False, explicit_media=False, duration=None):
        media, equivalent, platforms = [], [], []
        for value in values:
            value = _url(value, base)
            if not _usable(value):
                continue
            platform = _platform(value)
            if platform:
                platforms.append(platform)
                equivalent.extend((platform, value))
            elif _media(value) or explicit_media:
                media.append(value)
                equivalent.append(value)
            elif allow_page:
                equivalent.append(value)
        if not equivalent:
            return
        for value in aliases:
            value = _url(value, base)
            if value and value not in (current, canonical):
                equivalent.append(value)
        primary = media[0] if media else platforms[0] if platforms else equivalent[0]
        groups.append({"url": primary, "title": _space(name)[:1000] or title,
                       "description": _space(desc)[:5000] or description, "page_url": current,
                       "media_urls": list(dict.fromkeys(media)), "aliases": list(dict.fromkeys(equivalent)),
                       'duration': parse_duration(duration)})
        return groups[-1]

    for node in document.nodes:
        attrs = node.attrs
        if node.tag == "video":
            values = [attrs.get(key, "") for key in ("src", "data-src", "data-url", "data-video-src")]
            for child in node.descendants():
                if child.tag == "source":
                    values.extend(child.attrs.get(key, "") for key in ("src", "data-src"))
            add(values, attrs.get("title") or attrs.get("aria-label"), explicit_media=True,
                duration=_near_duration(node, base, current))
        elif node.tag in ("iframe", "embed", "object"):
            value = attrs.get("src") or attrs.get("data-src") or attrs.get("data") or ""
            add([value], attrs.get("title"), duration=_near_duration(node, base, current))
        elif node.tag == "script":
            text = _raw(node)
            if "ld+json" in attrs.get("type", "").lower():
                try:
                    data = json.loads(text.strip())
                except (ValueError, TypeError):
                    data = None
                for video in _video_objects(data):
                    content = _values(video.get("contentUrl"))
                    embeds = _values(video.get("embedUrl"))
                    pages = _values(video.get("url"))
                    added = add(content + embeds + pages, video.get("name") or video.get("headline"), video.get("description"),
                                aliases=embeds + pages, allow_page=True, duration=video.get('duration'))
                    # Explicit contentUrl can be an extensionless streaming API.
                    if content and added:
                        for value in content:
                            value = _url(value, base)
                            if _usable(value) and not _platform(value) and value not in added["media_urls"]:
                                added["media_urls"].append(value)
                        if added["media_urls"]:
                            added["url"] = added["media_urls"][0]
            else:
                for values, name, duration in _script_groups(text, base):
                    add(values, name, duration=duration)
        if node.tag not in ("script", "video", "source") and re.search(r"(?:player|video)", node.context(), re.I):
            add([attrs.get(key, "") for key in ("data-src", "data-url", "data-video", "data-video-url", "data-m3u8", "data-mp4")],
                attrs.get("title"), duration=_near_duration(node, base, current))
        video_id = attrs.get("data-video-id", "")
        player_index = attrs.get("data-player-index", "0")
        if "dplayer" in attrs.get("class", "").lower().split() and video_id.isdigit() and player_index.isdigit():
            endpoint = _url(f"/play-url/{video_id}.json?i={player_index}", base)
            if endpoint and endpoint not in lazy_seen:
                result["lazy_players"].append(endpoint)
                lazy_seen.add(endpoint)
        if node.tag in ("a", "link") and attrs.get("href"):
            target = _url(attrs["href"], base)
            if not target:
                continue
            if _pagination(node, target, current):
                if target not in page_seen:
                    result["pages"].append(target)
                    page_seen.add(target)
                continue
            if node.tag == "a":
                if not _in_chrome(node):
                    duration = _near_duration(node, base, current)
                    add([target], _anchor_title(node), duration=duration)
                if listing and not (_media(target) or _platform(target)) and _detail_link(node, target, current):
                    name = _anchor_title(node)
                    if target not in links or len(name) > len(links[target]):
                        links[target] = name
                    if (duration := _near_duration(node, base, current)) is not None:
                        link_durations.setdefault(target, duration)
    # Open Graph/Twitter video metadata commonly describes the same player as
    # HTML5/JSON-LD. Exact matching below combines those representations.
    for key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player", "twitter:player:stream"):
        if meta.get(key):
            add([meta[key]], explicit_media=key == "twitter:player:stream" or meta.get("og:video:type", "").startswith("video/"),
                duration=meta.get('video:duration') or meta.get('og:video:duration'))

    # Merge overlapping URL sets transitively. Page URLs do not establish video
    # identity: an article can contain multiple independent clips.
    merged = []
    for group in groups:
        group["aliases"] = [value for value in group["aliases"] if value not in (current, canonical) or group["url"] == value]
        identity = set(group["media_urls"] + group["aliases"]) - {current, canonical}
        matching = [other for other in merged if identity.intersection(set(other["media_urls"] + other["aliases"]) - {current, canonical})]
        if not matching:
            merged.append(group)
            continue
        first = matching[0]
        for other in matching[1:] + [group]:
            first["media_urls"] = list(dict.fromkeys(first["media_urls"] + other["media_urls"]))
            first["aliases"] = list(dict.fromkeys(first["aliases"] + other["aliases"]))
            if first["title"] == title and other["title"] != title:
                first["title"] = other["title"]
            if first["description"] == description and other["description"] != description:
                first["description"] = other["description"]
            if first['duration'] is None and other['duration'] is not None:
                first['duration'] = other['duration']
            # A new duplicate dict may become equal to first after enriching
            # duration/title. Only remove the actual previous object, not its
            # now-equal retained representative.
            merged[:] = [entry for entry in merged if entry is not other]
        if first["media_urls"]:
            first["url"] = first["media_urls"][0]
    for group in merged:
        if len(merged) == 1 and not listing:
            group["aliases"] = list(dict.fromkeys(group["aliases"] + [current, canonical]))
            if group['duration'] is None:
                group['duration'] = parse_duration(meta.get('video:duration') or meta.get('og:video:duration') or meta.get('duration'))
        else:
            group["aliases"] = [value for value in group["aliases"] if value not in (current, canonical)]
    result["videos"] = merged
    result["links"] = [{"url": target, "title": name, **({'duration': link_durations[target]} if target in link_durations else {})}
                       for target, name in links.items() if target not in page_seen]
    return result
