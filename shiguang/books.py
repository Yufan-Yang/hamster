"""书架: e-books. A book sent as EPUB / TXT / PDF / MOBI / AZW3 is turned once into something every browser can read:
reflowed chapters of clean HTML ("flow": EPUB, TXT, MOBI, AZW3) or the PDF itself with its pages listed ("pdf"),
plus its text per chapter / page for search. The reading pack is what a phone caches to read without the Pi."""
import gzip
import hashlib
import html
import io
import json
import posixpath
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET

from html.parser import HTMLParser
from pathlib import Path
from . import core
from .core import BOOKS_DIR, UA, db_lock, q, safe_name


BOOK_EXT = {".epub": "epub", ".txt": "txt", ".pdf": "pdf", ".mobi": "mobi", ".azw3": "azw3", ".azw": "azw3"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp"}
BIG_CHAPTER = 60000  # characters: a longer chapter is split (a whole book in one HTML file is slow to lay out)
DOWNLOAD_MAX = 500 << 20


class BookError(ValueError):
    """The file can't be read as a book (said in plain words on the shelf); trying again won't help."""


def book_fmt(name):
    return BOOK_EXT.get(Path(name or "").suffix.lower())


def is_book_url(url):
    """A link straight to an e-book file (…/x.epub, …/x.pdf?dl=1): goes to the shelf instead of the download queue."""
    if not url.startswith(("http://", "https://")):
        return False
    return book_fmt(urllib.parse.unquote(urllib.parse.urlsplit(url).path)) is not None


def sniff(head, name=""):
    """The format from a file's first bytes (downloads are often named oddly), else from its name."""
    fmt = book_fmt(name)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK") and (b"mimetypeapplication/epub+zip" in head[:100] or fmt == "epub"):
        return "epub"  # (some EPUBs don't put their mimetype first)
    if head[60:68] == b"BOOKMOBI":
        return fmt if fmt in ("mobi", "azw3") else "mobi"
    if fmt == "txt":
        return "txt"
    start = head[:200].lstrip().lower()
    if fmt is None and head and not start.startswith((b"<!doctype", b"<html", b"<?xml", b"{")) \
            and decode_text(head[:4096], probe=True):
        return "txt"  # a download without a telling name that is plain text (not an error page)
    return None


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def insert(sql, args):
    with db_lock:
        cur = core.DB.execute(sql, args)
        core.DB.commit()
        return cur.lastrowid


def path_of(bid, what):
    """Files of a book: the original (`orig`, with its own extension), a converted copy, the pack, the cover."""
    return BOOKS_DIR / {"pack": f"{bid}.pack.json.gz", "cover": f"{bid}.jpg"}[what]


# ---------------------------------------------------------------- HTML: parse loosely, keep only what's safe

VOID = {"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr", "source", "param", "embed", "image",
        "mbp:pagebreak"}
DROP = {"script", "style", "head", "title", "iframe", "object", "embed", "form", "input", "button", "textarea",
        "select", "noscript", "template", "audio", "video", "canvas", "math", "map", "link", "meta"}
BLOCK = {"p", "div", "section", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd",
         "pre", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "figure", "figcaption", "hr"}
INLINE = {"a", "span", "em", "i", "strong", "b", "u", "s", "del", "ins", "sub", "sup", "small", "code", "br", "img",
          "ruby", "rt", "rp", "rb", "q", "cite", "abbr", "mark"}
RENAME = {"big": "span", "font": "span", "tt": "code", "strike": "s", "center": "div", "aside": "div", "header": "div",
          "footer": "div", "main": "div", "nav": "div", "article": "section", "kbd": "code", "var": "em"}
# a closing tag missing in sloppy HTML (MOBI): a new block closes an open paragraph
CLOSES_P = BLOCK - {"td", "th", "tr", "tbody", "thead", "tfoot", "caption", "li", "dt", "dd"}
CJK = "⺀-鿿豈-﫿＀-￯　-〿"


class El:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs=None, kids=None):
        self.tag, self.attrs, self.kids = tag, attrs or {}, kids or []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag in CLOSES_P and any(e.tag == "p" for e in self.stack[1:]):
            while len(self.stack) > 1 and self.stack[-1].tag != "p":
                self.stack.pop()
            self.stack.pop()
        el = El(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].kids.append(el)
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(El(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def parse_html(text):
    t = _Tree()
    t.feed(text)
    t.close()
    return t.root


def find_all(el, pred):
    out, todo = [], [el]
    while todo:
        e = todo.pop()
        if isinstance(e, El):
            if pred(e):
                out.append(e)
            todo.extend(reversed(e.kids))
    return out


def text_of(el):
    if isinstance(el, str):
        return el
    if el.tag in DROP:
        return ""
    sep = "\n" if el.tag in BLOCK or el.tag == "br" else ""
    return sep + "".join(text_of(k) for k in el.kids) + sep


def tidy_text(s):
    """Whitespace as a browser would show it, except that a line break between two CJK characters is no space."""
    s = re.sub(rf"(?<=[{CJK}])[ \t\r]*\n\s*(?=[{CJK}])", "", s)
    return re.sub(r"\s+", " ", s)


class Cleaner:
    """Turns a parsed chapter into safe HTML: a short list of tags and attributes, pictures and links pointing into
    the book, ids prefixed so they can't clash with the page's."""

    def __init__(self, doc_path, names, links):
        self.dir = posixpath.dirname(doc_path)
        self.doc = doc_path
        self.names = names  # files in the book (pictures that exist)
        self.links = links  # path -> True for chapter files a link may point to
        self.res = set()
        self.ids = set()

    def resolve(self, href):
        href = urllib.parse.unquote((href or "").strip())
        path, _, frag = href.partition("#")
        if not path:
            return self.doc, frag
        return posixpath.normpath(posixpath.join(self.dir, path)), frag

    def picture(self, src, alt=""):
        if not src or src.startswith(("data:", "http:", "https:")):
            return []
        path, _ = self.resolve(src)
        if path not in self.names or Path(path).suffix.lower() not in IMAGE_TYPES:
            return []
        self.res.add(path)
        return [El("img", {"data-res": path, "alt": alt[:200]})]

    def clean(self, node, pre=False):
        if isinstance(node, str):
            return [node if pre else tidy_text(node)] if node else []
        tag = node.tag
        if tag in DROP:
            return []
        if tag == "svg":  # a picture wrapped in SVG (covers): only its <image>
            return [x for im in find_all(node, lambda e: e.tag in ("image", "svg:image"))
                    for x in self.picture(im.attrs.get("xlink:href") or im.attrs.get("href"))]
        if tag in ("image", "img"):
            return self.picture(node.attrs.get("src") or node.attrs.get("xlink:href") or node.attrs.get("href"),
                                node.attrs.get("alt", ""))
        if tag == "mbp:pagebreak":
            return [El("hr", {"data-break": "1"})]
        kids = [x for k in node.kids for x in self.clean(k, pre or tag == "pre")]
        tag = RENAME.get(tag, tag)
        if tag not in BLOCK and tag not in INLINE:
            return kids  # html, body, unknown tags: keep what's inside
        a, attrs = node.attrs, {}
        ident = a.get("id") or (a.get("name") if tag == "a" else "")
        if ident and re.fullmatch(r"[\w.:-]{1,80}", ident):
            attrs["id"] = "x-" + ident
            self.ids.add(ident)
        style, cls = a.get("style", "").lower(), a.get("class", "").lower()
        if node.tag == "center" or "text-align:center" in style.replace(" ", "") or re.search(r"\b(center|centre)\b", cls):
            attrs["class"] = "c"
        elif "text-align:right" in style.replace(" ", "") or re.search(r"\bright\b", cls):
            attrs["class"] = "r"
        if tag == "a":
            href = a.get("href", "").strip()
            if re.match(r"(?i)(https?:|mailto:)", href):
                attrs.update(href=href, target="_blank", rel="noopener")
            elif href and not re.match(r"(?i)[a-z][a-z0-9+.-]*:", href):
                path, frag = self.resolve(href)
                if path in self.links:
                    attrs["data-go"] = f"\x00{path}#{frag}\x00"  # the chapter number is known once all are read
            et = (a.get("epub:type", "") + " " + cls).lower()
            if "noteref" in et or "footnote" in et:
                attrs["data-note"] = "1"
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit():
                    attrs[k] = a[k]
        elif tag in ("abbr", "span") and a.get("title"):
            attrs["title"] = a["title"][:200]
        if tag == "span" and not attrs:
            return kids
        return [El(tag, attrs, kids)]


def is_block(n):
    return isinstance(n, El) and n.tag in BLOCK


def paragraphs(nodes):
    """Containers holding text directly next to blocks (or a <div> used as a paragraph): runs of inline content
    become paragraphs, so every line of text is in a <p> the reader can style."""
    out, run = [], []

    def flush():
        if any((isinstance(x, str) and x.strip()) or (isinstance(x, El) and x.tag != "br") for x in run):
            while run and (isinstance(run[0], str) and not run[0].strip() or isinstance(run[0], El) and run[0].tag == "br"):
                run.pop(0)
            out.append(El("p", {}, list(run)))
        run.clear()
    for n in nodes:
        if is_block(n):
            flush()
            if n.tag in ("div", "section", "blockquote", "li", "dd", "td", "th", "figure"):
                if any(is_block(k) for k in n.kids):
                    n.kids = paragraphs(n.kids)
                elif n.tag in ("div", "section"):
                    n.tag = "p"  # a <div> holding only text is a paragraph
            out.append(n)
        elif isinstance(n, El) and n.tag == "br" and run and isinstance(run[-1], El) and run[-1].tag == "br":
            flush()  # two line breaks in a row: a paragraph break (MOBI, old HTML)
        else:
            run.append(n)
    flush()
    return [n for n in out if not (n.tag == "p" and not n.kids)]


def serialize(nodes, out=None):
    out = [] if out is None else out
    for n in nodes:
        if isinstance(n, str):
            out.append(html.escape(n, quote=False))
            continue
        attrs = "".join(f' {k}="{html.escape(v)}"' for k, v in n.attrs.items())
        out.append(f"<{n.tag}{attrs}>")
        if n.tag not in ("br", "hr", "img"):
            serialize(n.kids, out)
            out.append(f"</{n.tag}>")
    return out


def plain(nodes):
    return re.sub(r"\n\s*\n+", "\n", "".join(text_of(n) for n in nodes)).strip()


def section_title(x):
    """The title if this block starts a section: a heading, or a short paragraph like "第十二回 …" / "Chapter 3"
    (books converted from text often have no heading tags)."""
    if not isinstance(x, El):
        return ""
    if x.tag in ("h1", "h2", "h3", "h4"):
        return re.sub(r"\s+", " ", text_of(x)).strip()[:60]
    if x.tag == "p":
        t = re.sub(r"\s+", " ", text_of(x)).strip()
        if len(t) <= 40 and TXT_HEADING.match(t) and "。" not in t:
            return t
    return ""


def heading(nodes):
    for x in find_all(El("x", {}, nodes), lambda e: e.tag in ("h1", "h2", "h3", "h4", "p")):
        t = section_title(x)
        if t:
            return t
    return ""


def flatten(nodes):
    """A chapter wrapped in one container (a <div> around everything): its children, so it can be split."""
    while len([n for n in nodes if isinstance(n, El)]) == 1:
        only = next(n for n in nodes if isinstance(n, El))
        if only.tag not in ("div", "section") or not any(is_block(k) for k in only.kids):
            break
        nodes = only.kids
    return nodes


def split_big(nodes):
    """A long chapter in parts: at each section it has (a whole book in one file), else of about BIG_CHAPTER
    characters, cut between paragraphs."""
    nodes = flatten(nodes)
    total = len(re.sub(r"\s", "", plain(nodes)))
    if total <= BIG_CHAPTER * 1.3:
        return [nodes]
    sections = sum(bool(section_title(x)) for x in nodes) >= 3
    parts, cur, n = [], [], 0
    for x in nodes:
        size = len(re.sub(r"\s", "", text_of(x)))
        starts = bool(section_title(x)) and (sections or n > BIG_CHAPTER / 3)
        if cur and (n + size > BIG_CHAPTER or (starts and n > 0)):
            parts.append(cur)
            cur, n = [], 0
        cur.append(x)
        n += size
    if cur:
        parts.append(cur)
    return parts


def chapters_from(docs, names):
    """docs: [(path, html text)] in reading order -> chapters [{t, h, text, n, res}] and where each file and id ended up
    (for links and the table of contents)."""
    links = {p: True for p, _ in docs}
    chapters, where, ids = [], {}, {}
    for path, text in docs:
        c = Cleaner(path, names, links)
        nodes = paragraphs(c.clean(parse_html(text)))
        # a MOBI book is one file with page breaks between its chapters
        pieces, cur = [], []
        for n in nodes:
            if isinstance(n, El) and n.tag == "hr" and n.attrs.get("data-break"):
                pieces.append(cur)
                cur = []
            else:
                cur.append(n)
        pieces.append(cur)
        first = True
        for piece in pieces:
            for part in split_big(piece):
                body = plain(part)
                has_pic = bool(find_all(El("x", {}, part), lambda e: e.tag == "img"))
                if not body and not has_pic:
                    continue
                if first:
                    where[path] = len(chapters)
                    first = False
                for e in find_all(El("x", {}, part), lambda e: "id" in e.attrs):
                    ids.setdefault((path, e.attrs["id"][2:]), len(chapters))
                chapters.append({"t": heading(part), "h": "".join(serialize(part)), "text": body,
                                 "n": len(re.sub(r"\s", "", body)), "pic": has_pic})
        if first:  # nothing to read in this file (a blank page): links to it go to the next chapter
            where[path] = len(chapters)
    last = max(0, len(chapters) - 1)

    def go(m):
        path, _, frag = html.unescape(m.group(1)).partition("#")
        if path not in where:
            return ""
        i = ids.get((path, frag), min(where[path], last)) if frag else min(where[path], last)
        return f"{i}#x-{frag}" if (path, frag) in ids and re.fullmatch(r"[\w.:-]{1,80}", frag) else str(i)
    for ch in chapters:
        ch["h"] = re.sub(r"\x00([^\x00]*)\x00", go, ch["h"])
    return chapters, where, ids


# ---------------------------------------------------------------- formats

def xml_root(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # undeclared entities (&nbsp; in XHTML tables of contents): drop them and try again
        return ET.fromstring(re.sub(rb"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)\w+;", b" ", data))


def decode_html(data):
    m = re.search(rb'encoding=["\']([\w-]+)["\']', data[:200]) or re.search(rb'charset=["\']?([\w-]+)', data[:2000])
    for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "gb18030"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", "replace")


def read_epub(path):
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise BookError("文件坏了，不是完整的 EPUB")
    names = set(z.namelist())
    try:
        container = xml_root(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = xml_root(z.read(opf_path))
    except (KeyError, AttributeError, ET.ParseError):
        raise BookError("EPUB 里找不到目录文件（content.opf），文件可能不完整")
    if "META-INF/encryption.xml" in names:  # encrypted chapters are DRM (encrypted fonts are only obfuscation)
        uris = re.findall(rb'URI="([^"]+)"', z.read("META-INF/encryption.xml"))
        if any(u.lower().endswith((b".html", b".xhtml", b".htm")) for u in uris):
            raise BookError("这本书有 DRM 加密，打不开（需要先去掉加密）")
    base = posixpath.dirname(opf_path)

    def full(href):
        return posixpath.normpath(posixpath.join(base, urllib.parse.unquote(href)))
    manifest = {}
    for it in opf.iterfind(".//{*}item"):
        if it.get("href"):
            manifest[it.get("id")] = (full(it.get("href")), it.get("media-type", ""), it.get("properties", ""))
    meta = opf.find("{*}metadata")
    dc = lambda tag: [(e.text or "").strip() for e in (meta.iterfind(f".//{{*}}{tag}") if meta is not None else []) if (e.text or "").strip()]  # noqa: E731
    title, authors, lang = (dc("title") or [""])[0], dc("creator"), (dc("language") or [""])[0]
    # the spine: the reading order
    spine = opf.find("{*}spine")
    order = []
    for ref in (spine.iterfind("{*}itemref") if spine is not None else []):
        p, mt, _ = manifest.get(ref.get("idref"), (None, "", ""))
        if p and p in names and ("html" in mt or p.lower().endswith((".html", ".htm", ".xhtml"))):
            order.append(p)
    if not order:
        raise BookError("EPUB 里没有可以读的章节")
    docs = [(p, decode_html(z.read(p))) for p in dict.fromkeys(order)]
    chapters, where, ids = chapters_from(docs, names)
    if not chapters:
        raise BookError("EPUB 里没有文字也没有图片")
    # the table of contents: EPUB 3's nav document, else EPUB 2's NCX
    toc = []

    def add(label, href, level, rel_to):
        label = re.sub(r"\s+", " ", label or "").strip()
        if not label or not href:
            return
        p, _, frag = href.partition("#")
        p = posixpath.normpath(posixpath.join(posixpath.dirname(rel_to), urllib.parse.unquote(p))) if p else rel_to
        if p not in where:
            return
        i = ids.get((p, urllib.parse.unquote(frag)), where[p]) if frag else where[p]
        toc.append({"t": label[:100], "ch": min(i, len(chapters) - 1), "lv": min(level, 3)})
    nav = next((p for p, mt, props in manifest.values() if "nav" in props.split() and p in names), None)
    if nav:
        root = parse_html(decode_html(z.read(nav)))
        navs = find_all(root, lambda e: e.tag == "nav")
        toc_nav = next((n for n in navs if "toc" in n.attrs.get("epub:type", "")), navs[0] if navs else None)

        def walk(ol, level):
            for li in [k for k in ol.kids if isinstance(k, El) and k.tag == "li"]:
                a = next(iter(find_all(li, lambda e: e.tag in ("a", "span"))), None)
                if a is not None:
                    add(text_of(a), a.attrs.get("href", ""), level, nav)
                for sub in [k for k in li.kids if isinstance(k, El) and k.tag == "ol"]:
                    walk(sub, level + 1)
        if toc_nav is not None:
            for ol in [k for k in toc_nav.kids if isinstance(k, El) and k.tag == "ol"]:
                walk(ol, 0)
    if not toc and spine is not None and spine.get("toc") in manifest:
        ncx_path = manifest[spine.get("toc")][0]
        try:
            ncx = xml_root(z.read(ncx_path))

            def walk_ncx(el, level):
                for pt in el.findall("{*}navPoint"):
                    label = pt.find("{*}navLabel/{*}text")
                    content = pt.find("{*}content")
                    add(label.text if label is not None else "", content.get("src", "") if content is not None else "", level, ncx_path)
                    walk_ncx(pt, level + 1)
            nav_map = ncx.find("{*}navMap")
            if nav_map is not None:
                walk_ncx(nav_map, 0)
        except (KeyError, ET.ParseError):
            pass
    # the cover picture
    cover = None
    meta_cover = next((m.get("content") for m in (meta.iterfind(".//{*}meta") if meta is not None else []) if m.get("name") == "cover"), None)
    for p, mt, props in ([manifest[meta_cover]] if meta_cover in manifest else []) + \
            [v for v in manifest.values() if "cover-image" in v[2].split()]:
        if p in names and (mt.startswith("image/") or Path(p).suffix.lower() in IMAGE_TYPES):
            cover = p
            break
    if not cover:
        first = re.search(r'data-res="([^"]+)"', chapters[0]["h"])
        cover = html.unescape(first.group(1)) if first else None
    if not cover:
        cover = next((n for n in names if "cover" in n.lower() and Path(n).suffix.lower() in IMAGE_TYPES), None)
    return {"view": "flow", "title": title, "author": "、".join(dict.fromkeys(authors[:3])), "lang": lang,
            "chapters": chapters, "toc": toc, "cover": z.read(cover) if cover else None, "zip": path}


def decode_text(data, probe=False):
    """Text in whatever encoding Chinese TXT books come in: UTF-8 (with or without BOM), UTF-16, GB18030/GBK, Big5."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        if probe and e.start > len(data) - 4:
            return data[:e.start].decode("utf-8")  # cut in the middle of a character
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data[:200000]).best()
        if best and best.encoding and best.encoding not in ("ascii", "utf_8"):
            enc = "gb18030" if best.encoding in ("gb2312", "gbk", "gb18030", "hz") else best.encoding
            return data.decode(enc, "replace" if not probe else "strict")
    except Exception:
        pass
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError:
        return None if probe else data.decode("gb18030", "replace")


NUM = "0-9０-９零〇一二两三四五六七八九十百千万"
TXT_HEADING = re.compile(
    rf"^\s*(?:第\s*[{NUM}]+\s*[章回节卷集部篇]|[卷部]\s*[{NUM}]+\b|(?:正文\s*)?(?:序章|序言|序|楔子|引子|前言|后记|尾声|终章|番外|附录|完本感言)"
    rf"(?=$|[\s:：·\-—_（(])|(?:chapter|part|book)\s+(?:[0-9]+|[ivxlc]+)\b)[^\n]{{0,40}}$", re.I)
VOLUME = re.compile(rf"^\s*(?:第\s*[{NUM}]+\s*[卷集部篇]|[卷部]\s*[{NUM}]+|(?:part|book)\s)", re.I)


def unwrap(lines, lang):
    """Paragraphs of a TXT. Usually one line is one paragraph. Some books (Project Gutenberg's, old scans) break lines
    at a fixed width instead, mid-sentence: there a line runs on into the next unless the next is indented, a blank
    line comes, or the line is clearly short (a paragraph's last line). Widths are in GBK bytes, the way those
    books were wrapped (a Chinese character is two)."""
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        return []
    widths = [len(l.strip().encode("gb18030", "replace")) for l in nonblank]
    counts = {}
    for w in widths:
        counts[w] = counts.get(w, 0) + 1
    modal = max(counts, key=counts.get)
    full = sum(n for w, n in counts.items() if modal - 8 <= w <= modal + 2)
    if modal < 40 or full < len(nonblank) * 0.4:
        return [l.strip() for l in nonblank]
    indent = re.compile(r"^(\u3000|\s{2,}|\t)")
    uses_indent = sum(bool(indent.match(l)) for l in nonblank) > len(nonblank) * 0.03

    def join(parts):  # Chinese runs straight on; words in Latin script keep a space between them
        out = parts[0]
        for p in parts[1:]:
            out += (" " if re.match(r"[\w,.;:!?'\")\]-]", out[-1:], re.A) and re.match(r"[\w(\[\"']", p, re.A) else "") + p
        return out
    paras, cur, prev_short = [], [], True
    for l in lines:
        if not l.strip():
            prev_short = True
            continue
        text = l.strip()
        head = len(text) <= 30 and TXT_HEADING.match(text)  # a chapter heading is a line of its own
        if cur and (prev_short or head or (uses_indent and indent.match(l))):
            paras.append(join(cur))
            cur = []
        cur.append(text)
        prev_short = head or len(text.encode("gb18030", "replace")) < modal * 0.7
    if cur:
        paras.append(join(cur))
    return paras


def read_txt(path, title_hint=""):
    data = Path(path).read_bytes()
    text = decode_text(data)
    if not text or not text.strip():
        raise BookError("TXT 是空的")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("　", "  ")
    lines = text.split("\n")
    title = re.sub(r"[《》]|[（(\[【].*?[）)\]】]|作者\s*[:：].*$", "", title_hint).strip() or "未命名"
    m = re.search(r"作者\s*[:：]\s*([^\n]{1,30})", "\n".join(lines[:40]))
    author = m.group(1).strip() if m else ""
    cjk = len(re.findall(rf"[{CJK}]", text[:20000]))
    lang = "zh" if cjk > len(text[:20000]) * 0.2 else "en"
    paras = unwrap(lines, lang)
    # chapters by their headings ("第十二章 …", "Chapter 3"); a volume heading leads into the chapters under it
    heads = [i for i, p in enumerate(paras) if len(p) <= 50 and TXT_HEADING.match(p) and "。" not in p
             and not p.endswith(("，", "、", ",", "；"))]
    sections = []
    if len(heads) >= 2:
        if heads[0] > 0:
            sections.append(("前言" if lang == "zh" else "Front matter", paras[:heads[0]], 0))
        for k, i in enumerate(heads):
            end = heads[k + 1] if k + 1 < len(heads) else len(paras)
            sections.append((paras[i], paras[i + 1:end], 1 if not VOLUME.match(paras[i]) else 0))
    else:  # no headings: parts of about 5,000 characters
        cur, n = [], 0
        for p in paras:
            cur.append(p)
            n += len(p)
            if n >= 5000:
                sections.append((f"第 {len(sections) + 1} 部分" if lang == "zh" else f"Part {len(sections) + 1}", cur, 1))
                cur, n = [], 0
        if cur:
            sections.append((f"第 {len(sections) + 1} 部分" if lang == "zh" else f"Part {len(sections) + 1}", cur, 1))
    chapters, toc, has_volumes = [], [], any(VOLUME.match(paras[i]) for i in heads)
    pending = []  # volume headings with nothing under them yet
    for head, body, lv in sections:
        if not body:
            pending.append(head)
            continue
        for pv in pending:
            toc.append({"t": pv[:100], "ch": len(chapters), "lv": 0})
        hs = pending + [head]
        pending = []
        # a giant chapter (or a book without headings) still gets cut into readable lengths
        pieces, cur, n = [], [], 0
        for p in body:
            cur.append(p)
            n += len(p)
            if n >= BIG_CHAPTER:
                pieces.append(cur)
                cur, n = [], 0
        if cur or not pieces:
            pieces.append(cur)
        for k, piece in enumerate(pieces):
            h = "".join(f"<h2>{html.escape(x, False)}</h2>" for x in (hs if k == 0 else [])) + \
                "".join(f"<p>{html.escape(p, False)}</p>" for p in piece)
            if k == 0:
                toc.append({"t": head[:100], "ch": len(chapters), "lv": 1 if has_volumes and lv else 0})
            chapters.append({"t": head if k == 0 else f"{head}（{k + 1}）", "h": h, "text": "\n".join(piece),
                             "n": len(re.sub(r"\s", "", "\n".join(piece)))})
    if not chapters:
        raise BookError("TXT 里没有文字")
    return {"view": "flow", "title": title, "author": author, "lang": lang, "chapters": chapters, "toc": toc,
            "cover": None, "zip": None}


def read_pdf(path):
    import pymupdf
    try:
        doc = pymupdf.open(path)
    except Exception:
        raise BookError("PDF 打不开，文件可能坏了")
    if doc.needs_pass:
        raise BookError("PDF 有密码，打不开")
    if not doc.page_count:
        raise BookError("PDF 一页也没有")
    meta = doc.metadata or {}
    pages, sizes = [], []
    for page in doc:
        t = page.get_text("text").strip()
        pages.append({"t": "", "text": t, "n": len(re.sub(r"\s", "", t))})
        sizes.append([round(page.rect.width, 1), round(page.rect.height, 1)])
    toc = [{"t": str(t)[:100], "ch": max(0, min(p - 1, len(pages) - 1)), "lv": min(lv - 1, 3)}
           for lv, t, p, *_ in doc.get_toc(simple=True) if str(t).strip()]
    for e in toc:  # each page is named after the section it's in, for search results
        if not pages[e["ch"]]["t"]:
            pages[e["ch"]]["t"] = e["t"]
    cur = ""
    for i, p in enumerate(pages):
        cur = p["t"] or cur
        p["t"] = cur or f"第 {i + 1} 页"
    zoom = 600 / max(doc[0].rect.height, 1)
    cover = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
    scanned = sum(p["n"] for p in pages) < len(pages) * 20
    return {"view": "pdf", "title": (meta.get("title") or "").strip(), "author": (meta.get("author") or "").strip(),
            "lang": "", "chapters": pages, "toc": toc, "cover": cover, "sizes": sizes, "scanned": scanned}


def read_mobi(path, bid):
    """MOBI / AZW3 (Kindle) through KindleUnpack: AZW3 comes out as an EPUB, old MOBI as one HTML file with page
    breaks between chapters, a few as a PDF. The converted copy is kept next to the original (pictures come from it)."""
    import contextlib
    import mobi
    try:
        with contextlib.redirect_stdout(io.StringIO()):  # KindleUnpack prints every step
            tmp, out = mobi.extract(str(path))
    except Exception as e:
        if "drm" in str(e).lower() or "encrypt" in str(e).lower():
            raise BookError("这本书有 Kindle DRM 加密，打不开（需要先去掉加密）")
        raise BookError(f"Kindle 格式解不开：{str(e)[:100]}")
    try:
        if out.endswith(".epub"):
            conv = BOOKS_DIR / f"{bid}.conv.epub"
            shutil.copy(out, conv)
            return read_epub(conv)
        if out.endswith(".pdf"):
            conv = BOOKS_DIR / f"{bid}.conv.pdf"
            shutil.copy(out, conv)
            return {**read_pdf(conv), "conv": conv.name}
        folder = Path(out).parent
        conv = BOOKS_DIR / f"{bid}.conv.zip"
        with zipfile.ZipFile(conv, "w", zipfile.ZIP_STORED) as z:
            for f in folder.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(folder).as_posix())
        names = set(zipfile.ZipFile(conv).namelist())
        doc = decode_html(Path(out).read_bytes())
        title = re.search(r"<title>(.*?)</title>", doc, re.S | re.I)
        author = re.search(r'<meta[^>]+name="author"[^>]+content="([^"]*)"', doc, re.I)
        chapters, _, _ = chapters_from([(Path(out).name, doc)], names)
        if not chapters:
            raise BookError("Kindle 书里没有文字")
        cover = next((n for n in sorted(names) if "cover" in n.lower() and Path(n).suffix.lower() in IMAGE_TYPES), None)
        opf = folder / "content.opf"  # KindleUnpack writes the book's metadata here
        if opf.exists():
            meta = xml_root(opf.read_bytes())
            title = meta.find(".//{*}title")
            author = meta.find(".//{*}creator")
            lang = meta.find(".//{*}language")
            title, author, lang = [(e.text or "").strip() if e is not None else "" for e in (title, author, lang)]
        else:
            title = html.unescape(title.group(1)).strip() if title else ""
            author, lang = html.unescape(author.group(1)).strip() if author else "", ""
        return {"view": "flow", "title": title, "author": author, "lang": lang, "chapters": chapters, "toc": [],
                "cover": zipfile.ZipFile(conv).read(cover) if cover else None, "zip": conv}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- importing

def res_zip(row):
    """The zip a flow book's pictures come from: its EPUB, or the copy a Kindle book was converted to."""
    for name in (f"{row['id']}.conv.epub", f"{row['id']}.conv.zip", row["file"] if row["fmt"] == "epub" else None):
        if name and (BOOKS_DIR / name).exists():
            return BOOKS_DIR / name
    return None


def pdf_file(row):
    conv = BOOKS_DIR / f"{row['id']}.conv.pdf"
    return conv if conv.exists() else BOOKS_DIR / row["file"]


def save_cover(bid, data):
    out = path_of(bid, "cover")
    if not data:
        out.unlink(missing_ok=True)
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((600, 900))
            im.save(out, "JPEG", quality=85)
        return True
    except Exception:
        return False


def fetch(row):
    """A book added by its link: download it (through the Pi's proxy like everything else)."""
    import requests
    BOOKS_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = BOOKS_DIR / f"{row['id']}.part"
    with requests.get(row["url"], stream=True, timeout=(15, 120), headers={"User-Agent": UA}) as r:
        if r.status_code >= 400:
            raise BookError(f"下载失败：网站回答 HTTP {r.status_code}")
        name = urllib.parse.unquote(urllib.parse.urlsplit(r.url).path.rsplit("/", 1)[-1])
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
        if m:
            name = urllib.parse.unquote(m.group(1))
        size, sha = 0, hashlib.sha1()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > DOWNLOAD_MAX:
                    raise BookError("文件超过 500 MB，不像是电子书")
                sha.update(chunk)
                f.write(chunk)
    with open(tmp, "rb") as f:
        fmt = sniff(f.read(8192), name)
    if not fmt:
        tmp.unlink(missing_ok=True)
        raise BookError("下载到的不是电子书（可能是网页或要登录才能下载）")
    ext = next(e for e, v in BOOK_EXT.items() if v == fmt)
    final = f"{row['id']}{ext}"
    tmp.rename(BOOKS_DIR / final)
    title = row["title"] or safe_name(Path(name).stem, 80)
    q("UPDATE books SET file=?, fmt=?, size=?, sha1=?, title=?, updated=? WHERE id=?",
      (final, fmt, size, sha.hexdigest(), title, time.time(), row["id"]))
    return q("SELECT * FROM books WHERE id=?", (row["id"],), one=True)


def import_book(bid, beat=None):
    """Read the book's file into chapters / pages, its text, the pack and the cover. Raises BookError when the file
    isn't readable (the shelf shows why)."""
    row = q("SELECT * FROM books WHERE id=?", (bid,), one=True)
    if not row:
        raise BookError(f"没有这本书 {bid}")
    if not row["file"]:
        row = fetch(row)
    path = BOOKS_DIR / row["file"]
    if not path.exists():
        raise BookError("书的文件不见了")
    for old in BOOKS_DIR.glob(f"{bid}.conv.*"):  # from an earlier file of this book
        old.unlink()
    fmt = row["fmt"]
    if fmt == "epub":
        book = read_epub(path)
    elif fmt == "txt":
        book = read_txt(path, row["title"])
    elif fmt == "pdf":
        book = read_pdf(path)
    elif fmt in ("mobi", "azw3"):
        book = read_mobi(path, bid)
    else:
        raise BookError(f"不认识的格式 {fmt}")
    if beat:
        beat({"pct": 80})
    chapters = book["chapters"]
    for i, ch in enumerate(chapters):  # untitled chapters: after the table of contents, a short first line, a number
        if not ch["t"]:
            entry = next((e for e in book["toc"] if e["ch"] == i), None)
            line = ch["text"].strip().split("\n", 1)[0].strip()
            ch["t"] = entry["t"] if entry else ("封面" if i == 0 and ch.get("pic") and not ch["n"] else
                                                line if 0 < len(line) <= 30 else f"第 {i + 1} 节")
    if not book["lang"] and book["view"] == "flow":
        sample = "".join(ch["text"] for ch in chapters[:20])[:20000]
        book["lang"] = "zh" if len(re.findall(rf"[{CJK}]", sample)) > len(sample) * 0.2 else "en"
    q("DELETE FROM book_ch WHERE book=?", (bid,))
    with db_lock:
        core.DB.executemany("INSERT INTO book_ch (book, idx, title, text, chars) VALUES (?,?,?,?,?)",
                            [(bid, i, ch["t"], ch["text"], ch["n"]) for i, ch in enumerate(chapters)])
        core.DB.commit()
    toc = book["toc"]
    if book["view"] == "flow" and len(toc) < min(3, len(chapters)):  # none, or only a stray entry: the chapters
        toc = [{"t": ch["t"], "ch": i, "lv": 0} for i, ch in enumerate(chapters) if ch["n"]]
    version = row["version"]
    if book["view"] == "flow":
        pack = {"id": bid, "v": version, "view": "flow", "lang": book["lang"], "toc": toc,
                "chapters": [{"t": ch["t"], "h": ch["h"], "n": ch["n"]} for ch in chapters],
                "res": sorted({html.unescape(m) for ch in chapters for m in re.findall(r'data-res="([^"]+)"', ch["h"])})}
    else:
        sizes = book["sizes"]
        pack = {"id": bid, "v": version, "view": "pdf", "toc": toc, "pages": len(chapters),
                "sizes": sizes if len({tuple(s) for s in sizes}) > 1 else sizes[:1], "chars": [ch["n"] for ch in chapters],
                "scanned": book.get("scanned", False)}
    data = gzip.compress(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode(), 6)
    tmp = path_of(bid, "pack").with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.rename(path_of(bid, "pack"))
    cover = save_cover(bid, book["cover"])
    first = row["chars"] == 0 and row["version"] == 1
    title = (book["title"] if first and book["title"] else row["title"]) or book["title"] or "未命名"
    author = (book["author"] if first and book["author"] else row["author"]) or book["author"]
    q("UPDATE books SET title=?, author=?, lang=?, view=?, cover=?, chars=?, chapters=?, toc=?, status='ready', error='', "
      "updated=? WHERE id=?", (title[:200], author[:100], book["lang"][:10], book["view"], int(cover),
                               sum(ch["n"] for ch in chapters), len(chapters), json.dumps(toc, ensure_ascii=False),
                               time.time(), bid))
    return {"view": book["view"], "chapters": len(chapters), "chars": sum(ch["n"] for ch in chapters),
            "pack_kb": len(data) // 1024}


def add_file(owner, device, f):
    """An uploaded file -> a book row (importing), or the id of the same file already on this shelf."""
    BOOKS_DIR.mkdir(mode=0o700, exist_ok=True)
    name = f.filename or "book"
    tmp = tempfile.NamedTemporaryFile(dir=BOOKS_DIR, delete=False, suffix=".up")
    f.save(tmp)
    tmp.close()
    tmp = Path(tmp.name)
    with open(tmp, "rb") as fh:
        fmt = sniff(fh.read(8192), name)
    if not fmt:
        tmp.unlink()
        return None, "不是电子书"
    sha = sha1_of(tmp)
    dup = q("SELECT id FROM books WHERE owner=? AND sha1=?", (owner, sha), one=True)
    if dup:
        tmp.unlink()
        return dup["id"], "duplicate"
    now = time.time()
    bid = insert("INSERT INTO books (owner, title, fmt, size, sha1, device, created, updated) VALUES (?,?,?,?,?,?,?,?)",
                 (owner, safe_name(Path(name).stem, 80), fmt, tmp.stat().st_size, sha, device, now, now))
    ext = next(e for e, v in BOOK_EXT.items() if v == fmt)
    tmp.rename(BOOKS_DIR / f"{bid}{ext}")
    q("UPDATE books SET file=? WHERE id=?", (f"{bid}{ext}", bid))
    return bid, "new"


def add_url(owner, device, url):
    """A link to an e-book file -> a book row; the import task downloads it. The same link twice is one book."""
    dup = q("SELECT id FROM books WHERE owner=? AND url=?", (owner, url), one=True)
    if dup:
        return dup["id"], "duplicate"
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    now = time.time()
    return insert("INSERT INTO books (owner, title, fmt, url, device, created, updated) VALUES (?,?,?,?,?,?,?)",
                  (owner, safe_name(Path(name).stem, 80) if Path(name).stem else "", book_fmt(name) or "", url, device,
                   now, now)), "new"


def replace_file(row, f):
    """A newer file of the same book (a TXT novel that has new chapters): read again, the reading position kept."""
    tmp = tempfile.NamedTemporaryFile(dir=BOOKS_DIR, delete=False, suffix=".up")
    f.save(tmp)
    tmp.close()
    tmp = Path(tmp.name)
    with open(tmp, "rb") as fh:
        fmt = sniff(fh.read(8192), f.filename or "")
    if not fmt:
        tmp.unlink()
        return "不是电子书"
    if row["file"]:
        (BOOKS_DIR / row["file"]).unlink(missing_ok=True)
    ext = next(e for e, v in BOOK_EXT.items() if v == fmt)
    name = f"{row['id']}{ext}"
    sha = sha1_of(tmp)
    tmp.rename(BOOKS_DIR / name)
    q("UPDATE books SET file=?, fmt=?, size=?, sha1=?, status='importing', error='', version=version+1, updated=? WHERE id=?",
      (name, fmt, (BOOKS_DIR / name).stat().st_size, sha, time.time(), row["id"]))
    return None


def remove(row):
    for f in BOOKS_DIR.glob(f"{row['id']}.*"):
        f.unlink(missing_ok=True)
    for t in ("book_ch WHERE book", "book_read WHERE book", "book_marks WHERE book"):
        q(f"DELETE FROM {t}=?", (row["id"],))
    q("DELETE FROM activity WHERE kind='book' AND ref=?", (row["id"],))
    q("DELETE FROM books WHERE id=?", (row["id"],))


# ---------------------------------------------------------------- the shelf

def book_dict(r, read=None):
    d = {k: r[k] for k in ("id", "title", "author", "fmt", "view", "size", "chars", "chapters", "status", "error",
                           "shelf", "version", "created", "updated", "url", "device")}
    d["cover"] = bool(r["cover"])
    if read:
        d["read"] = {"pct": read["pct"] or 0, "done": bool(read["done"]), "at": read["updated"],
                     "seconds": read["seconds"] or 0, "pos": json.loads(read["pos"] or "null")}
    return d


_cc = {}


def variants(term):
    """The words as typed and in simplified / traditional characters (old e-books are often traditional: 寶玉)."""
    out = [term]
    try:
        from opencc import OpenCC
        for conv in ("s2t", "t2s"):
            cc = _cc.setdefault(conv, OpenCC(conv))
            out.append(cc.convert(term))
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _find(text, terms, start=0):
    """(position, the variant found) of the first of `terms` in `text` from `start`, or (-1, None)."""
    low = text.lower()
    best = (-1, None)
    for t in terms:
        i = low.find(t.lower(), start)
        if i >= 0 and (best[0] < 0 or i < best[0]):
            best = (i, text[i:i + len(t)])
    return best


def _snippet(text, terms):
    i, found = _find(text, terms)
    if i < 0:
        return text[:60], None
    start = max(0, i - 20)
    return ("…" if start else "") + text[start:i + len(found) + 50].replace("\n", " ") + "…", found


def search(owner, term, limit=200):
    """{book id: [{"ch", "title", "text", "match"}]} for the words in the books' text, plus books whose title / author
    match (with no places)."""
    terms = variants(term)
    likes = [f"%{t}%" for t in terms]
    any_of = lambda col: "(" + " OR ".join(f"{col} LIKE ?" for _ in terms) + ")"  # noqa: E731
    out = {r["id"]: [] for r in q(f"SELECT id FROM books WHERE owner=? AND ({any_of('title')} OR {any_of('author')})",
                                  (owner, *likes, *likes))}
    for r in q(f"SELECT c.book, c.idx, c.title, c.text FROM book_ch c JOIN books b ON b.id = c.book "
               f"WHERE b.owner=? AND {any_of('c.text')} ORDER BY c.book, c.idx LIMIT ?", (owner, *likes, limit * 4)):
        hits = out.setdefault(r["book"], [])
        if len(hits) < 30:
            text, found = _snippet(r["text"], terms)
            hits.append({"ch": r["idx"], "title": r["title"], "text": text, "match": found})
    return out


def search_in(bid, term, limit=300):
    """Every place in one book (chapters / pages) where the words are; `nth`: which occurrence in its chapter."""
    terms = variants(term)
    out = []
    for r in q(f"SELECT idx, title, text FROM book_ch WHERE book=? AND ({' OR '.join('text LIKE ?' for _ in terms)}) ORDER BY idx",
               (bid, *[f"%{t}%" for t in terms])):
        start, nth = 0, 0
        while len(out) < limit:
            i, found = _find(r["text"], terms, start)
            if i < 0:
                break
            s = max(0, i - 20)
            out.append({"ch": r["idx"], "title": r["title"], "match": found, "nth": nth,
                        "text": ("…" if s else "") + r["text"][s:i + len(found) + 40].replace("\n", " ") + "…"})
            start, nth = i + len(found), nth + 1
    return out
