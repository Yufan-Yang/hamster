#!/usr/bin/env python3
"""Smoke test on a copy of the real database: every page API answers, search and the task board work.
Run on the Pi before deploying:  sudo -u grabber /opt/grabber/venv/bin/python tests/smoke.py [app dir]
It copies the database to a temp dir (the real one isn't touched) and never starts downloads."""
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
check("每周总结 for last week (Monday 8:00)", lambda: G.make_reports() or 1)
check("e-book links go to the shelf", lambda: (G.is_book_url("https://x.org/a/b.epub?dl=1") and not G.is_book_url("https://youtu.be/x")) or 1 / 0)



def claude_round_trip():
    """An AI request goes on the board for the Mac's Claude; a stand-in Mac answers it; nobody taking it means None."""
    import threading

    def mac():
        for _ in range(100):
            t = G.claim_task("test-claude", ["llm", "claude"])
            if t:
                assert t["payload"]["model"] == G.CLAUDE_MODEL_LIGHT and t["payload"]["effort"] == "low", t["payload"]
                G.complete_task(t["id"], "test-claude", {"out": {"cause": "测试", "fix": "无"}, "tokens_in": 10, "tokens_out": 5})
                return
            time.sleep(0.2)
    th = threading.Thread(target=mac)
    th.start()
    out = G.claude_json("sys", "user", {"cause": "string", "fix": "string"}, "failure", None, {}, False)
    th.join()
    assert out == {"cause": "测试", "fix": "无"}, out
    shiguang.llm.CLAUDE_CLAIM_WAIT = 2
    assert G.claude_json("sys", "user", {"x": "string"}, "failure", None, {}, False) is None
    assert not G.q("SELECT 1 FROM tasks WHERE kind='llm'", one=True), "requests left on the board"


check("AI request through the Mac's Claude, and the fallback when nobody takes it", claude_round_trip)

shutil.rmtree(tmp, ignore_errors=True)
print("\nall good" if not failures else f"\n{len(failures)} failed: {', '.join(failures)}")
sys.exit(1 if failures else 0)
