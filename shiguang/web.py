"""The web page and its API, accounts and privacy, the task board over HTTP."""
import base64
import gzip
import hashlib
import hmac
import html
import http.cookiejar
import ipaddress
import json
import os
import re
import requests
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import traceback
import urllib.parse
import zipfile
from io import BytesIO

from flask import Response
from flask import g
from flask import jsonify
from flask import request
from flask import send_file
from flask import send_from_directory
from flask import session
from pathlib import Path
from werkzeug.security import check_password_hash
from werkzeug.security import generate_password_hash
from .core import (ADMIN_PASSWORD, AUDIO_EXT, BOOKS_DIR, BOOK_MAX_UPLOAD, CAST_URL, EXTERNAL_PORT, HERE, INCOMPLETE, LLM_API_KEY, MEDIA, NOTES_DIR, NOTE_MAX_UPLOAD, STATE, TG_TOKEN, UA, URL_RE, VIDEO_EXT, app, bell_mark, bell_wait, db_lock, find_urls, job_dict, kv_get, kv_set, q, safe_name, secret_key)


DEVICE_COOKIE = "grabber_device"
NAME_RE = re.compile(r"[\w.\-]{1,32}")
_seen_cache = {}
BILI_QR_LOGINS = {}
BILI_QR_TTL = 180


def ensure_admin():
    """The admin account gets GRABBER_PASSWORD when it's made and whenever that setting changes; a password changed
    on the page stays until then."""
    if not ADMIN_PASSWORD:
        return
    mark = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
    if kv_get("admin_env_pw") != mark or not q("SELECT 1 FROM users WHERE name='admin'", one=True):
        q("INSERT INTO users (name, pw, admin, created) VALUES ('admin', ?, 1, ?) "
          "ON CONFLICT(name) DO UPDATE SET pw=excluded.pw, admin=1", (generate_password_hash(ADMIN_PASSWORD), time.time()))
        kv_set("admin_env_pw", mark)


def device_label(ua):
    """Short readable name for a browser, e.g. "iPhone · Safari"."""
    ua = ua or ""
    os_name = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                                   ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "设备")
    browser = next((n for k, n in (("Edg/", "Edge"), ("MicroMessenger", "微信"), ("CriOS", "Chrome"), ("FxiOS", "Firefox"),
                                   ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "浏览器")
    return f"{os_name} · {browser}"


def owner_of_device(device_id):
    row = q("SELECT user FROM devices WHERE id=?", (device_id,), one=True)
    return f"user:{row['user']}" if row and row["user"] else device_id


def phone_owner(name):
    """Owner for a job sent by the iOS shortcut from the phone called `name`."""
    row = q("SELECT device FROM phones WHERE name=?", (name,), one=True)
    if not row:
        dev = q("SELECT id FROM devices WHERE ip=? AND seen>? ORDER BY seen DESC LIMIT 1",
                (client_ip(), time.time() - 30 * 86400), one=True)
        if not dev:
            return "shortcut"  # never opened the page on this phone: admin-only
        q("INSERT OR IGNORE INTO phones (name, device, created) VALUES (?,?,?)", (name, dev["id"], time.time()))
        row = q("SELECT device FROM phones WHERE name=?", (name,), one=True)
    return owner_of_device(row["device"])


def shortcut_user(key):
    """The account whose shortcut key this is (the iOS shortcut sends it, so it works from outside too)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", key):
        return None
    row = q("SELECT name FROM users WHERE shortcut_key=?", (key,), one=True)
    return row["name"] if row else None


def is_admin(name):
    return bool((q("SELECT admin FROM users WHERE name=?", (name,), one=True) or {"admin": 0})["admin"])


def shortcut_key(name, new=False):
    row = q("SELECT shortcut_key FROM users WHERE name=?", (name,), one=True)
    if new or not row["shortcut_key"]:
        q("UPDATE users SET shortcut_key=? WHERE name=?", (secrets.token_urlsafe(18), name))
        row = q("SELECT shortcut_key FROM users WHERE name=?", (name,), one=True)
    return row["shortcut_key"]


# From outside, only these work without logging in (everything else needs an account)
PUBLIC_PATHS = ("/", "/api/login", "/api/jobs", "/api/account", "/sw.js")
login_failures = {}  # ip -> [failed attempts, first failure time]; wrong passwords and wrong shortcut keys


def locked_out(ip):
    fails, since = login_failures.get(ip, (0, time.time()))
    return fails >= 10 and time.time() - since < 15 * 60


def account_locked(name):
    """From outside, an account also locks after 10 wrong passwords in 15 min, whatever the addresses they came
    from (at home it still opens, so this can't lock the owner out of their own page)."""
    return g.external and locked_out("account:" + name)


def note_failure(ip):
    fails, since = login_failures.get(ip, (0, time.time()))
    recent = time.time() - since < 15 * 60
    login_failures[ip] = (fails + 1 if recent else 1, since if recent else time.time())
    time.sleep(1)  # slow down guessing


def is_external():
    return str(request.environ.get("SERVER_PORT")) == str(EXTERNAL_PORT)


def client_ip():
    # Through the tunnel the peer is 127.0.0.1; Caddy on the VPS passes the real address in
    # X-Forwarded-For, which waitress (trusting 127.0.0.1) turns into the remote address
    return request.remote_addr


@app.before_request
def identify():
    g.external = is_external()
    # Browsers name the page a request comes from in Origin: another site posting here (a page at home making the
    # browser send links or delete things) is refused. The shortcuts and the Mac worker send no Origin.
    origin = request.headers.get("Origin")
    cross_site = (request.method not in ("GET", "HEAD", "OPTIONS") and bool(origin)
                  and urllib.parse.urlsplit(origin).netloc != request.host)
    # The iOS / Android shortcut (not the page): a POST to /api/add with a key, or without cookies. (iOS keeps the
    # cookies a response set, so a shortcut can come with one: those are ignored, and none are set for it.)
    g.shortcut = False
    if request.path == "/api/add" and request.method == "POST":
        body = request.get_json(silent=True) or {}

        def field(k):  # also from a body that isn't valid JSON (see add_post)
            m = re.search(rf'"{k}"\s*:\s*"([^"]{{1,64}})"', request.get_data(as_text=True))
            return str(body.get(k, "")).strip() or (m.group(1) if m else "")
        phone, key = field("device")[:60], field("key")
        g.shortcut = bool(key) or not request.cookies
    if g.shortcut:
        g.device, g.new_device, g.user, g.admin = "", False, None, False
        user = None
        if key:
            if locked_out(client_ip()):
                return Response("尝试次数太多，请 15 分钟后再试", mimetype="text/plain", status=429)
            user = shortcut_user(key)
            if user and g.external and is_admin(user):
                return Response("管理员账号只能在家里用", mimetype="text/plain", status=403)
            if not user:
                note_failure(client_ip())
                return Response("快捷指令密钥不对：在拾光的账号页里复制新的密钥，重新安装快捷指令", mimetype="text/plain", status=403)
        elif cross_site:  # without a key only from home, and never a web page making a browser post here
            return Response("cross-site request", mimetype="text/plain", status=403)
        elif g.external:  # from outside, only a shortcut with its account's key
            got = ", ".join(body) if body else f"{request.content_type or '?'} {request.content_length or 0} B"
            app.logger.warning("shortcut from outside without a key: %s, %s", got, request.headers.get("User-Agent"))
            return Response(f"没收到快捷指令密钥（收到：{got}）。请在账号页重新安装「发送到拾光（外网）」",
                            mimetype="text/plain", status=401)
        g.owner = f"user:{user}" if user else phone_owner(phone) if phone else "shortcut"
        g.device_label = f"📱 {phone}" if phone else "快捷指令"
        return
    if cross_site:
        return jsonify(error="cross-site request"), 403
    if request.path.startswith("/api/compute/"):  # the Mac worker: token, no cookie, not a browser
        g.device, g.new_device, g.user, g.admin, g.owner = "", False, None, False, None
        return
    g.device = request.cookies.get(DEVICE_COOKIE) or ""
    g.new_device = not re.fullmatch(r"[0-9a-f]{32}", g.device)
    if g.new_device:
        g.device = secrets.token_hex(16)
    g.user = session.get("user")
    # The login is a signed cookie, so it is also checked against the devices table: a browser removed from
    # the account (e.g. a lost phone) is logged out on its next request, not only when it logs out itself
    if g.user and (not q("SELECT 1 FROM users WHERE name=?", (g.user,), one=True)
                   or (q("SELECT user FROM devices WHERE id=?", (g.device,), one=True) or {"user": None})["user"] != g.user):
        session.clear()
        g.user = None
    g.admin = bool(g.user and q("SELECT admin FROM users WHERE name=?", (g.user,), one=True)["admin"])
    if g.admin and g.external:  # the admin sees everyone's things: only from home
        session.clear()
        g.user, g.admin = None, False
    g.owner = f"user:{g.user}" if g.user else g.device
    if g.external and not g.user:
        # No anonymous device mode on the internet: show the page and the login form, nothing else
        if request.path == "/api/jobs":
            return jsonify(jobs=[], login_required=True, external=True, user=None, admin=False,
                           disk={"free": 0, "total": 0}, features={}, privacy={})
        if request.path == "/api/account":
            return jsonify(user=None, devices=[])
        if request.path not in PUBLIC_PATHS and not request.path.startswith(("/static/", "/play/", "/castplay/", "/subs/", "/thumb/")):
            return jsonify(error="login required", login_required=True), 401
    g.device_label = device_label(request.headers.get("User-Agent"))
    if request.path.startswith("/api/notes") and request.method == "POST":
        request.max_content_length = NOTE_MAX_UPLOAD
    if request.path.startswith("/api/books") and request.method == "POST":
        request.max_content_length = BOOK_MAX_UPLOAD
    # Remember where each browser was last seen (throttled; the page polls every 2 s)
    key = (g.device, client_ip())
    if time.time() - _seen_cache.get(key, 0) > 600:
        _seen_cache[key] = time.time()
        q("INSERT INTO devices (id, ip, seen, label) VALUES (?,?,?,?) "
          "ON CONFLICT(id) DO UPDATE SET ip=excluded.ip, seen=excluded.seen, label=excluded.label",
          (g.device, client_ip(), time.time(), g.device_label))


@app.after_request
def remember(resp):
    if getattr(g, "new_device", False):
        resp.set_cookie(DEVICE_COOKIE, g.device, max_age=10 * 365 * 86400, httponly=True, samesite="Lax")
    if getattr(g, "external", False):  # only ever sent back over https (outside is https only; home is plain http)
        cookies = [c if "; Secure" in c else c + "; Secure" for c in resp.headers.getlist("Set-Cookie")]
        resp.headers.setlist("Set-Cookie", cookies)
    return resp


def scope_sql():
    return ("1", ()) if g.admin else ("owner = ?", (g.owner,))


def visible(jid):
    row = q("SELECT owner FROM jobs WHERE id=?", (jid,), one=True)
    return bool(row) and (g.admin or row["owner"] == g.owner)


def sign_in(name):
    """Log this browser into `name` and move the jobs it added anonymously into the account."""
    session.permanent = True
    session["user"] = name
    q("INSERT INTO devices (id, user, ip, seen) VALUES (?,?,?,?) "
      "ON CONFLICT(id) DO UPDATE SET user=excluded.user", (g.device, name, client_ip(), time.time()))
    q("UPDATE jobs SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    # followed uploaders too; one the account already follows stays once
    q("DELETE FROM subs WHERE owner=? AND key IN (SELECT key FROM subs WHERE owner=?)", (g.device, f"user:{name}"))
    q("UPDATE subs SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    q("UPDATE notes SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    q("UPDATE OR IGNORE watch SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    # the books on this browser's shelf and where it was in them
    q("UPDATE books SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    for t in ("book_read", "book_marks", "activity"):
        q(f"UPDATE OR IGNORE {t} SET owner=? WHERE owner=?", (f"user:{name}", g.device))
    # bring this device's privacy settings into the account
    dev, acc = privacy_get(g.device), privacy_get(f"user:{name}")
    privacy_set(f"user:{name}", list(dict.fromkeys(acc["tags"] + dev["tags"])), list(dict.fromkeys(acc["ids"] + dev["ids"])))


# ---------------------------------------------------------------- privacy mode

def privacy_get(owner):
    row = q("SELECT tags, ids FROM privacy WHERE owner=?", (owner,), one=True)
    return {"tags": json.loads(row["tags"]), "ids": json.loads(row["ids"])} if row else {"tags": [], "ids": []}


def privacy_set(owner, tags, ids):
    q("INSERT INTO privacy (owner, tags, ids) VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET tags=excluded.tags, ids=excluded.ids",
      (owner, json.dumps(tags, ensure_ascii=False), json.dumps(ids)))


# Privacy unlocks live only in the open page: unlocking returns a token the page keeps in memory and sends
# back as X-Privacy-Token. Reloading the page (or the token expiring) locks everything again.
privacy_tokens = {}  # token -> {"owner", "expires", "reveal"}


def privacy_token():
    tok = privacy_tokens.get(request.headers.get("X-Privacy-Token", ""))
    if tok and tok["owner"] == g.owner and tok["expires"] > time.time():
        return tok
    return None


def revealed():
    tok = privacy_token()
    return bool(tok and tok["reveal"])


def is_hidden(d, prefs):
    # hiding is by tag only (hiding single items by hand was dropped)
    aliases = kv_get("tag_aliases", {})
    hide = {aliases.get(t, t).lower() for t in prefs["tags"]}
    return any(aliases.get(t, t).lower() in hide for t in (d.get("analysis") or {}).get("tags") or [])


def hidden_ids():
    """Videos this page must not mention right now (privacy mode, not revealed): none of them, nor their counts."""
    prefs = privacy_get(g.owner)
    if not prefs["tags"] or revealed():
        return set()
    scope, scope_args = scope_sql()
    return {r["id"] for r in q(f"SELECT id, analysis FROM jobs WHERE {scope}", scope_args)
            if is_hidden({"analysis": json.loads(r["analysis"] or "{}")}, prefs)}


def hidden_subs(skip=None):
    """Followed uploaders this page must not name right now (privacy mode, not revealed): adult sites', and any with
    a hidden video."""
    prefs = privacy_get(g.owner)
    if not prefs["tags"] or revealed():
        return set()
    skip = hidden_ids() if skip is None else skip
    out = {r["id"] for r in q("SELECT id, platform FROM subs") if r["platform"] in channels.ADULT_PLATFORMS}
    if skip:
        out |= {int(r["source"][4:]) for r in q(f"SELECT DISTINCT source FROM jobs WHERE source LIKE 'sub:%' AND id IN "
                                                 f"({','.join('?' * len(skip))})", tuple(skip))}
    return out


def sub_names():
    """Names of the followed uploaders this page may mention (for the AI reading a search or a question)."""
    hide = hidden_subs()
    return "、".join(r["name"] for r in q("SELECT id, name FROM subs WHERE owner=? OR ?", (g.owner, int(g.admin)))
                    if r["id"] not in hide)


@app.post("/api/privacy/unlock")
def privacy_unlock():
    """Open the hidden privacy menu (reached by tapping the avatar three times) with the account password."""
    if not g.user:
        return jsonify(error="先登录账号"), 403
    key = f"privacy:{g.user}"
    fails, since = login_failures.get(key, (0, time.time()))
    if fails >= 5 and time.time() - since < 15 * 60:
        return jsonify(error="尝试次数太多，请 15 分钟后再试"), 429
    row = q("SELECT pw FROM users WHERE name=?", (g.user,), one=True)
    if not row or not check_password_hash(row["pw"], str((request.get_json(silent=True) or {}).get("password", ""))):
        time.sleep(1)
        recent = time.time() - since < 15 * 60
        login_failures[key] = (fails + 1 if recent else 1, since if recent else time.time())
        return jsonify(error="密码不对"), 403
    login_failures.pop(key, None)
    for t in [t for t, v in privacy_tokens.items() if v["expires"] < time.time()]:
        privacy_tokens.pop(t, None)
    token = secrets.token_urlsafe(24)
    privacy_tokens[token] = {"owner": g.owner, "expires": time.time() + 30 * 60, "reveal": False}
    return jsonify(token=token, expires_in=30 * 60)


@app.get("/api/privacy")
def privacy_info():
    if not privacy_token():
        return jsonify(error="locked"), 403
    prefs = privacy_get(g.owner)
    return jsonify(**prefs, revealed=revealed(), library_tags=llm.library_tags()[:300])


@app.post("/api/privacy")
def privacy_update():
    """{"tags": [...]} sets the hidden tags; {"reveal": true/false} shows or hides them in this page."""
    tok = privacy_token()
    if not tok:
        return jsonify(error="locked"), 403
    body = request.get_json(silent=True) or {}
    prefs = privacy_get(g.owner)
    if isinstance(body.get("tags"), list):
        prefs["tags"] = list(dict.fromkeys(str(t).strip()[:40] for t in body["tags"] if str(t).strip()))[:200]
    privacy_set(g.owner, prefs["tags"], [])
    if "reveal" in body:
        tok["reveal"] = bool(body["reveal"])
    return jsonify(ok=True)


def credentials():
    body = request.get_json(silent=True) or {}
    return str(body.get("name", "")).strip(), str(body.get("password", ""))


@app.post("/api/login")
def login():
    ip = client_ip()
    if locked_out(ip):
        return jsonify(error="尝试次数太多，请 15 分钟后再试"), 429
    name, pw = credentials()
    if g.external and is_admin(name):  # (refused before the password is checked: no guessing it from outside)
        return jsonify(error="管理员账号只能在家里的网络登录"), 403
    if account_locked(name):
        return jsonify(error="这个账号密码错太多次，请 15 分钟后再试（在家里的网络可以直接登录）"), 429
    row = q("SELECT pw FROM users WHERE name=?", (name,), one=True)
    if not row or not check_password_hash(row["pw"], pw):
        if g.external:
            fails, since = login_failures.get("account:" + name, (0, time.time()))
            recent = time.time() - since < 15 * 60
            login_failures["account:" + name] = (fails + 1 if recent else 1, since if recent else time.time())
        note_failure(ip)
        return jsonify(error="用户名或密码不对"), 403
    login_failures.pop(ip, None)
    sign_in(name)
    return jsonify(ok=True)


@app.get("/api/name-available")
def name_available():
    name = request.args.get("name", "").strip()
    return jsonify(available=bool(NAME_RE.fullmatch(name)) and not q("SELECT 1 FROM users WHERE name=?", (name,), one=True))


@app.post("/api/register")
def register():
    if g.external:  # accounts are made at home; strangers on the internet can't sign up
        return jsonify(error="只能在家里的网络注册新账号"), 403
    name, pw = credentials()
    if not NAME_RE.fullmatch(name):
        return jsonify(error="用户名：1-32 个字母、数字、汉字或 . - _"), 400
    if len(pw) < 8:  # the page can be reached from the internet
        return jsonify(error="密码至少 8 位"), 400
    try:
        with db_lock:
            core.DB.execute("INSERT INTO users (name, pw, created) VALUES (?,?,?)",
                       (name, generate_password_hash(pw), time.time()))
            core.DB.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="这个用户名已被注册"), 409
    sign_in(name)
    return jsonify(ok=True)


@app.get("/api/account")
def account():
    """Devices tied to the logged-in account: browsers that logged in, and iPhones using the shortcut."""
    if not g.user:
        return jsonify(user=None, devices=[])
    # The same browser gets a new cookie for every address it opens the page by (192.168.3.200 and
    # pi-gateway.local are two sites to it), so it shows up as several devices: one row per kind of browser
    # on one IP, removed together
    devices, rows = [], {}
    for d in q("SELECT * FROM devices WHERE user=? ORDER BY seen DESC", (g.user,)):
        phones = [p["name"] for p in q("SELECT name FROM phones WHERE device=?", (d["id"],))]
        row = rows.get((d["label"], d["ip"]))
        if row:
            row["ids"].append(d["id"])
            row["current"] = row["current"] or d["id"] == g.device
            row["phones"] += [p for p in phones if p not in row["phones"]]
            continue
        row = rows[(d["label"], d["ip"])] = {"id": d["id"], "ids": [d["id"]], "label": d["label"] or "设备", "seen": d["seen"],
                                             "current": d["id"] == g.device, "phones": phones}
        devices.append(row)
    # Jobs from before devices were recorded have no device; JSON keys must be strings
    counts = {r["device"] or "未知设备": r["n"] for r in q("SELECT device, COUNT(*) n FROM jobs WHERE owner=? GROUP BY device",
                                                       (f"user:{g.user}",))}
    return jsonify(user=g.user, devices=devices, counts=counts, shortcut_key=shortcut_key(g.user),
                   public_url=PUBLIC_URL, remote_shortcut=remote_shortcut_state(g.user, make=False))


@app.post("/api/shortcut-key/new")
def new_shortcut_key():
    """A new key for the iOS shortcut (a phone was lost): shortcuts with the old key stop working."""
    if not g.user:
        return jsonify(error="not logged in"), 403
    key = shortcut_key(g.user, new=True)
    remote_shortcut_state(g.user)  # the Mac makes the shortcut for the new key
    return jsonify(shortcut_key=key)


@app.post("/api/devices/<device_id>/remove")
def remove_device(device_id):
    """Untie a browser (and the iPhone shortcut that goes with it) from the account; its jobs stay."""
    if not g.user:
        return jsonify(error="not logged in"), 403
    ids = device_id.split(",")  # one row in the list can be several cookies of the same browser
    for i in ids:
        q("UPDATE devices SET user=NULL WHERE id=? AND user=?", (i, g.user))
    if g.device in ids:
        session.clear()
    return jsonify(ok=True)


@app.post("/api/password")
def change_password():
    """Change the logged-in account's password: only from home (from outside a stolen login could lock the owner out)."""
    if not g.user:
        return jsonify(error="先登录"), 403
    if g.external:
        return jsonify(error="只能在家里的网络改密码"), 403
    body = request.get_json(silent=True) or {}
    old, new = str(body.get("old", "")), str(body.get("new", ""))
    if locked_out(client_ip()):
        return jsonify(error="尝试次数太多，请 15 分钟后再试"), 429
    if not check_password_hash(q("SELECT pw FROM users WHERE name=?", (g.user,), one=True)["pw"], old):
        note_failure(client_ip())
        return jsonify(error="原密码不对"), 403
    if len(new) < 8:
        return jsonify(error="新密码至少 8 位"), 400
    q("UPDATE users SET pw=? WHERE name=?", (generate_password_hash(new), g.user))
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    # This browser goes back to being anonymous; the account keeps its jobs
    q("UPDATE devices SET user=NULL WHERE id=?", (g.device,))
    session.clear()
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(HERE / "static", name, max_age=30 * 86400)


@app.get("/sw.js")
def service_worker():
    """Keeps the page itself for reading cached books without the Pi (only works over https; see static/sw.js)."""
    resp = send_from_directory(HERE / "static", "sw.js", mimetype="text/javascript", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(HERE / "static", "favicon.ico", max_age=30 * 86400, mimetype="image/x-icon")


@app.get("/shortcut")
def shortcut():
    """iOS share-sheet shortcut for home: share a link from any app and it goes to /api/add at the Pi's LAN address."""
    return send_from_directory(HERE, "send-to-pi.shortcut", as_attachment=True, download_name="发送到拾光.shortcut")


# The shortcut for outside carries the account's key and the public address, so each account gets its own; iOS only
# opens signed shortcuts, and only a Mac can sign, so the Mac worker makes it (a `shortcut` task) and the Pi keeps it
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://example.org:8443 (not in the code)


def remote_shortcut(name):
    """(file, task target) of this account's shortcut for outside, for its current key."""
    h = hashlib.sha256(shortcut_key(name).encode()).hexdigest()[:20]
    return tasks.SHORTCUTS / f"{h}.shortcut", f"shortcut:{h}"


def remote_shortcut_state(name, make=True):
    """"ready", "making", "failed: why" or "off" (no public address); asks the Mac for it when it's missing."""
    if not PUBLIC_URL:
        return "off"
    f, target = remote_shortcut(name)
    if f.exists():
        return "ready"
    t = q("SELECT state, error FROM tasks WHERE kind='shortcut' AND target=?", (target,), one=True)
    if t and t["state"] == "failed" and not make:
        return "failed: " + (t["error"] or "")[:200]
    if make and (not t or t["state"] in ("done", "failed")):
        board.publish("shortcut", target, 95, force=True,
                      payload={"url": PUBLIC_URL + "/api/add", "key": shortcut_key(name), "file": f.name,
                               "title": f"快捷指令 · {name}"})
    return "making"


@app.get("/shortcut/remote")
def shortcut_remote():
    """This account's shortcut for outside (public address + key); while the Mac is still making it: 202."""
    if not g.user:
        return jsonify(error="先登录"), 403
    state = remote_shortcut_state(g.user)
    if state != "ready":
        return jsonify(state=state), 202 if state == "making" else 503
    return send_file(remote_shortcut(g.user)[0], as_attachment=True, download_name="发送到拾光（外网）.shortcut")


ADD_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>发送到拾光</title><body style="font:15px system-ui;margin:16px;word-break:break-all">{}</body>"""


@app.route("/add", methods=["GET", "POST"])
def add_get():
    """Target for the bookmarklet: /add?url=... asks first, the button (a POST from this page) adds it."""
    url = (request.args.get("url") or request.form.get("url") or "").strip()
    if not URL_RE.fullmatch(url):
        return ADD_PAGE.format("没有链接"), 400
    shown = html.escape(url)
    if request.method == "GET":
        return ADD_PAGE.format(f"<p>{shown}</p><form method=post><input type=hidden name=url value=\"{shown}\">"
                               "<button style='font-size:17px;padding:6px 18px'>发送到拾光</button></form>"
                               "<script>document.querySelector('button').focus()</script>")
    if (bad := internal_link(url)):
        return ADD_PAGE.format(html.escape(bad)), 400
    if channels.channel_of(url):
        channels.add_sub(url, g.owner, g.device_label)
        return ADD_PAGE.format(f"开始追更 – {shown}<script>setTimeout(() => close(), 1200)</script>")
    jid = pipeline.add_job(url, source="web", owner=g.owner, device=g.device_label)
    return ADD_PAGE.format(f"已加入 #{jid} – {shown}<script>setTimeout(() => close(), 1200)</script>")


def internal_link(url):
    """Links into the home network (the router, the Pi's own services) aren't fetched: the Pi would be reaching
    them for whoever sent the link. Returns why, or None for a normal link."""
    if not url.lower().startswith(("http://", "https://")):
        return None
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return None  # doesn't resolve: the download fails on its own
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        # (198.18/15 is what a proxy's fake-IP DNS hands out for real sites)
        if not ip.is_global and not (ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15")):
            return f"不下载家里网络内部的地址：{host}"
    return None


@app.post("/api/add")
def add_post():
    ids = []
    if "torrent" in request.files:
        f = request.files["torrent"]
        path = STATE / "uploads" / f"{int(time.time())}-{safe_name(f.filename)}"
        path.parent.mkdir(exist_ok=True)
        f.save(path)
        ids.append(pipeline.add_job("torrent-file:" + str(path), owner=g.owner, device=g.device_label))
    body = request.get_json(silent=True) or {}
    raw = "" if body or request.form else request.get_data(as_text=True)
    text = request.form.get("text") or body.get("text", "") or raw
    device = str(body.get("device", "")).strip()[:60]
    if raw and not device:
        # Android's HTTP Shortcuts pastes shared text into a JSON template without escaping it, so a title
        # with quotes breaks the JSON; still take the links and the device name out of the raw body
        m = re.search(r'"device"\s*:\s*"([^"]{1,60})"', raw)
        device = m.group(1) if m else ""
    source = f"shortcut:{device}" if device else "web"
    try:  # 追更 page: follow with a filter (which videos to cache)
        flt = request.form.get("filter") or body.get("filter")
        flt = channels.clean_filter(json.loads(flt) if isinstance(flt, str) else flt) if flt else None
    except ValueError as e:
        return jsonify(error=str(e)), 400
    hows, subs, shelf = [], [], []
    blocked = []
    for u in find_urls(text):
        if (bad := internal_link(u)):
            blocked.append(bad)
            continue
        if channels.channel_of(u):  # an uploader's page: follow it instead of downloading the page
            sid, how = channels.add_sub(u, g.owner, g.device_label, flt)
            subs.append({"id": sid, "how": how})
            continue
        if books.is_book_url(u):  # a link to an .epub / .pdf / .txt / .mobi file: onto the shelf
            bid, how = books.add_url(g.owner, g.device_label, u)
            if how == "new":
                board.publish("book_import", f"book:{bid}", 80, force=True)
            shelf.append({"id": bid, "how": how})
            continue
        jid, how = pipeline.add_job_ex(u, source=source, owner=g.owner, device=g.device_label)
        ids.append(jid)
        hows.append(how)
    if not ids and (subs or shelf):
        if g.shortcut:
            return Response("，".join(x for x in (f"开始追更 {len(subs)} 个 UP 主，新视频会自动下载" if subs else "",
                                                  f"{len(shelf)} 本电子书加到书架了" if shelf else "") if x), mimetype="text/plain")
        return jsonify(ids=[], duplicates=[], linked=[], subs=subs, books=shelf)
    if blocked and not ids and not subs and not shelf:
        return (Response(blocked[0], mimetype="text/plain", status=400) if g.shortcut
                else (jsonify(error=blocked[0]), 400))
    if not ids and g.shortcut and text.strip():
        # the iOS / Android shortcut shared plain text (no link): keep it as a 随记
        now = time.time()
        q("INSERT INTO notes (owner, text, device, created, updated) VALUES (?,?,?,?,?)",
          (g.owner, text.strip()[:20000], g.device_label, now, now))
        return Response("没有链接，已经记到「随记」里", mimetype="text/plain")
    if not ids:
        if g.shortcut:
            return Response("没找到链接", mimetype="text/plain", status=400)
        return jsonify(error="No link found"), 400
    dupes = [i for i, h in zip(ids, hows) if h == "duplicate"]
    linked = [i for i, h in zip(ids, hows) if h == "linked"]
    if g.shortcut:
        # iOS shortcut / Android HTTP Shortcuts show the reply as a notification: keep it readable
        new = len(ids) - len(dupes) - len(linked)
        msg = "，".join(x for x in (f"开始追更 {len(subs)} 个 UP 主" if subs else "", f"{len(shelf)} 本电子书加到书架了" if shelf else "",
                                    f"开始下载 {new} 个" if new else "",
                                    f"{len(linked)} 个别人已经下过，直接加进来了" if linked else "",
                                    f"{len(dupes)} 个已经在拾光里了" if dupes else "") if x)
        return Response(msg, mimetype="text/plain")
    return jsonify(ids=ids, duplicates=dupes, linked=linked, subs=subs, books=shelf)


def owner_label(o):
    o = o or ""
    return o[5:] if o.startswith("user:") else "快捷指令（未识别）" if o == "shortcut" else "匿名设备" if o else "旧任务"


def category_counts():
    """How many finished videos each category chip has (all of them, not only the page loaded)."""
    scope, args = scope_sql()
    out = {}
    for r in q(f"SELECT json_extract(analysis, '$.library') lib, json_extract(analysis, '$.folder') folder, COUNT(*) n "
               f"FROM jobs WHERE {scope} AND status IN ('done', 'linked') GROUP BY lib, folder", args):
        key = (r["folder"] or "Other") if r["lib"] == "Videos" else (r["lib"] or "Other")
        out[key] = out.get(key, 0) + r["n"]
    return out


@app.get("/api/jobs")
def jobs():
    term = request.args.get("q", "").strip()
    term = kv_get("tag_aliases", {}).get(term, term)  # a merged-away tag searches for its canonical form
    wanted_tags = tag_query(term)  # "#标签": only by tags
    # Sort before the finished-item page is cut off. The page repeats this for
    # its mixed cards (unfinished and resume items).
    sort = request.args.get("sort", "added")
    order = {
        "published": "json_extract(analysis, '$.published') DESC, id DESC",
        "added": "created DESC, id DESC",
        "title": "COALESCE(json_extract(analysis, '$.title'), title, '') COLLATE NOCASE, id DESC",
        "author": "(COALESCE(json_extract(analysis, '$.creator'), '') = ''), "
                  "COALESCE(json_extract(analysis, '$.creator'), '') COLLATE NOCASE, "
                  "COALESCE(json_extract(analysis, '$.title'), title, '') COLLATE NOCASE, id DESC",
    }.get(sort, "created DESC, id DESC")
    scope, scope_args = scope_sql()
    scope += " AND status != 'cancelled'"  # cancelled jobs are hidden
    sub = request.args.get("sub", "")
    # every unfinished job (queued / downloading / failed) is always sent; finished ones a page at a time
    limit = int(request.args["limit"]) if request.args.get("limit", "").isdigit() else 100
    if sub.isdigit():  # one followed uploader's videos
        scope, scope_args, limit = scope + " AND source = ?", (*scope_args, f"sub:{sub}"), max(limit, 1000)
    cat = request.args.get("cat", "")
    if cat:  # a category chip: every video in it (a small category can be spread over many pages of the newest)
        field = "folder" if cat not in ("Movies", "TV", "Music", "Downloads") else "library"
        scope += f" AND json_extract(analysis, '$.{field}') = ?" + (" AND json_extract(analysis, '$.library') = 'Videos'" if field == "folder" else "")
        scope_args, limit = (*scope_args, cat), max(limit, 2000)
    more = False
    parsed = None
    # A sentence ("上个月小王讲伊朗、画面里有地图的片段"): an AI reads it into keywords, what's on screen, an
    # uploader and dates (the page asks for this once typing pauses; `exact` searches the words as typed)
    if term and not wanted_tags and request.args.get("nl") == "1" and LLM_API_KEY and len(term) >= 6:
        try:
            ups = sub_names()
            parsed = llm.parse_query(term, ups, time.strftime("%Y-%m-%d"))
        except Exception:
            traceback.print_exc()
        if parsed and not (parsed["keywords"] or parsed["visual"]) and not (parsed["uploader"] or parsed["since"]):
            parsed = None
    if wanted_tags:
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND {' AND '.join(['analysis LIKE ?'] * len(wanted_tags))} ORDER BY {order}",
                 (*scope_args, *(f"%{t}%" for t in wanted_tags)))
        out = [d for d in map(job_dict, rows) if has_tags((d["analysis"] or {}).get("tags"), wanted_tags)]
    elif parsed:
        out = search.smart_search(parsed, scope, scope_args, g.owner)
    elif term:
        # Searches titles, links, summaries, key points, tags, file paths and transcripts
        like = f"%{term}%"
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND (title LIKE ? OR url LIKE ? OR analysis LIKE ? OR files LIKE ? "
                 f"OR transcript LIKE ?) ORDER BY {order} LIMIT ?", (*scope_args, *(like,) * 5, max(limit, 200)))
        # The idle-time index belongs to the job that downloaded the files; entries linked to it share it
        by_source = {}
        for r in q(f"SELECT id, ref FROM jobs WHERE {scope} AND status IN ('done', 'linked')", scope_args):
            by_source.setdefault(r["ref"] or r["id"], []).append(r["id"])
        said = {}  # lines said in the video (subtitles) or written on its cover
        for r in q("SELECT ref, part, t, src, text FROM seg WHERE kind='job' AND text LIKE ? ORDER BY ref, part, t", (like,)):
            for jid in by_source.get(r["ref"], []):
                said.setdefault(jid, []).append({"part": r["part"], "t": r["t"], "src": r["src"],
                                                 "text": search.snippet(r["text"], term, 10, 40).strip("…") if len(r["text"]) > 50 else r["text"]})
        looks = {}  # covers and frames that look like it
        for source, found in search.visual_hits(term, "job", set(by_source)).items():
            for jid in by_source[source]:
                looks[jid] = found
        known = {r["id"] for r in rows}
        extra = [i for i in dict.fromkeys([*said, *looks]) if i not in known]
        if extra:
            rows += q(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(extra))})", extra)
        out, only_looks = [], []
        for r in rows:
            d = job_dict(r)
            hits = said.get(r["id"], [])[:30]
            if not hits and d["status"] == "done" and term.lower() in (r["transcript"] or "").lower():
                hits = [{**h, "src": "字幕"} for h in search.subtitle_hits(d, r, term)]  # not indexed yet
            seen = [{"part": p, "t": t, "src": "画面" if t is not None else "封面", "text": f"看起来像「{term}」"}
                    for _, p, t, _ in looks.get(r["id"], [])[:5]]
            if term.lower() not in search.titleish(d).lower():
                # say where it was found when it isn't in the title, so the result doesn't look random
                if hits:
                    d["match_where"], d["match"] = hits[0]["src"], hits[0]["text"]
                else:
                    d["match_where"], d["match"] = next(((k, search.snippet(t, term)) for k, t in search.match_fields(d, r)
                                                         if term.lower() in t.lower()), ("", ""))
                if not d["match"] and seen:
                    d["match_where"], d["match"] = seen[0]["src"], seen[0]["text"]
                    if seen[0]["t"] is not None:
                        d["frame"] = {"part": seen[0]["part"], "t": seen[0]["t"]}  # show that moment as the cover
            d["hits"] = hits + seen
            # matched only by how it looks: after the text matches, most alike first
            (only_looks if d.get("match_where") in ("画面", "封面") and not hits else out).append(d)
        # at most a dozen: further down the list the likeness gets thin
        out += sorted(only_looks, key=lambda d: -looks[d["id"]][0][0])[:12]
    else:
        unfinished = "status IN ('queued', 'downloading', 'processing', 'linked', 'failed')"
        rows = q(f"SELECT * FROM jobs WHERE {scope} AND {unfinished}", scope_args)
        finished = q(f"SELECT * FROM jobs WHERE {scope} AND NOT {unfinished} ORDER BY {order} LIMIT ?", (*scope_args, limit + 1))
        more = len(finished) > limit
        if request.args.get("ids"):  # particular videos (opened from a digest, a note...), wherever they are
            wanted = [int(x) for x in request.args["ids"].split(",") if x.isdigit()][:50]
            finished += q(f"SELECT * FROM jobs WHERE {scope} AND id IN ({','.join('?' * len(wanted))})", (*scope_args, *wanted)) if wanted else []
            finished = list({r["id"]: r for r in finished}.values())
            limit = len(finished)
        shown = {r["id"] for r in rows + finished[:limit]}
        # videos you're in the middle of are always there, also when they're further back than the first page
        resume = [r["job_id"] for r in q("SELECT job_id FROM watch WHERE owner=? AND done=0 AND pos > 15 "
                                         "ORDER BY updated DESC LIMIT 12", (g.owner,)) if r["job_id"] not in shown]
        extra = q(f"SELECT * FROM jobs WHERE {scope} AND id IN ({','.join('?' * len(resume))})", (*scope_args, *resume)) if resume else []
        out = [job_dict(r) for r in sorted(rows + finished[:limit] + extra, key=lambda r: r["id"], reverse=True)]
    # privacy mode: hidden items aren't even sent unless they've been revealed with the password
    prefs, show_hidden = privacy_get(g.owner), revealed()
    hidden_count = 0
    kept = []
    for d in out:
        if is_hidden(d, prefs):
            hidden_count += 1
            if not show_hidden:
                continue
            d["hidden"] = True
        kept.append(d)
    out = kept
    watched = {r["job_id"]: r for r in q("SELECT * FROM watch WHERE owner=?", (g.owner,))}
    for d in out:
        w = watched.get(d["id"])
        if w:
            d["watch"] = {"part": w["part"], "pos": w["pos"], "dur": w["dur"], "done": bool(w["done"]), "at": w["updated"]}
        if d["status"] == "linked" and d.get("ref"):  # show the original download's progress
            src = q("SELECT status, stage, progress, speed, title, thumb FROM jobs WHERE id=?", (d["ref"],), one=True)
            if src:
                d.update(status=src["status"] if src["status"] in ("queued", "downloading", "processing") else "queued",
                         stage=src["stage"], progress=src["progress"], speed=src["speed"], title=d["title"] or src["title"])
        owner = d.pop("owner", None)
        d["media"] = library.media_info(d) if d["status"] == "done" else []
        d["missing"] = d["status"] == "done" and not d["media"] and any(
            Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT for f in d["files"])
        if g.admin:
            d["owner_label"] = owner_label(owner)
    usage = shutil.disk_usage(MEDIA)
    return jsonify(jobs=out, disk={"free": usage.free, "total": usage.total},
                   features={"ai": bool(LLM_API_KEY), "telegram": bool(TG_TOKEN)}, admin=g.admin, user=g.user,
                   privacy={"revealed": show_hidden}, external=g.external,  # no hidden counts on purpose
                   subs=subs_list(), sub_interval=channels.SUB_INTERVAL,
                   bili_cookie=channels.bili_cookie_state() if g.admin else None, more=more,
                   notes=q("SELECT COUNT(*) n FROM notes WHERE owner=?", (g.owner,), one=True)["n"],
                   books=q("SELECT COUNT(*) n FROM books WHERE owner=?", (g.owner,), one=True)["n"],
                   # ... and books: by title / author and by what's written in them
                   book_hits=book_hits(term) if term and not parsed and not wanted_tags else [],
                   reading=reading_now() if not term else [],
                   # a search also shows matching 随记 among the videos
                   note_hits=[notes.note_dict(r, [], []) for r in q("SELECT * FROM notes WHERE owner=? ORDER BY created DESC, id DESC",
                                                                    (g.owner,)) if has_tags(json.loads(r["tags"] or "[]"), wanted_tags)][:50]
                   if wanted_tags else
                   [notes.note_dict(r, m, seen) for r, m, seen in
                    search.notes_search(" ".join(parsed["keywords"]) if parsed and parsed["keywords"] else term)[:50]]
                   if term else [],
                   understood=search.understood(parsed) if parsed else None,
                   partial=bool(parsed and out and out[0].get("partial")), cats=category_counts())


def tag_query(term):
    """"#猫" or "#猫 #狗" (also ＃): the tags to search by, and nothing else; [] for an ordinary search."""
    if not term.startswith(("#", "＃")):
        return []
    aliases = kv_get("tag_aliases", {})
    return [aliases.get(t, t).lower() for t in re.findall(r"[#＃]([^\s#＃]+)", term)]


def has_tags(tags, wanted):
    """Every wanted tag is (part of) one of these tags: #成人 finds 成人视频, case doesn't matter."""
    tags = [str(t).lower() for t in tags or []]
    return bool(wanted) and all(any(w in t for t in tags) for w in wanted)


def reading_now():
    """继续阅读: the books you're in the middle of, last read first."""
    reads = q("SELECT * FROM book_read WHERE owner=? AND done=0 AND pct > 0 ORDER BY updated DESC LIMIT 8", (g.owner,))
    rows = {r["id"]: r for r in q(f"SELECT * FROM books WHERE owner=? AND id IN ({','.join('?' * len(reads))})",
                                  (g.owner, *[r["book"] for r in reads]))} if reads else {}
    return [books.book_dict(rows[r["book"]], r) for r in reads if r["book"] in rows]


def book_hits(term):
    found = books.search(g.owner, term, limit=60)
    if not found:
        return []
    reads = {r["book"]: r for r in q("SELECT * FROM book_read WHERE owner=?", (g.owner,))}
    out = []
    for r in q(f"SELECT * FROM books WHERE id IN ({','.join('?' * len(found))})", list(found)):
        d = books.book_dict(r, reads.get(r["id"]))
        d["hits"] = found[r["id"]][:5]
        out.append(d)
    return sorted(out, key=lambda d: -d["updated"])[:20]


def subs_list():
    """Followed uploaders visible here, with how many of their videos are downloaded / waiting."""
    where, args = ("1", ()) if g.admin else ("owner = ?", (g.owner,))
    counts = {}
    for r in q("SELECT source, status, COUNT(*) n FROM jobs WHERE source LIKE 'sub:%' GROUP BY source, status"):
        c = counts.setdefault(r["source"], {})
        c[r["status"]] = c.get(r["status"], 0) + r["n"]
    out, hide = [], hidden_subs()
    for r in q(f"SELECT * FROM subs WHERE {where} ORDER BY id DESC", args):
        if r["id"] in hide:
            continue
        c = counts.get(f"sub:{r['id']}", {})
        out.append({"id": r["id"], "platform": r["platform"], "name": r["name"], "url": r["url"],
                    "avatar": bool(r["avatar"]), "total": r["total"], "backfill": r["backfill"],
                    # `everything` stays set after a failed full backfill so it
                    # can resume later. It does not mean a request is still in
                    # flight; treating it as one hid the useful error message.
                    "checked": r["checked"], "error": r["error"], "pending": r["checked"] is None,
                    "filter": json.loads(r["filter"]) if r["filter"] else None,
                    "done": c.get("done", 0), "active": sum(c.get(k, 0) for k in ("queued", "downloading", "processing", "linked")),
                    "failed": c.get("failed", 0), "next": (r["checked"] or time.time()) + (channels.SUB_RETRY if r["error"] else channels.SUB_INTERVAL),
                    **({"owner_label": owner_label(r["owner"])} if g.admin else {})})
    return out


@app.post("/api/bilibili/cookies")
def upload_bilibili_cookies():
    """Install a Netscape cookies.txt exported from the administrator's B站 browser."""
    if not g.admin:
        return jsonify(error="只有管理员能更新 B站登录状态"), 403
    upload = request.files.get("cookies")
    if not upload or not upload.filename:
        return jsonify(error="请选择导出的 cookies.txt"), 400
    raw = upload.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        return jsonify(error="cookies.txt 太大了"), 400
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(error="cookies.txt 必须是 UTF-8 文本"), 400
    tmp_name = None
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=STATE, prefix="cookies-", delete=False) as tmp:
            tmp.write(text)
            tmp_name = tmp.name
        jar = http.cookiejar.MozillaCookieJar(tmp_name)
        jar.load(ignore_discard=True, ignore_expires=True)
        if not channels.BILI_LOGIN_COOKIES <= channels.bili_cookie_names(jar):
            return jsonify(error="这不是完整的 B站登录 Cookie；请重新导出包含 SESSDATA、bili_jct 和 DedeUserID 的 cookies.txt"), 400
        channels.save_bili_cookies(jar)
    except (OSError, http.cookiejar.LoadError, ValueError):
        return jsonify(error="cookies.txt 格式不对，请用 Netscape 格式重新导出"), 400
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
    # A fresh session is made for every listing; clear failures so the worker
    # picks all B站 subscriptions up on its next pass.
    q("UPDATE subs SET checked=NULL, error='' WHERE platform='bilibili'")
    return jsonify(ok=True)


def bili_login_required():
    if not g.admin:
        return jsonify(error="只有管理员能更新 B站登录状态"), 403


@app.post("/api/bilibili/login/qr")
def bilibili_login_qr():
    if denied := bili_login_required():
        return denied
    BILI_QR_LOGINS.clear()  # only the current administrator login needs to exist
    try:
        import qrcode
        client = requests.Session()
        client.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        body = client.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate", timeout=15).json()
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or "B站没有返回二维码")
        data = body["data"]
        image = qrcode.make(data["url"])
        png = BytesIO()
        image.save(png, format="PNG")
    except Exception as e:
        return jsonify(error=f"二维码获取失败：{e}"), 502
    lid = secrets.token_urlsafe(24)
    BILI_QR_LOGINS[lid] = {"client": client, "key": data["qrcode_key"], "expires": time.time() + BILI_QR_TTL}
    return jsonify(login=lid, qr="data:image/png;base64," + base64.b64encode(png.getvalue()).decode(), expires=BILI_QR_TTL)


@app.get("/api/bilibili/login/qr/<login>")
def bilibili_login_qr_poll(login):
    if denied := bili_login_required():
        return denied
    item = BILI_QR_LOGINS.get(login)
    if not item or item["expires"] < time.time():
        BILI_QR_LOGINS.pop(login, None)
        return jsonify(status="expired")
    try:
        body = item["client"].get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                                  params={"qrcode_key": item["key"]}, timeout=15).json()
        data = body.get("data") or {}
        status = data.get("code")
        if body.get("code") != 0:
            raise RuntimeError(body.get("message") or "B站没有返回登录状态")
        if status == 0:
            # B站 has used both Set-Cookie and a cross-domain URL with the
            # same cookies in its query string. Keep the latter too: requests
            # follows that redirect, so otherwise the one useful response can
            # be lost after a successful scan.
            cookies = item["client"].cookies.copy()
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(data.get("url") or "").query)
            expires = query.get("Expires", [None])[0]
            for name in channels.BILI_LOGIN_COOKIES | {"DedeUserID__ckMd5", "sid"}:
                if value := query.get(name, [None])[0]:
                    cookies.set(name, value, domain=".bilibili.com", path="/", expires=int(expires) if str(expires).isdigit() else None)
            channels.save_bili_cookies(cookies)
            BILI_QR_LOGINS.pop(login, None)
            q("UPDATE subs SET checked=NULL, error='' WHERE platform='bilibili'")
            return jsonify(status="done")
        return jsonify(status={86101: "waiting", 86090: "scanned", 86038: "expired"}.get(status, "waiting"))
    except (OSError, RuntimeError, ValueError, requests.RequestException) as e:
        return jsonify(error=f"登录状态检查失败：{e}"), 502


def sub_visible(sid):
    row = q("SELECT owner FROM subs WHERE id=?", (sid,), one=True)
    return bool(row) and (g.admin or row["owner"] == g.owner)


@app.post("/api/subs/<int:sid>/<action>")
def sub_action(sid, action):
    """refresh: check for new videos now · all: download every video of the uploader (that passes its filter) ·
    filter: change which videos are cached · delete: stop following
    (videos already downloaded stay; ones still waiting in the queue are dropped)"""
    if not sub_visible(sid):
        return jsonify(error="not found"), 404
    if action == "refresh":
        q("UPDATE subs SET checked=NULL WHERE id=?", (sid,))
    elif action == "all":
        q("UPDATE subs SET everything=1, checked=NULL WHERE id=?", (sid,))
    elif action == "filter":  # {"filter": {...} | null}: which of its videos to cache from now on
        try:
            channels.set_filter(sid, channels.clean_filter((request.get_json(silent=True) or {}).get("filter") or {"op": "and", "items": []}))
        except ValueError as e:
            return jsonify(error=str(e)), 400
    elif action == "delete":
        q("DELETE FROM subs WHERE id=?", (sid,))
        for r in q("SELECT id FROM jobs WHERE source=? AND status='queued'", (f"sub:{sid}",)):
            pipeline.cancel_job(r["id"])
        (channels.AVATARS / f"{sid}.jpg").unlink(missing_ok=True)
    else:
        return jsonify(error="unknown action"), 400
    return jsonify(ok=True)


@app.get("/subavatar/<int:sid>")
def sub_avatar(sid):
    row = q("SELECT avatar FROM subs WHERE id=?", (sid,), one=True)
    if not row or not row["avatar"] or not Path(row["avatar"]).exists():
        return "", 404
    return send_file(row["avatar"], max_age=86400)


def compute_auth():
    if g.external or not board.COMPUTE_TOKEN or not hmac.compare_digest(request.headers.get("X-Compute-Token", ""), board.COMPUTE_TOKEN):
        return jsonify(error="forbidden"), 403
    return None


def _worker():
    return str((request.get_json(silent=True) or {}).get("worker") or request.args.get("worker") or "")[:40] or None


@app.post("/api/tasks/publish")
def api_publish():
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    kind, target = str(body.get("kind", "")), str(body.get("target", ""))
    if not board.TASK_KINDS.get(kind, {}).get("remote"):
        return jsonify(error=f"may not publish {kind!r}"), 403
    try:
        tid = board.publish(kind, target, int(body.get("priority") or 20), by=_worker() or "remote", force=bool(body.get("force")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=tid)


@app.post("/api/tasks/claim")
def api_claim():
    """The next task this worker can do (caps), waiting up to `wait` seconds (at most 25) for one to come in."""
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    worker, caps = _worker(), [str(c) for c in body.get("caps") or []]
    if not worker:
        return jsonify(error="worker name missing"), 400
    if body.get("paused"):
        # still there, just not taking tasks for a while (a game in front): others keep leaving it its kinds,
        # until it's been paused for PREFER_WAIT
        row = q("SELECT paused, seen FROM workers WHERE name=?", (worker,), one=True)
        since = json.loads(row["paused"]).get("since") if row and row["paused"] else time.time()
        if time.time() - since < board.PREFER_WAIT_PAUSED:
            board.seen_worker(worker, caps, paused=json.dumps({"why": str(body["paused"])[:40], "since": since}))
        return jsonify(task=None)
    deadline = time.time() + min(float(body.get("wait") or 0), 25)
    while True:
        mark = bell_mark("tasks")
        task = board.claim_task(worker, caps)
        if task or time.time() >= deadline:
            return jsonify(task=task)
        bell_wait("tasks", mark, max(0.1, deadline - time.time()))


@app.post("/api/tasks/<int:tid>/heartbeat")
def api_heartbeat(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    return jsonify(ok=board.heartbeat_task(tid, _worker(), body.get("progress")))


@app.post("/api/tasks/<int:tid>/done")
def api_done(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    if not board.complete_task(tid, _worker(), body.get("result") or {}):
        return jsonify(error="not your task (any more)"), 409
    return jsonify(ok=True)


@app.post("/api/tasks/<int:tid>/fail")
def api_fail(tid):
    if (denied := compute_auth()):
        return denied
    body = request.get_json(silent=True) or {}
    return jsonify(ok=board.fail_task(tid, _worker(), body.get("error", ""), retry=bool(body.get("retry", True))))


@app.post("/api/tasks/ingest")
def api_ingest():
    """The Mac's drop folder (~/拾光投递): a file dropped there becomes a 随记 of `account`, with the file's date."""
    if (denied := compute_auth()):
        return denied
    request.max_content_length = NOTE_MAX_UPLOAD
    account = str(request.form.get("account", ""))
    if not q("SELECT 1 FROM users WHERE name=?", (account,), one=True):
        return jsonify(error=f"no account {account!r}"), 400
    g.owner, g.device_label = f"user:{account}", "Mac mini · 投递"
    return note_add()


@app.post("/api/tasks/ingest-book")
def api_ingest_book():
    """The Mac's drop folder: an e-book dropped there goes onto `account`'s shelf."""
    if (denied := compute_auth()):
        return denied
    request.max_content_length = BOOK_MAX_UPLOAD
    account = str(request.form.get("account", ""))
    if not q("SELECT 1 FROM users WHERE name=?", (account,), one=True):
        return jsonify(error=f"no account {account!r}"), 400
    g.owner, g.device_label = f"user:{account}", "Mac mini · 投递"
    return books_upload()


@app.post("/api/tasks/<int:tid>/release")
def api_release(tid):
    """Not a failure: the worker is wanted for something else (a game on the Mac). Back on the board, progress kept."""
    if (denied := compute_auth()):
        return denied
    board.release_task(tid, _worker())
    return jsonify(ok=True)


@app.get("/api/tasks/<int:tid>/audio")
def api_task_audio(tid):
    """The task's video's first sound track as it is (no re-encoding: the Pi only unpacks it, ~60 MB an hour of
    AAC); the worker decodes it. Re-encoding here made the Pi the bottleneck: 2 minutes for 27 minutes of sound."""
    if (denied := compute_auth()):
        return denied
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, _worker()), one=True)
    if not row:
        return jsonify(error="not your task"), 404
    try:
        path, _ = board.task_media({"payload": json.loads(row["payload"])})
    except ValueError as e:
        return jsonify(error=str(e)), 404
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "matroska", "-"],
                            stdout=subprocess.PIPE)

    def stream():
        try:
            while chunk := proc.stdout.read(1 << 16):
                yield chunk
        finally:
            proc.kill()
            proc.wait()
    return Response(stream(), mimetype="audio/x-matroska")


def my_task(tid):
    row = q("SELECT * FROM tasks WHERE id=? AND worker=? AND state='running'", (tid, _worker()), one=True)
    return board.task_dict(row) if row else None


@app.get("/api/tasks/<int:tid>/keyframes")
def api_task_keyframes(tid):
    """The task's video's keyframes as a tar of 224x224 JPEGs named by their time in seconds (~3 MB for 24 min)."""
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    if not task:
        return jsonify(error="not your task"), 404
    import tarfile
    path, _ = board.task_media(task)
    (INCOMPLETE / "tmp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=INCOMPLETE / "tmp") as tmp:
        frames = library.keyframes(path, Path(tmp))
        out = tempfile.NamedTemporaryFile(dir=INCOMPLETE / "tmp", suffix=".tar", delete=False)
        with tarfile.open(fileobj=out, mode="w") as tar:
            for t, f in frames:
                tar.add(f, arcname=f"{t:.2f}.jpg")
        out.close()

    def stream():
        try:
            with open(out.name, "rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk
        finally:
            os.unlink(out.name)
    return Response(stream(), mimetype="application/x-tar")


@app.get("/api/tasks/<int:tid>/note-file")
def api_task_note_file(tid):
    """One attachment of the note a note_media task is about."""
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    name = request.args.get("file", "")
    if not task or name not in [f["file"] for f in task["payload"].get("files", [])]:
        return jsonify(error="not your task / not its file"), 404
    return send_file(NOTES_DIR / name, conditional=True)


@app.get("/api/tasks/<int:tid>/cover")
def api_task_cover(tid):
    if (denied := compute_auth()):
        return denied
    task = my_task(tid)
    if not task:
        return jsonify(error="not your task"), 404
    return send_file(library.job_thumb(task))


@app.get("/api/tasks")
def api_board():
    """The board (for `task board` on the Mac and for 资源使用)."""
    if g.external and not g.user and (denied := compute_auth()):
        return denied
    return jsonify(board.board_summary())


@app.get("/api/notes")
def notes_list():
    term = request.args.get("q", "").strip()
    where, args = "owner = ?", [g.owner]
    # newest first by the note's date (which can be changed); 更早的 continues after the last one shown
    before, before_id = notes.note_time(request.args.get("before")), request.args.get("before_id", "")
    if before and before_id.isdigit():
        where, args = where + " AND (created < ? OR (created = ? AND id < ?))", args + [before, before, int(before_id)]
    if term:  # the text and what's said in voice notes and videos; forgiving (see _note_find)
        found = search.notes_search(term)
        return jsonify(notes=[notes.note_dict(r, m, seen) for r, m, seen in found[:200]], more=False)
    rows = q(f"SELECT * FROM notes WHERE {where} ORDER BY created DESC, id DESC LIMIT 51", args)
    extra = {}
    if not before:  # first page: 那年今天, and the places your notes were made
        today = time.strftime("%m-%d")
        extra["onthisday"] = [notes.note_dict(r) for r in q(
            "SELECT * FROM notes WHERE owner=? AND strftime('%m-%d', created, 'unixepoch', 'localtime')=? "
            "AND strftime('%Y', created, 'unixepoch', 'localtime') < strftime('%Y', 'now', 'localtime') "
            "ORDER BY created DESC", (g.owner, today))]
        places = {}
        for r in q("SELECT media FROM notes WHERE owner=?", (g.owner,)):
            for p in {m.get("place") for m in json.loads(r["media"]) if m.get("place")}:
                places[p] = places.get(p, 0) + 1
        extra["places"] = sorted(places.items(), key=lambda x: -x[1])[:20]
        extra["recap"] = kv_get(f"notes_recap:{g.owner}")  # last week, looked back on (Monday mornings)
    return jsonify(notes=[notes.note_dict(r, term) for r in rows[:50]], more=len(rows) > 50, **extra)


@app.post("/api/notes")
def note_add():
    text = (request.form.get("text") or "").strip()[:20000]
    files = request.files.getlist("media")
    if not text and not files:
        return jsonify(error="空的"), 400
    now = time.time()
    with db_lock:
        cur = core.DB.execute("INSERT INTO notes (owner, text, device, created, updated) VALUES (?,?,?,?,?)",
                         (g.owner, text, g.device_label, notes.note_time(request.form.get("created")) or now, now))
        core.DB.commit()
        nid = cur.lastrowid
    media = notes.save_note_files(nid, files)
    q("UPDATE notes SET media=?, pending=? WHERE id=?",
      (json.dumps(media, ensure_ascii=False), int(any(m["todo"] for m in media)), nid))
    if media:
        board.publish("note_media", f"note:{nid}", 60, force=True)  # note_ai follows it
    elif LLM_API_KEY:
        board.publish("note_ai", f"note:{nid}", 30)
    return jsonify(note=notes.note_dict(notes.note_row(nid)))


@app.post("/api/notes/<int:nid>")
def note_edit(nid):
    """Change the text and/or drop attachments (`drop`: their positions)."""
    row = notes.note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    media = json.loads(row["media"])
    drop = {int(i) for i in body.get("drop", []) if str(i).isdigit()}
    for i in drop:
        if i < len(media):
            for k in ("file", "poster"):
                if media[i].get(k):
                    (NOTES_DIR / media[i][k]).unlink(missing_ok=True)
            q("DELETE FROM vec WHERE kind='note' AND ref=? AND src=?", (nid, "照片:" + media[i]["file"]))
    media = [m for i, m in enumerate(media) if i not in drop]
    text = str(body.get("text", row["text"])).strip()[:20000]
    if not text and not media:
        return note_delete(nid)
    created = notes.note_time(body.get("created")) or row["created"]
    q("UPDATE notes SET text=?, media=?, created=?, updated=? WHERE id=?",
      (text, json.dumps(media, ensure_ascii=False), created, time.time(), nid))
    if text != row["text"] and LLM_API_KEY:
        board.publish("note_ai", f"note:{nid}", 30, force=True)
    return jsonify(note=notes.note_dict(notes.note_row(nid)))


@app.post("/api/notes/<int:nid>/media")
def note_add_media(nid):
    """More photos / videos / voice for a note that's already there."""
    row = notes.note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    added = notes.save_note_files(nid, request.files.getlist("media"))
    if not added:
        return jsonify(error="不是照片、视频或音频"), 400
    row = notes.note_row(nid)  # re-read: saving big files takes a while
    q("UPDATE notes SET media=?, pending=1, updated=? WHERE id=?",
      (json.dumps(json.loads(row["media"]) + added, ensure_ascii=False), time.time(), nid))
    board.publish("note_media", f"note:{nid}", 60, force=True)
    return jsonify(note=notes.note_dict(notes.note_row(nid)))


@app.post("/api/notes/<int:nid>/delete")
def note_delete(nid):
    row = notes.note_row(nid)
    if not row:
        return jsonify(error="not found"), 404
    for m in json.loads(row["media"]):
        for k in ("file", "poster"):
            if m.get(k):
                (NOTES_DIR / m[k]).unlink(missing_ok=True)
    q("DELETE FROM notes WHERE id=?", (nid,))
    q("DELETE FROM vec WHERE kind='note' AND ref=?", (nid,))
    return jsonify(ok=True)


@app.get("/notefile/<int:nid>/<int:n>")
@app.get("/notefile/<int:nid>/<int:n>/<what>")
def note_file(nid, n, what="file"):
    row = notes.note_row(nid)
    media = json.loads(row["media"]) if row else []
    if n >= len(media) or what not in ("file", "poster") or not media[n].get(what):
        return "", 404
    path = NOTES_DIR / media[n][what]
    if not path.exists():
        return "", 404
    return send_file(path, conditional=True, max_age=86400)  # Range requests: videos seek, iPhones play them


# ---------------------------------------------------------------- 书架: e-books

def book_row(bid):
    """The book if it's on this browser's / account's shelf (shelves are private, like 随记)."""
    row = q("SELECT * FROM books WHERE id=?", (bid,), one=True)
    return row if row and row["owner"] == g.owner else None


@app.get("/api/books")
def books_list():
    """The shelf; with q: books whose title / author / text has the words, and where in the text."""
    term = request.args.get("q", "").strip()
    hits = books.search(g.owner, term) if term else None
    reads = {r["book"]: r for r in q("SELECT * FROM book_read WHERE owner=?", (g.owner,))}
    out = []
    for r in q("SELECT * FROM books WHERE owner=? ORDER BY id DESC", (g.owner,)):
        if hits is not None and r["id"] not in hits:
            continue
        d = books.book_dict(r, reads.get(r["id"]))
        if hits:
            d["hits"] = hits[r["id"]]
        out.append(d)
    return jsonify(books=out)


@app.post("/api/books")
def books_upload():
    """Files (`file`, several at once) and/or a link (`url`) onto the shelf."""
    added, dupes, bad = [], [], []
    for f in request.files.getlist("file"):
        bid, how = books.add_file(g.owner, g.device_label, f)
        if how == "new":
            added.append(bid)
        elif how == "duplicate":
            dupes.append(bid)
        else:
            bad.append(f.filename)
    url = (request.form.get("url") or (request.get_json(silent=True) or {}).get("url") or "").strip()
    if url:
        if not URL_RE.fullmatch(url):
            bad.append(url)
        else:
            bid, how = books.add_url(g.owner, g.device_label, url)
            (added if how == "new" else dupes).append(bid)
    for bid in added:
        board.publish("book_import", f"book:{bid}", 80, force=True)
    if not added and not dupes:
        return jsonify(error="不是电子书（支持 EPUB、PDF、TXT、MOBI、AZW3）", bad=bad), 400
    return jsonify(ids=added, duplicates=dupes, bad=bad)


@app.get("/api/books/<int:bid>")
def book_info(bid):
    row = book_row(bid)
    if not row:
        return jsonify(error="not found"), 404
    read = q("SELECT * FROM book_read WHERE owner=? AND book=?", (g.owner, bid), one=True)
    d = books.book_dict(row, read)
    d["toc"] = json.loads(row["toc"] or "[]")
    d["marks"] = [{"id": m["id"], "pos": json.loads(m["pos"] or "null"), "pct": m["pct"], "text": m["text"], "created": m["created"]}
                  for m in q("SELECT * FROM book_marks WHERE owner=? AND book=? ORDER BY pct", (g.owner, bid))]
    return jsonify(book=d)


@app.get("/api/books/<int:bid>/pack")
def book_pack(bid):
    """Everything needed to read the book (chapters, contents; for a PDF its page sizes), gzipped once at import."""
    row = book_row(bid)
    path = BOOKS_DIR / f"{bid}.pack.json.gz"
    if not row or row["status"] != "ready" or not path.exists():
        return jsonify(error="not ready"), 404
    if "gzip" not in request.headers.get("Accept-Encoding", ""):
        return Response(gzip.decompress(path.read_bytes()), mimetype="application/json")
    resp = send_file(path, mimetype="application/json", conditional=False)
    resp.headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding", "Cache-Control": "no-cache"})
    return resp


@app.get("/bookres/<int:bid>")
def book_res(bid):
    """A picture inside a book (`p`: its path in the EPUB). Served so that an SVG can't run anything."""
    row = book_row(bid)
    path = request.args.get("p", "")
    zpath = books.res_zip(row) if row else None
    ctype = books.IMAGE_TYPES.get(Path(path).suffix.lower())
    if not zpath or not ctype:
        return "", 404
    try:
        with zipfile.ZipFile(zpath) as z:
            data = z.read(path)
    except KeyError:
        return "", 404
    resp = Response(data, mimetype=ctype)
    resp.headers.update({"Cache-Control": "private, max-age=2592000", "X-Content-Type-Options": "nosniff",
                         "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})
    return resp


@app.get("/bookcover/<int:bid>")
def book_cover(bid):
    row = book_row(bid)
    path = BOOKS_DIR / f"{bid}.jpg"
    if not row or not path.exists():
        return "", 404
    return send_file(path, max_age=86400)


@app.get("/bookfile/<int:bid>")
def book_file(bid):
    """The book's file: as it was sent (?download=1), or the PDF the reader shows (Range requests work)."""
    row = book_row(bid)
    if not row or not row["file"] or not (BOOKS_DIR / row["file"]).exists():
        return "", 404
    if request.args.get("download"):
        return send_file(BOOKS_DIR / row["file"], as_attachment=True,
                         download_name=safe_name(row["title"] or "book", 100) + Path(row["file"]).suffix)
    if row["view"] != "pdf":
        return "", 404
    return send_file(books.pdf_file(row), mimetype="application/pdf", conditional=True, max_age=0)


@app.post("/api/books/<int:bid>")
def book_edit(bid):
    """title, author, shelf ("want" / ""), done (true/false: 读完 / not), reset (forget where you are)."""
    row = book_row(bid)
    if not row:
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    title = str(body.get("title", row["title"])).strip()[:200] or row["title"]
    author = str(body.get("author", row["author"])).strip()[:100]
    shelf = body.get("shelf", row["shelf"]) if body.get("shelf", row["shelf"]) in ("", "want") else row["shelf"]
    q("UPDATE books SET title=?, author=?, shelf=?, updated=? WHERE id=?", (title, author, shelf, time.time(), bid))
    now = time.time()
    if body.get("reset"):
        q("DELETE FROM book_read WHERE owner=? AND book=?", (g.owner, bid))
    elif "done" in body:
        done = bool(body["done"])
        q("INSERT INTO book_read (owner, book, pct, done, started, finished, updated) VALUES (?,?,?,?,?,?,?) "
          "ON CONFLICT(owner, book) DO UPDATE SET done=excluded.done, finished=excluded.finished, updated=excluded.updated",
          (g.owner, bid, 1.0 if done else 0, int(done), now, now if done else None, now))
    return jsonify(ok=True)


@app.post("/api/books/<int:bid>/delete")
def book_delete(bid):
    row = book_row(bid)
    if not row:
        return jsonify(error="not found"), 404
    books.remove(row)
    return jsonify(ok=True)


@app.post("/api/books/<int:bid>/retry")
def book_retry(bid):
    row = book_row(bid)
    if not row:
        return jsonify(error="not found"), 404
    q("UPDATE books SET status='importing', error='', updated=? WHERE id=?", (time.time(), bid))
    board.publish("book_import", f"book:{bid}", 80, force=True)
    return jsonify(ok=True)


@app.post("/api/books/<int:bid>/replace")
def book_replace(bid):
    """A newer file of the same book (a TXT novel with new chapters): read again; where you are stays."""
    row = book_row(bid)
    f = request.files.get("file")
    if not row or not f:
        return jsonify(error="not found"), 404
    err = books.replace_file(row, f)
    if err:
        return jsonify(error=err), 400
    board.publish("book_import", f"book:{bid}", 80, force=True)
    return jsonify(ok=True)


@app.post("/api/books/<int:bid>/progress")
def book_progress(bid):
    """Where reading is (pos: {ch, f} or {page, f}; pct: of the whole book) and the seconds read since the last save.
    `at`: when it was read (saves made offline come later); an older position doesn't overwrite a newer one."""
    if not book_row(bid):
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    try:
        pct = max(0.0, min(1.0, float(body.get("pct") or 0)))
        at = min(float(body.get("at") or time.time()), time.time())
    except (TypeError, ValueError):
        return jsonify(error="bad position"), 400
    pos = json.dumps(body.get("pos"))[:500]
    prev = q("SELECT * FROM book_read WHERE owner=? AND book=?", (g.owner, bid), one=True)
    weekly.record(g.owner, "book", bid, body.get("secs"), prev["pct"] if prev else 0, pct)
    secs = max(0.0, min(float(body.get("secs") or 0), weekly.MAX_SAVE_SECONDS))
    if prev and prev["updated"] and at < prev["updated"]:  # a newer position came from another device meanwhile
        q("UPDATE book_read SET seconds=seconds+? WHERE owner=? AND book=?", (secs, g.owner, bid))
        return jsonify(ok=True, stale=True)
    done = bool(prev and prev["done"]) or pct >= 0.99
    finished = (prev["finished"] if prev and prev["finished"] else time.time()) if done else None
    q("INSERT INTO book_read (owner, book, pos, pct, done, seconds, started, finished, updated) VALUES (?,?,?,?,?,?,?,?,?) "
      "ON CONFLICT(owner, book) DO UPDATE SET pos=excluded.pos, pct=excluded.pct, done=excluded.done, "
      "seconds=seconds+excluded.seconds, started=COALESCE(started, excluded.started), finished=excluded.finished, "
      "updated=excluded.updated", (g.owner, bid, pos, pct, int(done), secs, time.time(), finished, at))
    if not prev and q("SELECT shelf FROM books WHERE id=?", (bid,), one=True)["shelf"] == "want":
        q("UPDATE books SET shelf='' WHERE id=?", (bid,))  # started reading: no longer just 想读
    return jsonify(ok=True, done=done)


@app.post("/api/books/<int:bid>/marks")
def book_mark_add(bid):
    if not book_row(bid):
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    try:
        pct = max(0.0, min(1.0, float(body.get("pct") or 0)))
    except (TypeError, ValueError):
        return jsonify(error="bad position"), 400
    q("INSERT INTO book_marks (owner, book, pos, pct, text, created) VALUES (?,?,?,?,?,?)",
      (g.owner, bid, json.dumps(body.get("pos"))[:500], pct, str(body.get("text", ""))[:300], time.time()))
    return book_info(bid)


@app.post("/api/books/<int:bid>/marks/<int:mid>/delete")
def book_mark_delete(bid, mid):
    if not book_row(bid):
        return jsonify(error="not found"), 404
    q("DELETE FROM book_marks WHERE id=? AND owner=? AND book=?", (mid, g.owner, bid))
    return book_info(bid)


@app.get("/api/books/<int:bid>/search")
def book_search(bid):
    term = request.args.get("q", "").strip()
    if not book_row(bid) or not term:
        return jsonify(hits=[])
    return jsonify(hits=books.search_in(bid, term))


@app.get("/api/weekly")
def weekly_get():
    """每周总结: this week so far, and the weeks kept on Monday mornings."""
    start, skip = weekly.week_start(), hidden_ids()
    hide = hidden_subs(skip)
    past = []
    for r in q("SELECT * FROM weekly WHERE owner=? ORDER BY start DESC LIMIT 12", (g.owner,)):
        body = json.loads(r["body"])
        if skip:  # kept before those were hidden (or with them): count the week again without them
            t0 = time.mktime(time.strptime(r["start"], "%Y-%m-%d"))
            body = {**body, **weekly.report(g.owner, t0, weekly.plus_days(t0, 7), backlog=False, skip=skip)}
        past.append({**body, "created": r["created"]})
    current = weekly.report(g.owner, start, weekly.plus_days(start, 7), skip=skip, skip_subs=hide)
    for body in past:  # (kept with every uploader in it)
        if body.get("backlog"):
            body["backlog"] = weekly.without_subs(body["backlog"], hide)
    return jsonify(current=current, past=past)


@app.get("/api/points/<int:jid>")
def key_point_times(jid):
    if not visible(jid):
        return jsonify(error="not found"), 404
    row = q("SELECT analysis, ref FROM jobs WHERE id=?", (jid,), one=True)
    a = json.loads(row["analysis"] or "{}")
    if row["ref"]:  # linked entry: the downloading job has the chapters
        a = {**json.loads(q("SELECT analysis FROM jobs WHERE id=?", (row["ref"],), one=True)["analysis"] or "{}"), **a}
    times = [{"part": 0, "t": t} if t is not None else None for t in a["point_times"]] if a.get("point_times") \
        else search.point_times(jid)
    return jsonify(times=times, chapters=a.get("chapters") or {}, markers=a.get("markers") or {}, heat=a.get("heat") or {})


@app.get("/api/similar/<int:jid>")
def similar_of(jid):
    """Same content elsewhere in the library: re-uploads, clips of it, or what it's a clip of."""
    if not visible(jid):
        return jsonify(error="not found"), 404
    out, skip = [], hidden_ids()
    for r in q("SELECT * FROM similar WHERE a=? OR b=?", (jid, jid)):
        other = r["b"] if r["a"] == jid else r["a"]
        if not visible(other) or other in skip:
            continue
        mine, theirs = (r["a_in_b"], r["b_in_a"]) if r["a"] == jid else (r["b_in_a"], r["a_in_b"])
        row = q("SELECT title, analysis FROM jobs WHERE id=?", (other,), one=True)
        rel = "same" if r["kind"] == "same" else "part_of" if mine >= theirs else "has_part"
        out.append({"job": other, "rel": rel, "mine": mine, "theirs": theirs,
                    "title": json.loads(row["analysis"] or "{}").get("title") or row["title"]})
    return jsonify(similar=out)


@app.post("/api/ask")
def ask_question():
    """问拾光: answer a question from what's said in the videos and written in the notes, with sources."""
    question = str((request.get_json(silent=True) or {}).get("q", "")).strip()[:300]
    if not question:
        return jsonify(error="问点什么"), 400
    if not LLM_API_KEY:
        return jsonify(error="没有配置 AI"), 400
    scope, scope_args = scope_sql()
    ups = sub_names()
    skip = hidden_ids()
    if skip:
        scope, scope_args = scope + f" AND id NOT IN ({','.join('?' * len(skip))})", (*scope_args, *skip)
    return jsonify(ask.answer(question, scope + " AND status != 'cancelled'", scope_args, g.owner, ups))


@app.get("/api/digests")
def digests_list():
    rows = q("SELECT * FROM digests WHERE owner=? ORDER BY end DESC, id DESC LIMIT 8", (g.owner,))
    pending = q("SELECT state FROM tasks WHERE kind='digest' AND target LIKE ? AND state IN ('queued','running')",
                (f"digest:{g.owner}:%",), one=True)
    skip, out = hidden_ids(), []
    hide = hidden_subs(skip)
    for r in rows:
        body = json.loads(r["body"])
        body["uploaders"] = [u for u in body.get("uploaders") or [] if u.get("sub") not in hide]
        for u in body.get("uploaders") or []:
            u["videos"] = [v for v in u.get("videos") or [] if v.get("job") not in skip]
        body["uploaders"] = [u for u in body.get("uploaders") or [] if u["videos"]]
        out.append({"id": r["id"], **body, "created": r["created"]})
    return jsonify(digests=out,
                   pending=bool(pending))


@app.post("/api/notes/recap")
def notes_recap_now():
    """The last 7 days of 随记, looked back on now (the automatic one comes on Monday mornings)."""
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    board.publish("notes_recap", f"notes-recap:{g.owner}:{start}:{end}", 70, force=True)
    return jsonify(ok=True)


@app.post("/api/digests/now")
def digest_now():
    """The last 7 days, now (the automatic one comes on Monday mornings)."""
    if not q("SELECT 1 FROM subs WHERE owner=?", (g.owner,), one=True):
        return jsonify(error="还没有追更的 UP 主"), 400
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 6 * 86400))
    board.publish("digest", f"digest:{g.owner}:{start}:{end}", 70, force=True)
    return jsonify(ok=True)


@app.post("/api/watch/<int:jid>")
def save_watch(jid):
    """Where playback is (sent every ~10 s, on pause and when the page is left): picked up on any device."""
    if not visible(jid):
        return jsonify(error="not found"), 404
    body = request.get_json(silent=True) or {}
    try:
        pos, dur, part = float(body.get("pos") or 0), float(body.get("dur") or 0), int(body.get("part") or 0)
    except (TypeError, ValueError):
        return jsonify(error="bad position"), 400
    done = bool(body.get("ended")) or (dur > 0 and (pos > dur - 30 or pos / dur > 0.95))
    prev = q("SELECT done, pos, dur FROM watch WHERE owner=? AND job_id=?", (g.owner, jid), one=True)
    if dur > 0:  # how long it was really played since the last save (每周总结)
        weekly.record(g.owner, "video", jid, body.get("secs"), (prev["pos"] / prev["dur"]) if prev and prev["dur"] else 0,
                      1.0 if done and body.get("ended") else min(1.0, pos / dur))
    q("INSERT INTO watch (owner, job_id, part, pos, dur, done, updated) VALUES (?,?,?,?,?,?,?) "
      "ON CONFLICT(owner, job_id) DO UPDATE SET part=excluded.part, pos=excluded.pos, dur=excluded.dur, "
      "done=excluded.done, updated=excluded.updated",
      # once seen to the end it stays 已看完, even when it's watched again
      (g.owner, jid, part, pos, dur, int(done or bool(prev and prev["done"])), time.time()))
    return jsonify(ok=True, done=done)


@app.delete("/api/watch/<int:jid>")
def clear_watch(jid):
    """Forget this account's playback position without touching the video or activity totals."""
    if not visible(jid):
        return jsonify(error="not found"), 404
    q("DELETE FROM watch WHERE owner=? AND job_id=?", (g.owner, jid))
    return jsonify(ok=True)


@app.post("/api/jobs/<int:jid>/<action>")
def job_action(jid, action):
    if not visible(jid):
        return jsonify(error="not found"), 404
    if action == "cancel":
        return jsonify(msg=pipeline.cancel_job(jid))
    if action == "retry":
        return jsonify(msg=pipeline.retry_job(jid))
    if action == "delete":
        with_files = bool((request.get_json(silent=True) or {}).get("files"))
        result, files = pipeline.remove_job(jid, with_files)
        if files:
            library.plex_refresh()
        return jsonify(msg="removed from list" if result != "stopping" else "still stopping; remove it again in a moment",
                       result=result, files=files)
    return jsonify(error="unknown action"), 400


@app.post("/api/tags/merge")
def tags_merge():
    if not g.admin:
        return jsonify(error="admin only"), 403
    return jsonify(merged=llm.merge_tags())


@app.post("/api/jobs/remove")
def remove_many():
    """Batch removal: {"ids": [...], "files": true/false}"""
    body = request.get_json(silent=True) or {}
    with_files = bool(body.get("files"))
    out = {"removed": 0, "stopping": 0, "files": 0}
    for jid in body.get("ids", [])[:500]:
        if isinstance(jid, int) and visible(jid):
            result, files = pipeline.remove_job(jid, with_files)
            out[result] = out.get(result, 0) + 1
            out["files"] += files
    if out["files"]:
        library.plex_refresh()
    return jsonify(out)


@app.get("/subs/<int:jid>/<int:n>/bi")
def subs_bilingual(jid, n):
    """Chinese above English in one track: the Chinese cues, each with the English said meanwhile under it."""
    m = library.job_media(jid, n)
    pair = library.bilingual_pair(m) if m else None
    if not pair:
        return "", 404
    zh, en = library.srt_cues(pair[0]), library.srt_cues(pair[1])

    def ts(t):
        h, rem = divmod(t, 3600)
        mm, sec = divmod(rem, 60)
        return f"{int(h):02}:{int(mm):02}:{sec:06.3f}"
    out, j = ["WEBVTT", ""], 0
    same = len(zh) == len(en) and all(abs(x[0] - y[0]) < 0.01 for x, y in zip(zh, en))  # translated line by line
    for i, (a, b, t) in enumerate(zh):
        if same:
            said = en[i][2]
        else:  # different files: the English said mostly within this cue
            while j < len(en) and en[j][1] <= a:
                j += 1
            said = " ".join(x for s, e, x in en[j:j + 4] if min(b, e) - max(a, s) > (e - s) / 2)
        out += [f"{ts(a)} --> {ts(b)}", t] + ([said] if said else []) + [""]
    return Response("\n".join(out), mimetype="text/vtt")


@app.get("/play/<int:jid>/<int:n>")
def play(jid, n):
    m = library.job_media(jid, n)
    if not m:
        return "", 404
    path = Path(m["path"])
    if path.suffix.lower() in library.BROWSER_DIRECT:
        return send_file(path, conditional=True)  # supports Range requests, so seeking works
    # Browsers can't open MKV/AVI/TS (or APE/WMA): repackage once as MP4 into a cache and serve that with
    # Range support (iPhones refuse video without it). Video is copied; only odd audio gets re-encoded.
    cached = library.remux_for_browser(path)
    if cached:
        return send_file(cached, conditional=True)
    return "", 415


@app.get("/castplay/<int:jid>/<int:n>/<path:name>")
def cast_play(jid, n, name):
    """A DLNA-friendly alias of /play: some TVs reject a media URL without a file extension."""
    out = play(jid, n)
    if isinstance(out, Response):
        out.headers["transferMode.dlna.org"] = "Streaming"
        out.headers["contentFeatures.dlna.org"] = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
    return out


@app.get("/api/cast/devices")
def cast_devices():
    """DLNA discovery runs on the Pi; a web page has no access to SSDP multicast."""
    return jsonify(devices=cast.discover())


@app.post("/api/cast/start")
def cast_start():
    data = request.get_json(silent=True) or {}
    try:
        jid, n = int(data.get("id")), int(data.get("part", 0))
    except (TypeError, ValueError):
        return jsonify(error="视频不对"), 400
    media = library.job_media(jid, n)
    row = q("SELECT title FROM jobs WHERE id=?", (jid,), one=True)
    if not media or not row:
        return jsonify(error="这个视频不能投屏"), 404
    base = CAST_URL or request.host_url.rstrip("/")
    if not re.match(r"^https?://", base):
        return jsonify(error="CAST_URL 要写成 http://地址:端口"), 500
    token = library.media_token(jid, n)
    media_url = f"{base}/castplay/{jid}/{n}/{urllib.parse.quote(Path(media['path']).stem[:120])}.mp4?t={token}"
    thumb_url = f"{base}/thumb/{jid}?t={library.media_token(jid, 0)}"
    mime = "audio/mpeg" if Path(media["path"]).suffix.lower() in AUDIO_EXT else "video/mp4"
    try:
        name = cast.start(str(data.get("device", "")), media_url, row["title"] or Path(media["path"]).stem, thumb_url, mime,
                          data.get("position", 0))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 502
    return jsonify(ok=True, name=name)


@app.post("/api/cast/control")
def cast_control():
    data = request.get_json(silent=True) or {}
    try:
        cast.control(str(data.get("device", "")), str(data.get("action", "")), data.get("position"), data.get("volume"))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 502
    return jsonify(ok=True)


@app.get("/api/cast/status")
def cast_status():
    try:
        return jsonify(cast.status(str(request.args.get("device", ""))))
    except (ValueError, RuntimeError) as e:
        return jsonify(error=str(e)), 502


@app.get("/subs/<int:jid>/<int:n>/<int:k>")
def subs(jid, n, k):
    m = library.job_media(jid, n)
    if not m or not 0 <= k < len(m["subs"]):
        return "", 404
    text = Path(m["subs"][k]).read_text(errors="ignore")
    if not text.startswith("WEBVTT"):  # SRT -> WebVTT: header + dot as the millisecond separator
        text = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", text.replace("\r", ""))
    return Response(text, mimetype="text/vtt")


@app.get("/frame/<int:jid>/<int:n>")
def frame(jid, n):
    """The frame `t` seconds into a video (a search found something there), cached."""
    m = library.job_media(jid, n)
    try:
        t = max(0.0, float(request.args.get("t", "0")))
    except ValueError:
        return "", 400
    if not m:
        return "", 404
    out = MEDIA / ".cache" / "frames" / f"{jid}-{n}-{int(t)}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", m["path"], "-frames:v", "1", "-vf", "scale=640:-2",
                        str(out)], capture_output=True, timeout=60)
    return send_file(out, max_age=86400) if out.exists() else ("", 404)


@app.get("/thumb/<int:jid>")
def thumb(jid):
    row = q("SELECT thumb, files FROM jobs WHERE id=?", (jid,), one=True)
    if not (library.token_ok(jid, 0) or visible(jid)) or not row:
        return "", 404
    thumb = row["thumb"]
    if not thumb or not Path(thumb).exists():
        # the file was renamed or moved: use the cover that sits with the media now, and remember it
        thumb = next((f for f in json.loads(row["files"] or "[]") if f.endswith(".jpg") and Path(f).exists()), None)
        if not thumb:
            return "", 404
        q("UPDATE jobs SET thumb=? WHERE id=?", (thumb, jid))
    return send_file(thumb, max_age=3600)


def setup_app():
    app.secret_key = secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      MAX_CONTENT_LENGTH=10 << 20)  # uploads are only .torrent files
    app.permanent_session_lifetime = 365 * 86400
    # big uploads (随记 videos) are spooled to temp files: keep those on the disks, not the SD card / RAM
    tmp = INCOMPLETE / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)


# The other modules, imported last: they import this one too, and are only used at run time
from . import ask, board, books, cast, channels, core, library, llm, notes, pipeline, search, tasks, weekly  # noqa: E402
