"""The Telegram bot (off unless TELEGRAM_BOT_TOKEN is set)."""
import os
import re
import requests
import shutil
import time
import traceback

from pathlib import Path
from .core import (MEDIA, STATE, TG_TOKEN, find_urls, job_dict, kv_get, kv_set, q, safe_name, unique_path)


# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    files = params.pop("files", None)
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=params if files else None,
                      json=None if files else params, files=files, timeout=70)
    return r.json()


def job_text(j):
    a = j["analysis"]
    icon = {"queued": "⏳", "downloading": "⬇️", "processing": "⚙️", "done": "✅", "failed": "❌", "cancelled": "🚫",
            "linked": "🔗"}.get(j["status"], "•")
    lines = [f"{icon} #{j['id']} {a.get('title') or j['title'] or j['url'][:80]}"]
    if j["status"] in ("downloading", "processing"):
        lines.append(f"{j['stage']} {j['progress']:.0f}% {j['speed']}".strip())
    if j["status"] == "done":
        if a.get("summary"):
            lines.append(a["summary"])
        if a.get("key_points"):
            lines += ["• " + p for p in a["key_points"][:6]]
        if a.get("tags"):
            lines.append(" ".join("#" + re.sub(r"\W+", "_", t).strip("_") for t in a["tags"][:8]))
        if j["files"]:
            lines.append("📁 " + j["files"][0].replace(str(MEDIA), "media"))
        if a.get("note"):
            lines.append("ℹ️ " + a["note"])
    if j["status"] == "failed":
        lines.append(j["error"][:500])
    return "\n".join(lines)


def notify(job_id):
    if not TG_TOKEN:
        return
    try:
        j = job_dict(q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True))
        chat = j["chat_id"] or kv_get("tg_owner")
        if not chat:
            return
        text = job_text(j)
        if j["msg_id"]:
            tg("deleteMessage", chat_id=chat, message_id=j["msg_id"])
        if j["status"] == "done" and j["thumb"] and Path(j["thumb"]).exists() and len(text) <= 1024:
            with open(j["thumb"], "rb") as f:
                tg("sendPhoto", chat_id=chat, caption=text, files={"photo": f})
        else:
            tg("sendMessage", chat_id=chat, text=text[:4096], disable_web_page_preview=True)
    except Exception:
        traceback.print_exc()


def tg_progress_loop():
    """Keep the 'working on it' messages in Telegram up to date."""
    last = {}
    while True:
        time.sleep(15)
        for row in q("SELECT * FROM jobs WHERE status IN ('queued','downloading','processing') AND msg_id IS NOT NULL"):
            j = job_dict(row)
            text = job_text(j)
            if last.get(j["id"]) != text:
                last[j["id"]] = text
                try:
                    tg("editMessageText", chat_id=j["chat_id"], message_id=j["msg_id"], text=text,
                       disable_web_page_preview=True)
                except Exception:
                    pass


def tg_loop():
    offset = kv_get("tg_offset", 0)
    allowed = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED", "").replace(",", " ").split() if x}
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        except Exception:
            time.sleep(10)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            kv_set("tg_offset", offset)
            msg = upd.get("message") or {}
            user = (msg.get("from") or {}).get("id")
            chat = (msg.get("chat") or {}).get("id")
            owner = kv_get("tg_owner")
            if owner is None and not allowed:
                kv_set("tg_owner", chat)  # first person to message the bot owns it
                owner = chat
            if user not in allowed and chat != owner:
                tg("sendMessage", chat_id=chat, text="Sorry, this is a private bot.")
                continue
            try:
                tg_handle(msg, chat)
            except Exception as e:
                traceback.print_exc()
                tg("sendMessage", chat_id=chat, text=f"Error: {e}")


def tg_handle(msg, chat):
    text = msg.get("text") or msg.get("caption") or ""
    doc = msg.get("document")
    if text.startswith("/start") or text.startswith("/help"):
        tg("sendMessage", chat_id=chat, text="Send me links (video pages, files, magnets) or .torrent files. "
           "I'll download them, analyze them and put them in Plex.\n/jobs – recent jobs\n/cancel <id>\n/retry <id>")
        return
    if text.startswith("/jobs"):
        rows = q("SELECT * FROM jobs ORDER BY id DESC LIMIT 10")
        out = "\n".join(job_text(job_dict(r)).split("\n")[0] for r in rows) or "No jobs yet."
        tg("sendMessage", chat_id=chat, text=out)
        return
    m = re.match(r"/(cancel|retry)\s+#?(\d+)", text)
    if m:
        action, jid = m.group(1), int(m.group(2))
        tg("sendMessage", chat_id=chat, text=(pipeline.cancel_job if action == "cancel" else pipeline.retry_job)(jid))
        return
    urls = find_urls(text)
    if doc and (doc.get("file_name", "").endswith(".torrent") or doc.get("mime_type") == "application/x-bittorrent"):
        path = save_tg_file(doc["file_id"], doc["file_name"])
        urls.append("torrent-file:" + str(path))
    elif doc or msg.get("video"):
        f = doc or msg["video"]
        if f.get("file_size", 0) > 20 << 20:
            tg("sendMessage", chat_id=chat, text="Telegram only lets bots fetch files up to 20 MB – send a link instead.")
        else:
            path = save_tg_file(f["file_id"], f.get("file_name") or f"telegram-{f['file_unique_id']}.mp4")
            dest = unique_path(MEDIA / "Downloads" / path.name)
            shutil.move(str(path), str(dest))
            tg("sendMessage", chat_id=chat, text=f"Saved to media/Downloads/{dest.name}")
    for u in urls:
        sent = tg("sendMessage", chat_id=chat, text=f"⏳ queued: {u[:200]}", disable_web_page_preview=True)
        pipeline.add_job(u, source="telegram", chat_id=chat, msg_id=sent.get("result", {}).get("message_id"))
    if not urls and not doc and not msg.get("video") and text:
        tg("sendMessage", chat_id=chat, text="I didn't find a link in that.")


def save_tg_file(file_id, name):
    info = tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=120)
    r.raise_for_status()
    path = STATE / "uploads" / f"{int(time.time())}-{safe_name(name)}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(r.content)
    return path


# The other modules, imported last: they import this one too, and are only used at run time
from . import pipeline  # noqa: E402
