"""分享: a link to one video for one person. The first browser that opens it owns it from then on and can open it
as often as it likes; the same link opened anywhere else (forwarded on) is refused.

Opening takes a tap on the page, not just loading it: chat apps fetch links to draw previews, and that fetch
must not use the link up. Whoever shared it can unbind it (the friend changed phones) or cancel it."""
import secrets
import time

from flask import g
from flask import jsonify
from flask import request
from flask import send_from_directory
from .core import HERE, app, job_dict, q

SHARE_MEDIA_DAYS = 0.5  # how long the play / subtitle links handed to a share page work (it fetches new ones on reload)
CODE_LEN = 12  # bytes of randomness in a link


def share_url(code):
    from .web import PUBLIC_URL
    return f"{PUBLIC_URL or request.host_url.rstrip('/')}/s/{code}"


def share_dict(r):
    return {"code": r["code"], "url": share_url(r["code"]), "created": r["created"], "claimed": r["claimed"],
            "label": r["label"], "opens": r["opens"], "seen": r["seen"], "revoked": bool(r["revoked"])}


def own_share(code):
    row = q("SELECT * FROM shares WHERE code=?", (code,), one=True)
    return row if row and (g.admin or row["owner"] == g.owner) else None


@app.get("/api/shares")
def shares_list():
    try:
        jid = int(request.args.get("job", ""))
    except ValueError:
        return jsonify(error="job?"), 400
    from .web import visible
    if not visible(jid):
        return jsonify(error="not found"), 404
    rows = q("SELECT * FROM shares WHERE job_id=? AND revoked=0 ORDER BY created DESC", (jid,))
    return jsonify(shares=[share_dict(r) for r in rows if g.admin or r["owner"] == g.owner])


@app.post("/api/shares")
def share_create():
    from . import library
    from .web import visible
    try:
        jid = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify(error="视频不对"), 400
    row = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not row or not visible(jid):
        return jsonify(error="not found"), 404
    if row["status"] != "done" or not library.playable(job_dict(row)):
        return jsonify(error="下载完、能播放的视频才能分享"), 400
    code = secrets.token_urlsafe(CODE_LEN)
    q("INSERT INTO shares (code, job_id, owner, created) VALUES (?,?,?,?)", (code, jid, g.owner, time.time()))
    return jsonify(share_dict(q("SELECT * FROM shares WHERE code=?", (code,), one=True)))


@app.post("/api/shares/<code>/<action>")
def share_action(code, action):
    row = own_share(code)
    if not row:
        return jsonify(error="not found"), 404
    if action == "reset":  # let the next browser that opens it have it
        q("UPDATE shares SET device=NULL, label=NULL, claimed=NULL WHERE code=?", (code,))
    elif action == "revoke":
        q("UPDATE shares SET revoked=1 WHERE code=?", (code,))
    else:
        return jsonify(error="unknown action"), 400
    return jsonify(ok=True)


# ---------------------------------------------------------------- the friend's side (no account needed)

@app.get("/s/<code>")
def share_page(code):
    resp = send_from_directory(HERE / "static", "share.html", max_age=0)
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def share_state(row):
    """What this browser may do with the link: open (nobody has it yet), mine, own (the sharer's), taken, gone."""
    if not row or row["revoked"]:
        return "gone"
    job = q("SELECT id, status FROM jobs WHERE id=?", (row["job_id"],), one=True)
    if not job or job["status"] != "done":
        return "gone"
    if row["owner"] == g.owner:
        return "own"
    if not row["device"]:
        return "open"
    return "mine" if row["device"] == g.device else "taken"


@app.get("/api/s/<code>")
def share_peek(code):
    """Only looks: the page asks this first, and opening (below) is a separate tap."""
    row = q("SELECT * FROM shares WHERE code=?", (code,), one=True)
    state = share_state(row)
    title = q("SELECT title FROM jobs WHERE id=?", (row["job_id"],), one=True)["title"] if state in ("open", "own") else None
    return jsonify(state=state, title=title)


@app.post("/api/s/<code>")
def share_open(code):
    from . import library
    row = q("SELECT * FROM shares WHERE code=?", (code,), one=True)
    state = share_state(row)
    if state == "open":
        # whoever gets here first has it (one statement, so two browsers opening at once can't both win)
        q("UPDATE shares SET device=?, label=?, claimed=? WHERE code=? AND device IS NULL",
          (g.device, g.device_label, time.time(), code))
        row = q("SELECT * FROM shares WHERE code=?", (code,), one=True)
        state = share_state(row)
    if state not in ("mine", "own"):
        return jsonify(state=state), 403
    if state == "mine":
        q("UPDATE shares SET opens=opens+1, seen=? WHERE code=?", (time.time(), code))
    job = job_dict(q("SELECT * FROM jobs WHERE id=?", (row["job_id"],), one=True))
    a = job["analysis"]
    return jsonify(state=state, title=job["title"], summary=a.get("summary") or "",
                   thumb=f"/thumb/{job['id']}?t={library.media_token(job['id'], 0, SHARE_MEDIA_DAYS)}" if job["thumb"] else None,
                   media=library.media_info(job, SHARE_MEDIA_DAYS))
