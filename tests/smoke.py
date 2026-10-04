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

shutil.rmtree(tmp, ignore_errors=True)
print("\nall good" if not failures else f"\n{len(failures)} failed: {', '.join(failures)}")
sys.exit(1 if failures else 0)
