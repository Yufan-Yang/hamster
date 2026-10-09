"""The task board: publishing, claiming with leases, the Pi's own workers."""
import json
import os
import re
import subprocess
import sys
import time
import traceback

from pathlib import Path
from .migrations import once
from .core import (ENTRY, VIDEO_EXT, _write, bell_mark, bell_wait, job_dict, kv_set, mem_available_mb, q, ring)


IDLE_WORK = os.environ.get("IDLE_WORK", "1") != "0"
# The Mac mini as a compute worker (mac_worker.py): it asks for work over the LAN with this token
COMPUTE_TOKEN = os.environ.get("COMPUTE_TOKEN", "").strip()


# ---- the task board
#
# Everything slow is a task on one board: the `tasks` table, here on the Pi because it's always on. Anyone may
# publish: the Pi when something happens (a download finished, a note was added), a finished task (transcribe ->
# save_subs -> summarize), the Mac (`task publish ...`). Anyone may claim what it can do, through the same steps:
#     claim (what I can do) -> heartbeat (progress; keeps the lease) -> done (result) | fail (retry or give up)
# Claimers: the Pi's own workers (a light one for library writes and AI calls, a heavy one for CPU work in idle
# time) and the Mac mini (mac/mac_worker.py, over HTTP). A lease that isn't renewed runs out and the task goes back
# on the board: a Mac that sleeps or a process that dies only means someone picks the task up later.
# Only the Pi changes the library (files, database): what the Mac works out comes back as a result, and a Pi task
# writes it.

# Every kind of task is declared once, with @task on the function that does it on the Pi:
#   label    how 资源使用 names it
#   pool     which of the Pi's own workers takes it: "light" (library writes, ~no CPU), "ai" (LLM calls; a long
#            translation), "ai-quick" (LLM calls that mustn't wait behind one), "cpu" (heavy, idle time only)
#   prefer   while a worker with this trait ("gpu": the Mac) is around, the Pi leaves the kind to it (unless the task
#            has waited PREFER_WAIT)
#   remote   a remote worker (the Mac, scripts) may publish it
#   then     what's published when it's done (whoever did it): a kind (its result is written by that Pi task), or a
#            function (task, result)
# A worker claims by listing the kinds it can do (plus "gpu" if it has one). "save_*" kinds write what a worker
# worked out into the library, so only the Pi does them.
TASK_KINDS = {}
WORKER_FRESH = 300
PREFER_WAIT = 3 * 86400  # e.g. a Mac that claims but never finishes
LEASE = 300  # seconds a claim lasts without a heartbeat
PREFER_WAIT_PAUSED = 4 * 3600  # a worker paused (a game) longer than this stops counting as around
# "now": CPU work someone is waiting for (a note just made): done right away, not only in idle time
# ("mac": kinds only the Mac does, e.g. asking Claude; no Pi worker takes them)
PI_WORKERS = {"light": "pi", "ai": "pi-ai", "ai-quick": "pi-ai-2", "cpu": "pi-cpu", "now": "pi-now"}


def task(kind, label, pool, prefer=None, remote=False, then=None, prefer_wait=PREFER_WAIT):
    def register(run):
        TASK_KINDS[kind] = {"label": label, "pool": pool, "prefer": prefer, "remote": remote, "then": then, "run": run,
                            "prefer_wait": prefer_wait}
        return run
    return register


def pi_worker(pool):
    """(name, kinds it takes) of one of the Pi's own workers. The "ai" worker also takes the quick AI kinds."""
    pools = {pool, "ai-quick"} if pool == "ai" else {pool}
    return PI_WORKERS[pool], [k for k, v in TASK_KINDS.items() if v["pool"] in pools]


def task_payload(kind, target):
    """What a task needs to know about its target, filled in by the Pi (a publisher only names the target)."""
    n = re.fullmatch(r"note:(\d+)", target)
    if n:
        row = q("SELECT text, media FROM notes WHERE id=?", (int(n.group(1)),), one=True)
        if not row:
            raise ValueError(f"no note {n.group(1)}")
        return {"note": int(n.group(1)), "title": (row["text"] or "随记")[:30],
                "files": [{"file": m["file"], "kind": m["kind"]} for m in json.loads(row["media"]) if m.get("todo")]}
    b = re.fullmatch(r"book:(\d+)", target)
    if b:
        row = q("SELECT title FROM books WHERE id=?", (int(b.group(1)),), one=True)
        if not row:
            raise ValueError(f"no book {b.group(1)}")
        return {"book": int(b.group(1)), "title": row["title"] or "电子书"}
    d = re.fullmatch(r"(?:digest|notes-recap):(.+):(\d{4}-\d\d-\d\d):(\d{4}-\d\d-\d\d)", target)
    if d:
        return {"owner": d.group(1), "start": d.group(2), "end": d.group(3), "title": f"{d.group(2)} ~ {d.group(3)}"}
    m = re.fullmatch(r"job:(\d+)(?::(\d+))?", target)
    if not m:
        raise ValueError(f"unknown target {target}")
    row = q("SELECT * FROM jobs WHERE id=?", (int(m.group(1)),), one=True)
    if not row or (row["status"] != "done" and not (kind == "explain_failure" and row["status"] == "failed")):
        raise ValueError(f"no finished job {m.group(1)}")
    a = json.loads(row["analysis"] or "{}")
    out = {"job": row["id"], "title": a.get("title") or row["title"]}
    if m.group(2) is not None:
        media = library.playable(job_dict(row))
        n = int(m.group(2))
        if n >= len(media):
            raise ValueError(f"job {row['id']} has no file {n}")
        out.update(part=n, duration=library.duration_of(media[n]["path"]) or 0, language=whisper_lang(a.get("language")))
    return out


def publish(kind, target, priority=0, parent=None, by="pi", force=False, not_before=None, payload=None):
    """Put a task on the board; one per kind and target (publishing it again is a no-op unless `force`, which runs
    a finished one again). Returns its id. `payload`: what the worker needs, when it isn't worked out from the target
    (a request to Claude carries its whole prompt)."""
    if kind not in TASK_KINDS:
        raise ValueError(f"unknown kind {kind}")
    payload = json.dumps(payload if payload is not None else task_payload(kind, target), ensure_ascii=False)
    now = time.time()
    _write("INSERT OR IGNORE INTO tasks (kind, target, priority, payload, parent, published_by, not_before, created, "
           "updated) VALUES (?,?,?,?,?,?,?,?,?)", (kind, target, priority, payload, parent, by, not_before, now, now))
    row = q("SELECT id, state FROM tasks WHERE kind=? AND target=?", (kind, target), one=True)
    ring("tasks")
    if force and row["state"] in ("done", "failed"):
        _write("UPDATE tasks SET state='queued', priority=?, payload=?, parent=?, published_by=?, result=NULL, "
               "progress=NULL, error='', attempts=0, worker=NULL, lease_until=NULL, not_before=?, updated=? "
               "WHERE id=? AND state IN ('done','failed')", (priority, payload, parent, by, not_before, now, row["id"]))
    return row["id"]


def publish_job_work(jid, priority, force=False):
    """A finished download's slow work: subtitles where there are none (speech-to-text, except songs), indexing the
    subtitle files it came with, the cover."""
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row or row["status"] != "done" or row["ref"]:
        return
    a = json.loads(row["analysis"] or "{}")
    music = a.get("library") == "Music" or a.get("folder") == "Music Videos"  # speech-to-text can't do songs
    for n, m in enumerate(library.playable(job_dict(row))):
        if m["subs"]:
            publish("index_subs", f"job:{jid}:{n}", priority, force=force)
        elif not music:
            publish("transcribe", f"job:{jid}:{n}", priority, force=force)
        if Path(m["path"]).suffix.lower() in VIDEO_EXT:
            publish("frames", f"job:{jid}:{n}", priority - 1, force=force)
    if row["thumb"] and Path(row["thumb"]).exists():
        publish("cover", f"job:{jid}", priority, force=force)


_seen_written = {}


def seen_worker(name, caps, task=None, paused=None):
    # at most once a minute unless something changed: three workers asking every few seconds wore the SD card
    state = (json.dumps(sorted(caps)), task, paused)
    last = _seen_written.get(name)
    if last and last[0] == state and time.time() - last[1] < 60:
        return
    _seen_written[name] = (state, time.time())
    q("INSERT INTO workers (name, caps, seen, task, paused) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
      "caps=excluded.caps, seen=excluded.seen, task=excluded.task, paused=excluded.paused",
      (name, json.dumps(sorted(caps)), time.time(), task, paused))


def task_dict(row):
    return {"id": row["id"], "kind": row["kind"], "target": row["target"], "priority": row["priority"],
            "payload": json.loads(row["payload"]), "progress": json.loads(row["progress"] or "null")}


def claim_task(worker, caps):
    """The most urgent task this worker can do, now leased to it; None if there's nothing."""
    caps, now = set(caps), time.time()
    seen_worker(worker, caps)
    # leases that ran out: back on the board (keeping any progress); looked for first, so a quiet board isn't written
    if q("SELECT 1 FROM tasks WHERE state='running' AND lease_until < ? LIMIT 1", (now,), one=True):
        _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL WHERE state='running' AND lease_until < ?", (now,))
    kinds = [k for k in TASK_KINDS if k in caps]
    if not kinds:
        return None
    alive = {c for r in q("SELECT caps FROM workers WHERE seen > ? AND name != ?", (now - WORKER_FRESH, worker))
             for c in json.loads(r["caps"])}
    # leave a preferred worker's tasks to it while it's around (unless they've waited long enough)
    waived = [k for k in kinds if TASK_KINDS[k].get("prefer") and TASK_KINDS[k]["prefer"] not in caps
              and TASK_KINDS[k]["prefer"] in alive]
    marks = ",".join("?" * len(kinds))
    cond = f"state='queued' AND kind IN ({marks}) AND COALESCE(not_before, 0) <= ?"
    args = [*kinds, now]
    for k in waived:  # until it has waited that kind's prefer_wait
        cond += " AND NOT (kind = ? AND created > ?)"
        args += [k, now - TASK_KINDS[k]["prefer_wait"]]
    for _ in range(5):  # another worker may take the same row first: try the next one
        row = q(f"SELECT * FROM tasks WHERE {cond} ORDER BY priority DESC, id DESC LIMIT 1", args, one=True)
        if not row:
            return None
        if _write("UPDATE tasks SET state='running', worker=?, lease_until=?, attempts=attempts+1, updated=? "
                  "WHERE id=? AND state='queued'", (worker, now + LEASE, now, row["id"])):
            seen_worker(worker, caps, row["id"])
            return task_dict(row) | {"lease": LEASE}
    return None


def heartbeat_task(tid, worker, progress=None):
    """Still on it (and how far): the lease is renewed. False if the task isn't this worker's any more."""
    seen_worker(worker, json.loads((q("SELECT caps FROM workers WHERE name=?", (worker,), one=True) or {"caps": "[]"})["caps"]), tid)
    return bool(_write("UPDATE tasks SET lease_until=?, progress=COALESCE(?, progress), updated=? "
                       "WHERE id=? AND worker=? AND state='running'",
                       (time.time() + LEASE, None if progress is None else json.dumps(progress, ensure_ascii=False),
                        time.time(), tid, worker)))


def complete_task(tid, worker, result):
    if not _write("UPDATE tasks SET state='done', result=?, lease_until=NULL, updated=? WHERE id=? AND worker=? "
                  "AND state='running'", (json.dumps(result, ensure_ascii=False), time.time(), tid, worker)):
        return False
    task = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
    if not task:  # removed meanwhile (its job was deleted): nothing to follow up
        return True
    try:
        then = TASK_KINDS.get(task["kind"], {}).get("then")
        if isinstance(then, str):  # a Pi task writes the result into the library
            publish(then, task["target"], task["priority"], parent=task["id"], force=True)
        elif then:
            then(task, result)
    except Exception:
        traceback.print_exc()
    return True


def fail_task(tid, worker, error, retry=True, trace="", expected=False):
    """retry: try again later (after 1, 4, 9... minutes; at most 5 times); else it stays failed, and unless it was
    `expected` (the target is gone...) it goes to 自修复 (heal.py) with its traceback."""
    row = q("SELECT attempts FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, worker), one=True)
    if not row:
        return False
    if retry and row["attempts"] < 5:
        _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL, error=?, not_before=?, updated=? WHERE id=?",
               (str(error)[:1000], time.time() + 60 * row["attempts"] ** 2, time.time(), tid))
    else:
        _write("UPDATE tasks SET state='failed', worker=NULL, lease_until=NULL, error=?, updated=? WHERE id=?",
               (str(error)[:1000], time.time(), tid))
        from . import heal
        heal.task_failed(tid, error, trace, expected)
    return True


def release_task(tid, worker):
    """Paused for other work (not a failure): back on the board with its progress, the try not counted."""
    ring("tasks")
    _write("UPDATE tasks SET state='queued', worker=NULL, lease_until=NULL, attempts=MAX(attempts-1, 0), updated=? "
           "WHERE id=? AND worker=? AND state='running'", (time.time(), tid, worker))


def task_media(task):
    p = task["payload"]
    row = q("SELECT * FROM jobs WHERE id=?", (p["job"],), one=True)
    media = library.playable(job_dict(row)) if row else []
    if p.get("part") is None or p["part"] >= len(media):
        raise ValueError("the file is gone")
    return Path(media[p["part"]]["path"]), media[p["part"]]["subs"]


class AlreadySaved(Exception):
    """The parent's result was written into the library by an earlier run of this task (which then failed to
    report back): only its summary is left, and saving it again would wipe what was saved."""


def parent_result(task):
    row = q("SELECT result FROM tasks WHERE id=(SELECT parent FROM tasks WHERE id=?)", (task["id"],), one=True)
    res = json.loads(row["result"] or "{}") if row else {}
    if res.get("saved"):
        raise AlreadySaved()
    return res


def drop_parent_result(task, summary):
    """Once written into the library, the bulky result (vectors, every subtitle line) isn't needed on the board."""
    _write("UPDATE tasks SET result=? WHERE id=(SELECT parent FROM tasks WHERE id=?)",
           (json.dumps({**summary, "saved": True}, ensure_ascii=False), task["id"]))


# ---- the Pi's own work for each kind

class Paused(Exception):
    pass


def run_claimed(task, worker):
    """Do a claimed task here and report back."""
    beat = lambda progress=None: heartbeat_task(task["id"], worker, progress)  # noqa: E731
    try:
        result = TASK_KINDS[task["kind"]]["run"](task, beat)
    except Paused:
        release_task(task["id"], worker)
        return "paused"
    except AlreadySaved:
        result = {"skipped": "saved by an earlier run"}
    except ValueError as e:  # the target is gone or doesn't fit: no point trying again
        fail_task(task["id"], worker, e, retry=False, expected=True)
        return "failed"
    except Exception as e:
        traceback.print_exc()
        fail_task(task["id"], worker, e, trace=traceback.format_exc())
        return "failed"
    complete_task(task["id"], worker, result)
    return "done"


def box_busy():
    """Something someone is waiting for needs the CPU: a download being processed, a note being transcribed.
    (Plain downloading doesn't count: it's network-bound.)"""
    return bool(q("SELECT 1 FROM jobs WHERE status='processing' LIMIT 1", one=True)
                or q("SELECT 1 FROM tasks WHERE kind='note_media' AND state='running' LIMIT 1", one=True))


def pi_idle():
    return not box_busy() and mem_available_mb() > 500


def light_loop(pool="light"):
    """Worker thread: writing results into the library ("light"), or AI calls ("ai", "ai-quick"); no CPU to speak
    of, so done right away, here."""
    worker = pi_worker(pool)
    while True:
        mark = bell_mark("tasks")
        try:
            claimed = claim_task(*worker)
            if claimed:
                run_claimed(claimed, worker[0])
                continue
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 60)  # a publish rings; a delayed retry or a lease running out within the minute


def heavy_loop():
    """Worker thread: CPU work (speech-to-text when no Mac is around, covers) when nothing else needs the CPU,
    one task at a time in its own lowest-priority process, which pauses (progress kept) when something comes in."""
    time.sleep(30)
    while True:
        mark = bell_mark("tasks")
        try:
            if IDLE_WORK and not box_busy() and mem_available_mb() > 1000:
                claimed = claim_task(*pi_worker("cpu"))
                if claimed:
                    subprocess.run(["nice", "-n", "19", sys.executable, ENTRY, "task", str(claimed["id"]), PI_WORKERS["cpu"]])
                    continue
            else:
                seen_worker(*pi_worker("cpu"))
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 120)  # a publish or a finished download rings


def now_loop():
    """Worker thread: CPU work someone is waiting for (a note just made), right away, in its own process."""
    worker = pi_worker("now")
    while True:
        mark = bell_mark("tasks")
        try:
            claimed = claim_task(*worker)
            if claimed:
                subprocess.run(["nice", "-n", "15", sys.executable, ENTRY, "task", str(claimed["id"]), worker[0]])
                continue
        except Exception:
            traceback.print_exc()
        bell_wait("tasks", mark, 60)


def run_task_process(tid, worker):
    """`grabber.py task N WORKER`: the process for one task a CPU worker claimed."""
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, worker), one=True)
    if row:
        run_claimed(task_dict(row), worker)


def board_summary(since=0):
    """The board for 资源使用: per kind how many wait / run now, how many were done / failed since `since`, who's
    working on what."""
    kinds = {k: {"label": v["label"], "queued": 0, "running": 0, "done": 0, "failed": 0, "hours": 0.0}
             for k, v in TASK_KINDS.items()}
    for r in q("SELECT kind, state, COUNT(*) n, SUM(CASE WHEN state IN ('queued','running') "
               "THEN json_extract(payload, '$.duration') ELSE 0 END) secs FROM tasks "
               "WHERE state IN ('queued','running') OR updated >= ? GROUP BY kind, state", (since,)):
        if r["kind"] in kinds:
            kinds[r["kind"]][r["state"]] = r["n"]
            kinds[r["kind"]]["hours"] += (r["secs"] or 0) / 3600
    for k in kinds.values():
        k["hours"] = round(k["hours"], 1)
    running = [{"id": r["id"], "kind": r["kind"], "label": TASK_KINDS.get(r["kind"], {}).get("label", r["kind"]),
                "worker": r["worker"], "title": json.loads(r["payload"]).get("title"),
                "pct": (json.loads(r["progress"] or "{}") or {}).get("pct")}
               for r in q("SELECT * FROM tasks WHERE state='running' ORDER BY updated DESC")]
    failed = [{"id": r["id"], "label": TASK_KINDS.get(r["kind"], {}).get("label", r["kind"]),
               "title": json.loads(r["payload"]).get("title"), "error": (r["error"] or "")[:200]}
              for r in q("SELECT * FROM tasks WHERE state='failed' AND updated >= ? ORDER BY updated DESC LIMIT 5", (since,))]
    workers = [{"name": r["name"], "caps": json.loads(r["caps"]), "seen": r["seen"],
                "online": time.time() - r["seen"] < WORKER_FRESH, "task": r["task"],
                "paused": json.loads(r["paused"])["why"] if r["paused"] else None}
               for r in q("SELECT * FROM workers ORDER BY seen DESC")]
    from . import heal
    return {"kinds": kinds, "running": running, "failed": failed, "workers": workers, "enabled": IDLE_WORK,
            "busy": box_busy(), "heal": heal.recent()}


@once("board_chapters")
def publish_chapters_once():
    """Once: chapters for the videos that have subtitles already (off-peak: half price)."""
    when = llm.offpeak_from()
    for r in q("SELECT DISTINCT ref, part FROM seg WHERE kind='job' AND src='字幕'"):
        try:
            publish("chapters", f"job:{r['ref']}:{r['part']}", 5, not_before=when)
        except ValueError:
            pass


def reindex_old_model():
    """Pictures indexed with another image model than search.CLIP_MODEL (after switching models): index them again.
    Their old vectors stay until the new ones replace them (search only uses the current model's)."""
    for r in q("SELECT DISTINCT kind, ref, part, src FROM vec WHERE model != ?", (search.CLIP_MODEL,)):
        try:
            if r["kind"] == "job" and r["src"] == "封面":
                publish("cover", f"job:{r['ref']}", 3, force=True)
            elif r["kind"] == "job":
                publish("frames", f"job:{r['ref']}:{r['part']}", 2, force=True)
            elif r["kind"] == "note":
                row = q("SELECT media FROM notes WHERE id=?", (r["ref"],), one=True)
                if row:
                    media = [{**m, "todo": True} if m["kind"] in ("image", "video") else m for m in json.loads(row["media"])]
                    q("UPDATE notes SET media=?, pending=1 WHERE id=?", (json.dumps(media, ensure_ascii=False), r["ref"]))
                    publish("note_media", f"note:{r['ref']}", 3, force=True)
        except ValueError:
            pass


def publish_pending_notes():
    """Notes whose attachments the old loop hadn't done yet (before notes were tasks)."""
    for r in q("SELECT id FROM notes WHERE pending=1"):
        try:
            publish("note_media", f"note:{r['id']}", 60)
        except ValueError:
            pass


@once("board_similar")
def publish_similar_once():
    """Once: look for re-uploads and clips among what's already there (after its frames and subtitles are in)."""
    for r in q("SELECT id FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        try:
            publish("similar", f"job:{r['id']}", 1)
        except ValueError:
            pass


@once("board_translate")
def publish_translations_once():
    """Once: English videos already in the library get Chinese subtitles too."""
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        for n, m in enumerate(library.playable(job_dict(row))):
            if m["subs"]:
                fake = {"target": f"job:{r['id']}:{n}", "priority": 9 if r["backfill"] else 29, "id": None}
                tasks.maybe_translate(fake, Path(m["path"]), m["subs"])


@once("board_frames")
def publish_frames_once():
    """Once (boards made before frames were a task): what's on screen in every video there is."""
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        for n, m in enumerate(library.playable(job_dict(row))):
            if Path(m["path"]).suffix.lower() in VIDEO_EXT:
                publish("frames", f"job:{r['id']}:{n}", 9 if r["backfill"] else 29)


@once("board_migrated")
def migrate_to_board():
    """Once: the work the old idle loop still had to do becomes tasks (newest videos get the higher ids, so they go
    first); its bookkeeping tables go."""
    states = {r["key"]: r["state"] for r in q("SELECT key, state FROM idle")} if \
        q("SELECT name FROM sqlite_master WHERE name='idle'", one=True) else {}
    for r in q("SELECT id, backfill FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id"):
        row = q("SELECT * FROM jobs WHERE id=?", (r["id"],), one=True)
        a = json.loads(row["analysis"] or "{}")
        music = a.get("library") == "Music" or a.get("folder") == "Music Videos"
        pri = 10 if r["backfill"] else 30
        for n, m in enumerate(library.playable(job_dict(row))):
            if Path(m["path"]).suffix.lower() in VIDEO_EXT:
                publish("frames", f"job:{r['id']}:{n}", pri - 1)
            if m["subs"] and states.get(f"cues:{r['id']}:{n}") != "done" and states.get(f"subs:{r['id']}:{n}") != "done":
                publish("index_subs", f"job:{r['id']}:{n}", pri)
            elif not m["subs"] and not music and states.get(f"subs:{r['id']}:{n}") != "done":
                publish("transcribe", f"job:{r['id']}:{n}", pri)
        if row["thumb"] and Path(row["thumb"]).exists() and states.get(f"cover:{r['id']}") != "done":
            publish("cover", f"job:{r['id']}", pri)
    _write("DROP TABLE IF EXISTS lease")
    kv_set("board_frames", True)  # a fresh migration already published frames below
    _write("DROP TABLE IF EXISTS idle")
    kv_set("idle_now", None)
    kv_set("mac_seen", None)


# ---------------------------------------------------------------- the task board over HTTP (the Mac mini, scripts)
#
# mac/mac_worker.py on the Mac mini claims tasks here (pull: the Pi never has to reach the Mac, and a Mac that's
# asleep or off simply stops asking) and publishes its own. LAN only, with COMPUTE_TOKEN.

WHISPER_LANGS = {"zh": ("中文", "汉语", "普通话", "国语", "chinese", "mandarin", "zh"), "yue": ("粤语", "广东话", "cantonese"),
                 "en": ("英语", "英文", "english", "en"), "ja": ("日语", "日文", "japanese", "ja"),
                 "ko": ("韩语", "韩文", "korean", "ko"), "fr": ("法语", "french"), "de": ("德语", "german"),
                 "es": ("西班牙语", "spanish"), "ru": ("俄语", "russian")}


def whisper_lang(language):
    """The classifier's idea of the language ("中文", "English", "zh-CN"...) as a Whisper code, if it's clearly one.
    Whisper guessing from a few seconds goes wrong on films with little talk (a Japanese film came out as Korean)."""
    text = str(language or "").casefold()
    found = {code for code, names in WHISPER_LANGS.items()
             if any(n == text or n == text.split("-")[0] or ((len(n) > 2 or not n.isascii()) and n in text) for n in names)}
    return found.pop() if len(found) == 1 else None  # several ("中英双语"): let Whisper find out


# The other modules, imported last: they import this one too, and are only used at run time
from . import library, llm, search, tasks  # noqa: E402
