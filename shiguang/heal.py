"""自修复: an error the Pi runs into (a task that fails for good, a page request that crashes) becomes an incident;
the Mac's Claude Code reads it with the code, works out why, and fixes it (mac_worker.py, heal). A code fix goes live
by itself when the smoke test passes (deploy.sh: tests on a copy of the database, rolls back if the Pi doesn't come
back up); the tasks that failed are then tried again.

The same error over and over is one incident (counted), and healed once: again only if it comes back after a fix
(at most HEAL_TRIES times). At most HEAL_PER_DAY are sent to the Mac a day."""
import json
import os
import re
import time

from . import board
from .core import _write, q

HEAL = os.environ.get("HEAL", "1") != "0"
HEAL_PER_DAY = int(os.environ.get("HEAL_PER_DAY", "6"))
HEAL_TRIES = 2
# not bugs to heal: AI requests fall back to DeepSeek on purpose, only a Mac signs shortcuts, and healing itself
SKIP_KINDS = {"llm", "shortcut", "heal"}

def signature(kind, error, trace):
    """The same bug however often it happens: the kind, the error without its numbers and paths, where it was raised."""
    where = re.findall(r'File "[^"]*/(shiguang/[^"]+|grabber\.py)", line \d+, in (\w+)', trace or "")
    text = re.sub(r"/[^\s'\"]+", "/…", str(error))
    text = re.sub(r"\d+", "N", text)[:200]
    return f"{kind}|{text}|{'/'.join(where[-1]) if where else ''}"


def report(kind, label, error, trace="", context=None, task_id=None, title=""):
    """Something went wrong for good: keep it as an incident and, when it's new (or back after a fix), send it to the
    Mac to heal. Never raises (it's called while handling another error)."""
    try:
        if not HEAL or kind in SKIP_KINDS or str(error).startswith("network:"):
            return
        sig, now = signature(kind, error, trace), time.time()
        row = q("SELECT * FROM incidents WHERE sig=?", (sig,), one=True)
        if row:
            tasks = json.loads(row["tasks"] or "[]")
            if task_id and task_id not in tasks:
                tasks = (tasks + [task_id])[-50:]
            back = row["state"] == "fixed" and row["healed"] and now > row["healed"] + 60  # came back after a fix
            _write("UPDATE incidents SET count=count+1, last=?, tasks=?, error=?, trace=?, context=?, state=? WHERE id=?",
                   (now, json.dumps(tasks), str(error)[:2000], (trace or "")[-8000:],
                    json.dumps(context or {}, ensure_ascii=False, default=str)[:6000],
                    "back" if back else row["state"], row["id"]))
            if back or row["state"] == "new":  # (new: the day's allowance was used up when it first happened)
                send(row["id"])
            return
        _write("INSERT OR IGNORE INTO incidents (sig, kind, label, title, error, trace, context, tasks, first, last) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (sig, kind, label, title[:80], str(error)[:2000], (trace or "")[-8000:],
                json.dumps(context or {}, ensure_ascii=False, default=str)[:6000],
                json.dumps([task_id] if task_id else []), now, now))
        row = q("SELECT id FROM incidents WHERE sig=?", (sig,), one=True)
        if row:
            send(row["id"])
    except Exception:
        import traceback
        traceback.print_exc()


def send(iid):
    """On the board for the Mac, within the day's allowance (else it waits as 'new': the next day's first errors
    pick it up again, or 重新诊断 on the page)."""
    row = q("SELECT * FROM incidents WHERE id=?", (iid,), one=True)
    if not row or row["tries"] >= HEAL_TRIES:
        return
    sent = q("SELECT COUNT(*) n FROM tasks WHERE kind='heal' AND created > ?", (time.time() - 86400,), one=True)["n"]
    if sent >= HEAL_PER_DAY:
        return
    _write("UPDATE incidents SET tries=tries+1, state='healing' WHERE id=?", (iid,))
    board.publish("heal", f"incident:{iid}", 40, force=True, payload={
        "incident": iid, "title": f"{row['label']}：{row['error'][:40]}", "kind": row["kind"], "label": row["label"],
        "error": row["error"], "trace": row["trace"], "context": json.loads(row["context"] or "{}"),
        "count": row["count"], "about": row["title"], "before": row["summary"] if row["state"] == "back" else ""})


def task_failed(tid, error, trace="", expected=False):
    """A task failed for good (board.fail_task). `expected`: its target is gone or doesn't fit (not a bug)."""
    if expected:
        return
    row = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
    if not row:
        return
    payload = json.loads(row["payload"] or "{}")
    parent = q("SELECT kind, result FROM tasks WHERE id=?", (row["parent"],), one=True) if row["parent"] else None
    context = {"task": {"id": tid, "kind": row["kind"], "target": row["target"], "worker": row["worker"],
                        "attempts": row["attempts"], "payload": json.dumps(payload, ensure_ascii=False)[:1500]}}
    if parent:
        context["parent"] = {"kind": parent["kind"], "result": (parent["result"] or "")[:1500]}
    report(row["kind"], board.TASK_KINDS.get(row["kind"], {}).get("label", row["kind"]), error, trace, context, tid,
           payload.get("title") or "")


def request_failed(method, path, exc, trace):
    """A page / API request crashed (500)."""
    report("web", "页面请求", f"{type(exc).__name__}: {exc}", trace, {"request": f"{method} {path}"}, title=path)


@board.task("heal", "自修复", "mac", remote=True)
def pi_heal(task, beat):
    """Only the Mac heals (Claude Code with the code next to it); no Pi worker takes these."""
    raise ValueError("only the Mac heals")


def heal_done(task, result):
    """What the Mac found (and did): kept with the incident; after a fix (or when it says so) the tasks that failed
    are tried again."""
    iid = json.loads(task["payload"])["incident"]
    _write("UPDATE incidents SET state=?, diagnosis=?, summary=?, commit_id=?, healed=? WHERE id=?",
           (result.get("state") or "diagnosed", str(result.get("diagnosis") or "")[:4000],
            str(result.get("summary") or "")[:1000], str(result.get("commit") or ""), time.time(), iid))
    if result.get("retry"):
        row = q("SELECT tasks FROM incidents WHERE id=?", (iid,), one=True)
        ids = [int(i) for i in json.loads(row["tasks"] or "[]")] if row else []
        if ids:
            _write(f"UPDATE tasks SET state='queued', attempts=0, error='', not_before=NULL, worker=NULL, updated=? "
                   f"WHERE state='failed' AND id IN ({','.join('?' * len(ids))})", (time.time(), *ids))
            board.ring("tasks")


board.TASK_KINDS["heal"]["then"] = heal_done


def recent(n=8):
    """For 资源使用: the latest incidents and how healing went."""
    out = []
    for r in q("SELECT i.*, t.state task_state, t.error task_error FROM incidents i LEFT JOIN tasks t "
               "ON t.kind='heal' AND t.target='incident:' || i.id ORDER BY i.last DESC LIMIT ?", (n,)):
        state = r["state"]
        if state == "healing" and r["task_state"] == "failed":
            state = "error"
        out.append({"id": r["id"], "label": r["label"], "title": r["title"], "error": (r["error"] or "")[:200],
                    "count": r["count"], "state": state, "diagnosis": r["diagnosis"], "summary": r["summary"],
                    "commit": r["commit_id"], "last": r["last"], "why": (r["task_error"] or "")[:200] if state == "error" else ""})
    return out
