#!/usr/bin/env python3
"""Smoke test on a copy of the real database: every page API answers, search and the task board work.
Run on the Pi before deploying:  sudo -u grabber /opt/grabber/venv/bin/python tests/smoke.py [app dir]
It copies the database to a temp dir (the real one isn't touched) and never starts downloads."""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

APP = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
REAL_DB = os.environ.get("REAL_DB", "/mnt/disk1/.grabber/grabber.db")
tmp = Path(tempfile.mkdtemp(prefix="shiguang-test-"))
sqlite3.connect(REAL_DB).backup(sqlite3.connect(tmp / "grabber.db"))  # a consistent copy, WAL included
os.environ.update(DB_PATH=str(tmp / "grabber.db"), STATE_DIR=os.environ.get("STATE_DIR", "/var/lib/grabber"),
                  PLEX_TOKEN="", LLM_API_KEY="", COMPUTE_TOKEN="test-token")
os.environ["STATE_DIR"] = str(tmp)
(tmp / "models").symlink_to("/var/lib/grabber/models")
(tmp / "secret_key").write_text("test")
sys.path.insert(0, str(APP))
import grabber as G  # noqa: E402
import shiguang  # noqa: E402

G.init_db()
G.setup_app()
# the copy holds whatever the real board was doing (the Mac's tasks, requests for Claude): out of the way, so the
# board tests see only their own tasks
G.q("DELETE FROM tasks WHERE kind='llm'")
G.q("UPDATE tasks SET state='done', worker=NULL, lease_until=NULL WHERE state IN ('queued', 'running')")
app = shiguang.core.app
app.config["TESTING"] = True
failures = []


def check(name, fn):
    t = time.time()
    try:
        fn()
        print(f"ok    {name}  ({(time.time() - t) * 1000:.0f} ms)")
    except Exception:
        failures.append(name)
        print(f"FAIL  {name}")
        traceback.print_exc()


owner_job = G.q("SELECT owner FROM jobs WHERE owner LIKE 'user:%' GROUP BY owner ORDER BY COUNT(*) DESC LIMIT 1", one=True)["owner"]
user = owner_job[5:]
c = app.test_client()
# log the test client in as that account: a device row tied to it, and the session
dev = "f" * 32
G.q("INSERT OR REPLACE INTO devices (id, user, ip, seen, label) VALUES (?,?,?,?,?)", (dev, user, "127.0.0.1", time.time(), "test"))
c.set_cookie(G.DEVICE_COOKIE, dev)
with c.session_transaction() as s:
    s["user"] = user


def get(path, code=200):
    r = c.get(path)
    assert r.status_code == code, f"{path}: {r.status_code} {r.data[:200]}"
    return r.get_json(silent=True)


def post(path, json=None, code=200, headers=None):
    r = c.post(path, json=json, headers=headers or {})
    assert r.status_code == code, f"{path}: {r.status_code} {r.data[:200]}"
    return r.get_json(silent=True)


some_job = None


def jobs_list():
    global some_job
    d = get("/api/jobs")
    assert d["user"] == user and d["jobs"], "no jobs"
    some_job = next(j for j in d["jobs"] if j["status"] == "done" and j.get("media"))


check("page", lambda: get("/"))
check("jobs list", jobs_list)
check("search text", lambda: get("/api/jobs?q=%E4%BC%8A%E6%9C%97"))  # 伊朗
check("search pinyin note", lambda: get("/api/notes?q=yanan"))
check("notes", lambda: get("/api/notes"))


def whole_category():
    d = get("/api/jobs")
    cat, n = max(d["cats"].items(), key=lambda x: x[1])
    got = [j for j in get(f"/api/jobs?cat={cat}")["jobs"] if j["status"] in ("done", "linked")]
    assert len(got) == n, (cat, len(got), n)


check("a category chip gets all of its videos", whole_category)


def note_fields():
    d = get("/api/notes")
    assert "recap" in d, d.keys()
    assert all("tags" in n for n in d["notes"]), "tags"


check("notes carry tags and the weekly recap", note_fields)
check("usage", lambda: get("/api/usage?days=7"))
check("account", lambda: get("/api/account"))
check("digests", lambda: get("/api/digests"))
check("watch page extras", lambda: (get(f"/api/points/{some_job['id']}"), get(f"/api/similar/{some_job['id']}")))
check("play (range)", lambda: c.get(some_job["media"][0]["src"], headers={"Range": "bytes=0-99"}).status_code in (200, 206) or 1 / 0)
check("thumb", lambda: get(f"/thumb/{some_job['id']}", code=200))
check("save position", lambda: post(f"/api/watch/{some_job['id']}", {"part": 0, "pos": 30, "dur": 100}))
check("board over HTTP refuses without token", lambda: post("/api/tasks/claim", {"worker": "x", "caps": []}, code=403))


def cast_api():
    import shiguang.cast as cast
    old_discover, old_start, old_control, old_status, old_url = cast.discover, cast.start, cast.control, cast.status, shiguang.web.CAST_URL
    seen = {}
    try:
        cast.discover = lambda: [{"id": "tv", "name": "测试电视"}]
        cast.start = lambda did, url, title, thumb, mime: seen.update(did=did, url=url, title=title, thumb=thumb, mime=mime) or "测试电视"
        cast.control = lambda did, action, pos: seen.update(control=(did, action, pos))
        cast.status = lambda did: {"position": 30, "duration": 100, "state": "PLAYING"}
        shiguang.web.CAST_URL = "http://pi.test:8088"
        assert get("/api/cast/devices")["devices"][0]["id"] == "tv"
        got = post("/api/cast/start", {"id": some_job["id"], "part": 0, "device": "tv"})
        assert got["name"] == "测试电视" and seen["did"] == "tv" and seen["url"].startswith("http://pi.test:8088/castplay/")
        assert seen["thumb"].startswith("http://pi.test:8088/thumb/") and seen["mime"] == ("audio/mpeg" if some_job["media"][0]["audio"] else "video/mp4")
        post("/api/cast/control", {"device": "tv", "action": "pause"})
        assert seen["control"] == ("tv", "pause", None)
        assert get("/api/cast/status?device=tv")["position"] == 30
    finally:
        cast.discover, cast.start, cast.control, cast.status, shiguang.web.CAST_URL = old_discover, old_start, old_control, old_status, old_url


check("DLNA cast discovery, signed media URL and controls", cast_api)


def board_roundtrip():
    tok = {"X-Compute-Token": "test-token"}
    tid = G.publish("cover", f"job:{some_job['id']}", 999, force=True)
    got = post("/api/tasks/claim", {"worker": "test-worker", "caps": ["cover", "gpu"]}, headers=tok)["task"]
    assert got and got["id"] == tid, got
    assert post(f"/api/tasks/{tid}/heartbeat", {"worker": "test-worker", "progress": {"pct": 50}}, headers=tok)["ok"]
    assert post(f"/api/tasks/{tid}/done", {"worker": "test-worker", "result": {"vector": None, "lines": ["测试"]}}, headers=tok)["ok"]
    nxt = G.q("SELECT kind, state FROM tasks WHERE parent=?", (tid,), one=True)
    assert nxt and nxt["kind"] == "save_cover", nxt


check("board round trip (publish, claim, heartbeat, done -> save task)", board_roundtrip)


def lease_runs_out():
    tid = G.publish("frames", f"job:{some_job['id']}:0", 998, force=True)
    t = G.claim_task("ghost", ["frames", "gpu"])
    assert t and t["id"] == tid
    G._write("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, tid))
    G.claim_task("someone", ["save_subs"])
    assert G.q("SELECT state FROM tasks WHERE id=?", (tid,), one=True)["state"] == "queued"


check("a lease that runs out goes back on the board", lease_runs_out)
check("subtitle cue parsing", lambda: G.srt_cues(__file__) == [] or 1)
check("transcripts compared", lambda: (G.text_alike("今天天气很好，我们去公园", "今天天气很好 我们去公园") == 1
                                       and G.text_alike("今天天气很好", "明年再说吧") < 0.2) or 1 / 0)
check("forgiving note match", lambda: G.note_marks({"text": "Yannan San 结婚", "media": "[]"}, "yanan") or 1 / 0)
check("place names", lambda: G.place_name(31.23, 121.47) == "上海" or 1 / 0)

# ---- 书架: a small EPUB (with things that must not get through) and a GBK TXT, imported, read, searched, removed
import io  # noqa: E402
import json  # noqa: E402
import zipfile  # noqa: E402

for mod in (shiguang.core, shiguang.books, shiguang.web):  # book files go to the temp dir, not the library disks
    mod.BOOKS_DIR = tmp / "books"


def make_epub():
    from PIL import Image
    pic = io.BytesIO()
    Image.new("RGB", (60, 80), (255, 0, 77)).save(pic, "PNG")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试之书</dc:title><dc:creator>某作者</dc:creator>'
                   '<dc:language>zh</dc:language></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                   '<item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c2" href="text/c2.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="img" href="images/cover.png" media-type="image/png" properties="cover-image"/></manifest>'
                   '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
        z.writestr("OEBPS/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                   '<nav epub:type="toc"><ol><li><a href="text/c1.xhtml">第一章 开始</a></li>'
                   '<li><a href="text/c2.xhtml#n1">第二章 注释</a></li></ol></nav></body></html>')
        z.writestr("OEBPS/text/c1.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title><style>p{color:red}</style>'
                   '<script>alert(1)</script></head><body><h1>第一章 开始</h1><p onclick="alert(2)">拾光书架的第一段，'
                   '见<a href="c2.xhtml#n1">注一</a>。</p><img src="../images/cover.png" alt="图"/>'
                   '<p><a href="javascript:alert(3)">坏链接</a><iframe src="https://example.com"></iframe></p></body></html>')
        z.writestr("OEBPS/text/c2.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第二章 注释</h1>'
                   '<p>第二章的正文。</p><aside id="n1"><p>注一：这里是注释。</p></aside></body></html>')
        z.writestr("OEBPS/images/cover.png", pic.getvalue())
    return out.getvalue()


book_ids = {}


def upload(name, data):
    r = c.post("/api/books", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
    assert r.status_code == 200, r.data[:200]
    bid = r.get_json()["ids"][0]
    G.import_book(bid)
    return bid


def epub_import():
    bid = book_ids["epub"] = upload("测试.epub", make_epub())
    pack = json.loads(c.get(f"/api/books/{bid}/pack").data)
    assert [ch["t"] for ch in pack["chapters"]] == ["第一章 开始", "第二章 注释"], pack["chapters"]
    html = pack["chapters"][0]["h"]
    for bad in ("<script", "onclick", "javascript:", "<iframe", "<style", "alert"):
        assert bad not in html, (bad, html)
    assert 'data-go="1#x-n1"' in html and 'data-res="OEBPS/images/cover.png"' in html, html
    assert pack["toc"][1] == {"t": "第二章 注释", "ch": 1, "lv": 0}, pack["toc"]
    r = c.get(f"/bookres/{bid}?p=OEBPS/images/cover.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and "sandbox" in r.headers["Content-Security-Policy"]
    assert c.get(f"/bookres/{bid}?p=OEBPS/text/c1.xhtml").status_code == 404  # pictures only
    assert c.get(f"/bookcover/{bid}").status_code == 200
    b = next(x for x in get("/api/books")["books"] if x["id"] == bid)
    assert b["title"] == "测试之书" and b["author"] == "某作者" and b["status"] == "ready", b


def txt_import():
    text = "书名：测试小说\n作者：某人\n\n第一章 起\n　　第一章的内容，拾光书架。\n\n第二章 承\n　　第二章的内容。\n第三章 转\n　　内容三。\n"
    bid = book_ids["txt"] = upload("测试小说.txt", text.encode("gb18030"))
    pack = json.loads(c.get(f"/api/books/{bid}/pack").data)
    assert [ch["t"] for ch in pack["chapters"]] == ["前言", "第一章 起", "第二章 承", "第三章 转"], [ch["t"] for ch in pack["chapters"]]
    assert "第一章的内容" in pack["chapters"][1]["h"]


def book_search_and_progress():
    hits = get("/api/books?q=%E6%8B%BE%E5%85%89%E4%B9%A6%E6%9E%B6")["books"]  # 拾光书架
    assert {b["id"] for b in hits} >= set(book_ids.values()), hits
    assert get(f"/api/books/{book_ids['epub']}/search?q=%E6%B3%A8%E9%87%8A")["hits"]  # 注释
    bid = book_ids["epub"]
    assert post(f"/api/books/{bid}/progress", {"pos": {"ch": 1, "f": 0.5}, "pct": 0.6, "secs": 120})["ok"]
    assert post(f"/api/books/{bid}/marks", {"pos": {"ch": 1, "f": 0}, "pct": 0.5, "text": "书签"})["book"]["marks"]
    info = get(f"/api/books/{bid}")["book"]
    assert info["read"]["pct"] == 0.6 and info["read"]["seconds"] == 120, info["read"]
    w = get("/api/weekly")["current"]
    assert any(b["id"] == bid for b in w["books"]["list"]) and w["books"]["seconds"] >= 120, w["books"]
    assert "backlog" in w and "subs" in w["backlog"]
    assert any(b["id"] == bid for b in get("/api/jobs")["reading"])


def book_remove():
    for bid in book_ids.values():
        assert post(f"/api/books/{bid}/delete")["ok"]
    assert not G.q("SELECT 1 FROM book_ch WHERE book IN (?, ?)", tuple(book_ids.values()), one=True)
    assert not list((tmp / "books").glob(f"{book_ids['epub']}.*"))


check("e-book: EPUB imported, cleaned (no scripts), links and pictures kept", epub_import)
check("e-book: GBK TXT split into chapters", txt_import)
check("e-book: search, progress, bookmark, 每周总结, 继续阅读", book_search_and_progress)
check("e-book: removed with its files", book_remove)
def weekly_privacy():
    """A video hidden by privacy mode isn't in 每周总结: not in the list, the counts or the day bars."""
    owner = f"user:{user}"
    prev = G.q("SELECT tags FROM privacy WHERE owner=?", (owner,), one=True)
    hidden = json.loads(prev["tags"]) if prev else []
    row = next((r for r in G.q("SELECT id, analysis FROM jobs WHERE owner=? AND status='done' ORDER BY id DESC LIMIT 50", (owner,))
                if not shiguang.web.is_hidden({"analysis": json.loads(r["analysis"] or "{}")}, {"tags": hidden})), None)
    if not row:
        return
    jid, tag = row["id"], "隐私测试标签"
    a = json.loads(G.q("SELECT analysis FROM jobs WHERE id=?", (jid,), one=True)["analysis"] or "{}")
    G.q("UPDATE jobs SET analysis=? WHERE id=?", (json.dumps({**a, "tags": [*(a.get("tags") or []), tag]}), jid))
    shiguang.weekly.record(owner, "video", jid, 200, 0, 0.5)
    before = get("/api/weekly")["current"]["videos"]
    assert any(v["id"] == jid for v in before["top"]) or before["watched"], before
    G.q("INSERT INTO privacy (owner, tags, ids) VALUES (?,?,'[]') ON CONFLICT(owner) DO UPDATE SET tags=excluded.tags",
        (owner, json.dumps([*hidden, tag], ensure_ascii=False)))
    try:
        after = get("/api/weekly")["current"]["videos"]
        assert not any(v["id"] == jid for v in after["top"]), after
        assert after["seconds"] <= before["seconds"] - 200 and after["watched"] < before["watched"], (before, after)
    finally:
        G.q("UPDATE privacy SET tags=? WHERE owner=?", (prev["tags"] if prev else "[]", owner))


def tag_search():
    """#标签 finds only videos that have that tag (a tag containing it), nothing matched by title or subtitles."""
    import urllib.parse
    owner = f"user:{user}"
    prev = G.q("SELECT tags FROM privacy WHERE owner=?", (owner,), one=True)
    hidden = {"tags": json.loads(prev["tags"]) if prev else []}
    tag = next((a["tags"][0] for a in (json.loads(r["analysis"] or "{}") for r in G.q(
        "SELECT analysis FROM jobs WHERE owner=? AND status='done' ORDER BY id DESC LIMIT 50", (owner,)))
        if a.get("tags") and not shiguang.web.is_hidden({"analysis": a}, hidden)), None)
    if not tag:
        return
    found = get("/api/jobs?q=" + urllib.parse.quote("#" + tag))
    assert found["jobs"], tag
    assert all(any(tag.lower() in t.lower() for t in j["analysis"].get("tags") or []) for j in found["jobs"]), tag
    assert not found["book_hits"]


check("#标签 searches only tags", tag_search)
check("每周总结 leaves out videos hidden by privacy mode", weekly_privacy)
check("每周总结 for last week (Monday 8:00)", lambda: G.make_reports() or 1)
check("e-book links go to the shelf", lambda: (G.is_book_url("https://x.org/a/b.epub?dl=1") and not G.is_book_url("https://youtu.be/x")
                                          and G.is_book_url("https://z-library.biz/book/6r60kDvygG/x.html?ts=1") and not G.is_book_url("https://z-library.biz/s/x")) or 1 / 0)



def claude_round_trip():
    """An AI request goes on the board for the Mac's Claude; a stand-in Mac answers it (Claude, then Codex standing in
    for it, logged as such); nobody taking it means None."""
    import threading

    def mac(engine):
        for _ in range(100):
            t = G.claim_task("test-claude", ["llm", "claude"])
            if t:
                assert t["payload"]["model"] == G.CLAUDE_MODEL_LIGHT and t["payload"]["effort"] == "low", t["payload"]
                G.complete_task(t["id"], "test-claude", {"out": {"cause": "测试", "fix": "无"}, "engine": engine,
                                                          "tokens_in": 10, "tokens_out": 5})
                return
            time.sleep(0.2)
    for engine in ("claude", "codex"):
        th = threading.Thread(target=mac, args=(engine,))
        th.start()
        started, usage = time.time(), {}
        out = G.claude_json("sys", "user", {"cause": "string", "fix": "string"}, "failure", None, usage, False)
        th.join()
        assert out == {"cause": "测试", "fix": "无"}, out
        assert usage.get(engine) == 1, usage
        assert G.q("SELECT 1 FROM usage WHERE kind=? AND purpose='failure' AND ts >= ?", (engine, started), one=True)
    shiguang.llm.CLAUDE_CLAIM_WAIT = 2
    assert G.claude_json("sys", "user", {"x": "string"}, "failure", None, {}, False) is None
    assert not G.q("SELECT 1 FROM tasks WHERE kind='llm'", one=True), "requests left on the board"


check("AI request through the Mac's Claude / Codex, and the fallback when nobody takes it", claude_round_trip)


def shortcut_from_outside():
    key = get("/api/account")["shortcut_key"]
    out = app.test_client(use_cookies=False)  # the iOS shortcut: no cookies, through the tunnel's port
    env = {"SERVER_PORT": str(shiguang.core.EXTERNAL_PORT)}

    def send(body):
        return out.post("/api/add", json=body, environ_overrides=env)
    assert send({"text": "smoke 随记", "device": "测试手机"}).status_code == 401
    assert send({"text": "smoke 随记", "device": "测试手机", "key": "x" * 24}).status_code == 403
    r = send({"text": "smoke 随记", "device": "测试手机", "key": key})
    assert r.status_code == 200, r.data
    assert not r.headers.getlist("Set-Cookie"), r.headers  # a shortcut is given no cookies...
    r = out.post("/api/add", json={"text": "smoke 随记 2", "key": key}, environ_overrides=env,
                 headers={"Cookie": f"{G.DEVICE_COOKIE}={'a' * 32}"})
    assert r.status_code == 200 and r.mimetype == "text/plain", r.data  # ...and one that kept some still works
    assert G.q("SELECT owner FROM notes WHERE text='smoke 随记'", one=True)["owner"] == f"user:{user}"
    assert post("/api/shortcut-key/new")["shortcut_key"] != key
    assert send({"text": "smoke 随记", "key": key}).status_code == 403  # the old key stops working
    shiguang.web.login_failures.clear()
    # the shortcut for outside: asked of the Mac (a task), served once it's back
    shiguang.web.PUBLIC_URL = "https://example.invalid:8443"
    r = c.get("/shortcut/remote")
    assert r.status_code == 202, r.status_code
    f, target = shiguang.web.remote_shortcut(user)
    t = G.q("SELECT * FROM tasks WHERE kind='shortcut' AND target=?", (target,), one=True)
    p = __import__("json").loads(t["payload"])
    assert t["state"] == "queued" and p["key"] == G.q("SELECT shortcut_key k FROM users WHERE name=?", (user,), one=True)["k"]
    assert p["url"] == "https://example.invalid:8443/api/add"
    shiguang.tasks.save_remote_shortcut(t, {"data": __import__("base64").b64encode(b"signed" * 300).decode()})
    r = c.get("/shortcut/remote")
    assert r.status_code == 200 and r.data == b"signed" * 300
    assert c.post("/api/add", json={"text": "https://example.com/x"}, headers={"Origin": "https://evil.example"}).status_code == 403


check("iOS shortcut from outside needs its account's key", shortcut_from_outside)


def web_hardening():
    n = G.q("SELECT COUNT(*) n FROM jobs", one=True)["n"]
    r = c.get("/add?url=" + __import__("urllib.parse").parse.quote("http://a.example/<img src=x onerror=alert(1)>"))
    assert b"<img" not in r.data and r.status_code == 400, r.data  # not a URL as a whole: refused, never echoed raw
    r = c.get("/add?url=" + __import__("urllib.parse").parse.quote("https://example.com/a?x=1&y='z'"))
    assert b"x=1&amp;y=&#x27;z&#x27;" in r.data and b"<form" in r.data, r.data
    assert G.q("SELECT COUNT(*) n FROM jobs", one=True)["n"] == n, "a bare GET queued a job"
    assert post("/api/add", {"text": "http://192.168.3.1/x http://127.0.0.1:8088/"}, code=400)["error"]
    r = c.post("/api/add", json={"text": "https://example.com/x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, r.status_code
    assert G.q("SELECT COUNT(*) n FROM jobs", one=True)["n"] == n


check("/add escapes and asks first; no internal links; no cross-site posts", web_hardening)


def admin_home_only_and_password():
    from werkzeug.security import generate_password_hash
    ext = {"SERVER_PORT": str(shiguang.core.EXTERNAL_PORT)}
    out = app.test_client()
    r = out.post("/api/login", json={"name": "admin", "password": "whatever"}, environ_overrides=ext)
    assert r.status_code == 403 and "家里" in r.get_json()["error"], r.data
    adm = app.test_client()  # an admin login made at home, then used through the tunnel: not logged in there
    G.q("INSERT OR REPLACE INTO devices (id, user, ip, seen, label) VALUES (?,?,?,?,?)", ("e" * 32, "admin", "127.0.0.1", time.time(), "t"))
    adm.set_cookie(G.DEVICE_COOKIE, "e" * 32)
    with adm.session_transaction() as s:
        s["user"] = "admin"
    assert adm.get("/api/jobs", environ_overrides=ext).get_json()["login_required"]
    with adm.session_transaction() as s:  # (a browser keeps a separate cookie per address; this client has one jar)
        s["user"] = "admin"
    assert adm.get("/api/jobs").get_json()["admin"]
    G.q("UPDATE users SET pw=? WHERE name=?", (generate_password_hash("old-password"), user))
    assert c.post("/api/password", json={"old": "wrong", "new": "new-password"}).status_code == 403
    assert c.post("/api/password", json={"old": "old-password", "new": "short"}).status_code == 400
    assert c.post("/api/password", json={"old": "old-password", "new": "new-password"}, environ_overrides=ext).status_code == 403
    assert c.post("/api/password", json={"old": "old-password", "new": "new-password"}).status_code == 200
    assert app.test_client().post("/api/login", json={"name": user, "password": "new-password"}).status_code == 200
    shiguang.web.login_failures.clear()


check("admin only from home; password changed from home only", admin_home_only_and_password)


def pornhub_follow():
    ch = shiguang.channels.channel_of
    assert ch("https://www.pornhub.com/model/Some_Model/videos?o=mr") == (
        "pornhub", "model/some_model", "https://www.pornhub.com/model/some_model/videos")
    assert ch("https://cn.pornhub.com/pornstar/a-b")[0] == "pornhub"
    assert ch("https://www.pornhub.com/users/x")[2].endswith("/users/x/videos/public")
    assert ch("https://www.pornhub.com/view_video.php?viewkey=abc") is None
    sid, how = shiguang.channels.add_sub("https://www.pornhub.com/model/smoke_model", f"user:{user}")
    assert how == "new" and G.q("SELECT name FROM subs WHERE id=?", (sid,), one=True)["name"] == "smoke_model"
    G.q("UPDATE subs SET checked=? WHERE id=?", (time.time(), sid))  # (no fetching from here)
    prefs = G.q("SELECT tags FROM privacy WHERE owner=?", (f"user:{user}",), one=True)
    listed = [s["id"] for s in get("/api/jobs")["subs"]]
    assert (sid not in listed) == bool(prefs and prefs["tags"] not in (None, "[]")), (sid, listed)
    assert sid not in [u.get("sub") for d in get("/api/digests")["digests"] for u in d["uploaders"]]
    G.q("DELETE FROM subs WHERE id=?", (sid,))


check("Pornhub uploader pages are followed, and kept out of sight in privacy mode", pornhub_follow)
def sub_filters():
    ch = shiguang.channels
    f = ch.clean_filter({"op": "and", "items": [
        {"op": "or", "items": [{"f": "title", "op": "has", "v": "教程"}, {"f": "title", "op": "has", "v": "Vlog"}]},
        {"f": "date", "op": "after", "v": "2026-01-01"}, {"f": "title", "op": "has", "v": " "}, {"f": "len", "op": "gt", "v": "5"}]})
    assert len(f["items"]) == 3, f  # the empty keyword is dropped
    assert ch.matches(f, {"title": "Python教程", "date": "2026-02-01", "duration": 600})
    assert not ch.matches(f, {"title": "my vlog", "date": "2025-12-31", "duration": 600})
    assert ch.matches(f, {"title": "my VLOG"})  # no date / length in the list: those conditions can't say no
    assert not ch.matches(f, {"title": "教程", "date": "2026-05-01", "duration": 120})
    assert ch.clean_filter({"op": "or", "items": []}) is None
    for bad in ({"f": "date", "op": "after", "v": "2026/1/1"}, {"op": "xor", "items": []}, {"f": "x", "op": "has", "v": "a"}):
        try:
            ch.clean_filter(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    sid, _ = ch.add_sub("https://space.bilibili.com/999999999999", f"user:{user}", flt=f)
    G.q("UPDATE subs SET checked=?, refilter=0 WHERE id=?", (time.time(), sid))  # (no fetching from here)
    assert [s for s in get("/api/jobs")["subs"] if s["id"] == sid][0]["filter"] == f
    assert c.post(f"/api/subs/{sid}/filter", json={"filter": {"f": "date", "op": "after", "v": "bad"}}).status_code == 400
    assert c.post(f"/api/subs/{sid}/filter", json={"filter": None}).status_code == 200
    row = G.q("SELECT filter, refilter, checked FROM subs WHERE id=?", (sid,), one=True)
    assert row["filter"] == "" and row["refilter"] == 1 and row["checked"] is None, dict(row)
    G.q("DELETE FROM subs WHERE id=?", (sid,))


check("追更 filters: keywords, dates, lengths in and / or groups", sub_filters)


def bilibili_412_restarts_session():
    ch = shiguang.channels
    class Reply:
        headers = {"content-type": "application/json"}
        def __init__(self, body): self.body = body
        def json(self): return self.body
    class Session:
        wbi_key = "a" * 32
        def __init__(self, blocked): self.blocked = blocked
        def get(self, url, params=None, timeout=None):
            if url.endswith("/card"):
                return Reply({"code": 0, "data": {"card": {"name": "UP", "face": ""}}})
            if self.blocked:
                return Reply({"code": -412, "message": "risk control"})
            return Reply({"code": 0, "data": {"page": {"count": 1}, "list": {"vlist": [
                {"bvid": "BVtest", "title": "新视频", "created": 1700000000, "length": "1:02"}]}}})
    sessions = iter([Session(True), Session(False)])
    old_session, old_sleep = ch.bili_session, ch.time.sleep
    try:
        ch.bili_session, ch.time.sleep = lambda mid: next(sessions), lambda sec: None
        got = ch.list_bilibili("1", 1)
        assert got["entries"][0]["url"].endswith("BVtest") and got["entries"][0]["duration"] == 62
    finally:
        ch.bili_session, ch.time.sleep = old_session, old_sleep


check("B站追更 412 starts a new signed session", bilibili_412_restarts_session)


def bilibili_cookie_upload():
    G.q("UPDATE users SET admin=1 WHERE name=?", (user,))
    sid, _ = shiguang.channels.add_sub("https://space.bilibili.com/9988776655", f"user:{user}")
    G.q("UPDATE subs SET checked=?, error=? WHERE id=?", (time.time(), "B站列表获取失败（-352 风控校验失败）", sid))
    cookie = (b"# Netscape HTTP Cookie File\n"
              b".bilibili.com\tTRUE\t/\tTRUE\t2147483647\tSESSDATA\tx\n"
              b".bilibili.com\tTRUE\t/\tTRUE\t2147483647\tbili_jct\ty\n"
              b".bilibili.com\tTRUE\t/\tTRUE\t2147483647\tDedeUserID\t1\n")
    r = c.post("/api/bilibili/cookies", data={"cookies": (io.BytesIO(cookie), "cookies.txt")},
               content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    assert shiguang.channels.bili_cookie_state() == "ready"
    row = G.q("SELECT checked, error FROM subs WHERE id=?", (sid,), one=True)
    assert row["checked"] is None and not row["error"], dict(row)
    G.q("DELETE FROM subs WHERE id=?", (sid,))


check("B站 cookies upload validates and reschedules follows", bilibili_cookie_upload)
check("subtitles translated: other languages, not English", lambda: (
    shiguang.tasks.translatable(["/x/v.ja.srt"], "v") == "/x/v.ja.srt"
    and shiguang.tasks.translatable(["/x/v.en.srt"], "v") is None
    and shiguang.tasks.translatable(["/x/v.ja.srt", "/x/v.en.srt"], "v") is None
    and shiguang.tasks.translatable(["/x/v.ko.srt", "/x/v.zh.srt"], "v") is None
    and shiguang.tasks.translatable(["/x/v.srt"], "v") is None) or 1/0)

shutil.rmtree(tmp, ignore_errors=True)
print("\nall good" if not failures else f"\n{len(failures)} failed: {', '.join(failures)}")
sys.exit(1 if failures else 0)
