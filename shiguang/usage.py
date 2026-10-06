"""资源使用: traffic, disks and CPU, what was processed, LLM tokens and costs."""
import json
import os
import shutil
import subprocess
import time
import traceback

from flask import jsonify
from flask import request
from pathlib import Path
from .migrations import once
from .core import (MEDIA, app, kv_get, kv_set, mem_available_mb, q)


# ---------------------------------------------------------------- 资源使用: what the box has been doing

XRAY = shutil.which("xray") or "/usr/local/bin/xray"


def record_traffic():
    """Add the traffic since the last look to today's totals. Counters restart from zero when Xray or the Pi
    restarts; a counter lower than last time is taken as such a restart."""
    now = {}
    try:
        out = subprocess.run([XRAY, "api", "statsquery", "--server=127.0.0.1:10085"], capture_output=True, text=True,
                             timeout=20).stdout
        for st in json.loads(out or "{}").get("stat", []):
            kind, name, _, way = (st["name"].split(">>>") + ["", "", "", ""])[:4]
            if kind == "outbound" and name not in ("block", "dns-out", "api"):
                route = "direct" if name == "direct" else "proxy"
                now[f"{st['name']}"] = (f"{route}_{'up' if way == 'uplink' else 'down'}", int(st.get("value") or 0))
    except Exception:
        pass
    try:
        for line in open("/proc/net/dev"):
            if line.strip().startswith(("wlan0:", "eth0:")):
                dev, rest = line.split(":", 1)
                f = rest.split()
                now[dev.strip() + ":rx"] = ("pi_down", int(f[0]))
                now[dev.strip() + ":tx"] = ("pi_up", int(f[8]))
    except OSError:
        pass
    last = kv_get("traffic_last", {})
    day = time.strftime("%Y-%m-%d")
    for counter, (bucket, value) in now.items():
        before = last.get(counter)
        delta = value if before is None or value < before else value - before
        if before is not None and delta:
            q("INSERT INTO traffic (day, name, bytes) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET "
              "bytes = bytes + excluded.bytes", (day, bucket, delta))
        last[counter] = value
    kv_set("traffic_last", last)


def read_health():
    """The Pi now: CPU temperature, fan, load, memory, SD card, and the disks' SMART readings (written by the
    root timer disk-health, which reads them every 15 minutes without waking sleeping disks)."""
    def first(path, conv=str):
        try:
            return conv(open(path).read().strip())
        except Exception:
            return None
    try:
        disks = json.load(open("/run/disk-health.json"))["disks"]
    except Exception:
        disks = []
    sd = shutil.disk_usage("/")
    for d in disks:  # used as df counts it (blocks in use), not size minus what's still free to write
        try:
            u = shutil.disk_usage(d["mount"])
            d["used"], d["total"] = u.used, u.total
        except Exception:
            pass
    return {"cpu_temp": (first("/sys/class/thermal/thermal_zone0/temp", int) or 0) / 1000 or None,
            "fan": first("/run/fan-level"), "load": os.getloadavg(), "cores": os.cpu_count(),
            "mem_available": mem_available_mb(), "mem_total": next((int(l.split()[1]) // 1024 for l in open("/proc/meminfo")
                                                                    if l.startswith("MemTotal")), None),
            "uptime": first("/proc/uptime", lambda x: float(x.split()[0])), "sd_free": sd.free, "sd_total": sd.total, "sd_used": sd.used,
            "disks": disks}


def record_health():
    h = read_health()
    q("INSERT INTO health (ts, cpu, disks) VALUES (?,?,?)",
      (time.time(), h["cpu_temp"], json.dumps({d["serial"]: d["temp"] for d in h["disks"] if not d.get("asleep")})))
    q("DELETE FROM health WHERE ts < ?", (time.time() - 30 * 86400,))


def health_summary():
    """Now, the day's highs, and what's worth a warning (thresholds: WD Red/white-label drives are rated to 65 °C;
    above ~55 °C wear goes up; any reallocated/pending/uncorrectable sector means the disk has started failing)."""
    h = read_health()
    day = q("SELECT cpu, disks FROM health WHERE ts > ?", (time.time() - 86400,))
    h["cpu_max"] = max([r["cpu"] or 0 for r in day] + [h["cpu_temp"] or 0]) or None
    highs = {}
    for r in day:
        for serial, t in json.loads(r["disks"] or "{}").items():
            if t is not None:
                highs[serial] = max(highs.get(serial, 0), t)
    for d in h["disks"]:
        if d.get("temp") is not None and not d.get("asleep"):
            highs[d["serial"]] = max(highs.get(d["serial"], 0), d["temp"])
    warnings = []
    if (h["cpu_temp"] or 0) >= 75:
        warnings.append(f"CPU {h['cpu_temp']:.0f}°C，偏热")
    for d in h["disks"]:
        d["temp_max"] = highs.get(d["serial"])
        name = f"{d['mount'] or d['dev']}（{round((d.get('size') or 0) / 1e12)}TB）"
        if d.get("passed") is False:
            warnings.append(f"{name} SMART 自检不通过，尽快换盘")
        bad = (d.get("reallocated") or 0) + (d.get("pending") or 0) + (d.get("uncorrectable") or 0)
        if bad:
            warnings.append(f"{name} 有 {bad} 个坏扇区（重映射/待处理/不可修复），开始老化了")
        hot = max(d.get("temp") or 0, d.get("temp_max") or 0)
        if hot >= 55:
            warnings.append(f"{name} 最高到过 {hot}°C，{'过热' if hot >= 60 else '偏热'}，注意散热")
    h["warnings"] = warnings
    return h


def record_space():
    """Once an hour: how much each top-level folder of the library takes (du, a few seconds; the page reads the
    last result instead of walking 20+ TB on every open)."""
    if time.time() - (kv_get("space", {}).get("ts") or 0) < 3600:
        return
    dirs = {}
    for p in sorted(MEDIA.iterdir()):
        if p.name == "lost+found" or not p.is_dir():
            continue
        out = subprocess.run(["nice", "-n", "19", "ionice", "-c3", "du", "-sb", "--", str(p)], capture_output=True,
                             text=True, timeout=600).stdout  # unreadable subfolders only make it a bit low
        if out.split():
            dirs[p.name] = int(out.split()[0])
    kv_set("space", {"ts": time.time(), "dirs": dirs})


def traffic_loop():
    while True:
        try:
            record_traffic()
        except Exception:
            traceback.print_exc()
        try:
            record_space()
        except Exception:
            traceback.print_exc()
        try:
            record_health()
        except Exception:
            traceback.print_exc()
        try:
            tasks.publish_digests()
        except Exception:
            traceback.print_exc()
        try:
            weekly.make_reports()
        except Exception:
            traceback.print_exc()
        try:
            llm.record_balance()
        except Exception:
            traceback.print_exc()
        time.sleep(300)


@once("usage_backfilled")
def backfill_usage():
    """Once: what was done before usage was recorded, from the jobs themselves."""
    for r in q("SELECT id, kind, files, analysis, updated FROM jobs WHERE status='done' AND ref IS NULL"):
        size = sum(Path(f).stat().st_size for f in json.loads(r["files"] or "[]") if Path(f).exists())
        a = json.loads(r["analysis"] or "{}")
        u = a.get("usage") or {}
        q("INSERT INTO usage (ts, kind, purpose, job_id, amount) VALUES (?,?,?,?,?)", (r["updated"], "download", r["kind"], r["id"], size))
        if u.get("calls"):
            q("INSERT INTO usage (ts, kind, purpose, job_id, amount, tokens_in) VALUES (?,?,?,?,?,?)",
              (r["updated"], "llm", "earlier", r["id"], u["calls"], u.get("tokens") or 0))


USAGE_NAMES = {("llm", "classify"): "AI 分类（看标题和简介）", ("llm", "summarize"): "AI 总结（看字幕）",
               ("llm", "earlier"): "AI 分类+总结（统计开始前，未细分）", ("llm", "translate"): "AI 翻译字幕",
               ("llm", "digest"): "AI 追更周报", ("llm", "chapters"): "AI 章节", ("llm", "search"): "AI 理解搜索", ("llm", "ask"): "AI 问拾光", ("llm", "notes"): "AI 整理随记",
               ("llm", "failure"): "AI 解释下载失败", ("llm", "tags"): "AI 合并同义标签",
               ("whisper", "job"): "语音转文字 · 新下载（抽样 6 分钟）", ("whisper", "note"): "语音转文字 · 随记",
               ("whisper", "idle"): "语音转文字 · 闲时生成字幕（Pi）",
               ("whisper", "mac"): "语音转文字 · 完整字幕（Mac）", ("encode", "plex"): "转码（Plex / 手机能播）",
               ("ocr", "picture"): "识别图中文字", ("clip", "cover"): "识别封面", ("clip", "frames"): "识别视频画面"}


# the same requests answered by Claude on the Mac (the subscription: no price per call)
USAGE_NAMES.update({("claude", p): n.replace("AI ", "Claude · ", 1) for (k, p), n in list(USAGE_NAMES.items()) if k == "llm"})
# and by Codex on the Mac when Claude can't (the ChatGPT subscription: no price per call either)
USAGE_NAMES.update({("codex", p): n.replace("AI ", "Codex · ", 1) for (k, p), n in list(USAGE_NAMES.items()) if k == "llm"})


@app.get("/api/usage")
def usage_summary():
    days = max(1, int(request.args.get("days", "30")) if request.args.get("days", "").isdigit() else 30)
    # whole calendar days, today included ("今天" = since midnight, "7 天" = today and the 6 before), so the daily
    # traffic rows and the timestamped ones cover the same stretch
    t = time.localtime()
    since = time.mktime((t.tm_year, t.tm_mon, t.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))
    day0 = time.strftime("%Y-%m-%d", time.localtime(since))
    traffic = {}
    for r in q("SELECT day, name, bytes FROM traffic WHERE day >= ? ORDER BY day", (day0,)):
        traffic.setdefault(r["day"], {})[r["name"]] = r["bytes"]
    total = {r["name"]: r["b"] for r in q("SELECT name, SUM(bytes) b FROM traffic WHERE day >= ? GROUP BY name", (day0,))}
    jobs = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM jobs WHERE status IN ('queued','downloading','processing') "
                                          "OR (status='failed' AND updated >= ?) GROUP BY status", (since,))}
    work = []
    for r in q("SELECT kind, purpose, COUNT(*) n, SUM(amount) amount, SUM(tokens_in) tin, SUM(tokens_out) tout, "
               "SUM(seconds) secs, SUM(cost) cost, SUM(MAX(cache_hit, 0)) hit, SUM(cache_hit < 0) guessed, COUNT(cost) priced "
               "FROM usage WHERE ts >= ? GROUP BY kind, purpose ORDER BY kind, purpose", (since,)):
        work.append({"kind": r["kind"], "purpose": r["purpose"], "name": USAGE_NAMES.get((r["kind"], r["purpose"]),
                     f"{r['kind']} {r['purpose']}"), "count": r["n"], "amount": r["amount"] or 0,
                     "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0, "seconds": r["secs"] or 0,
                     "cost": r["cost"] if r["priced"] else None, "cache_hit": r["hit"] or 0, "guessed": r["guessed"] or 0})
    bal = q("SELECT ts, currency, total FROM balance ORDER BY ts", ())
    charged = sum(max(0, a["total"] - b["total"]) for a, b in zip(bal, bal[1:]) if b["ts"] >= since)  # rises are top-ups
    finished = q("SELECT COUNT(*) n FROM usage WHERE kind='download' AND ts >= ?", (since,), one=True)["n"]
    return jsonify(days=days, traffic=traffic, traffic_total=total, jobs=jobs, finished=finished, work=work,
                   balance={"total": bal[-1]["total"], "currency": bal[-1]["currency"], "since": bal[0]["ts"],
                            "charged": round(charged, 4)} if bal else None,
                   board=board.board_summary(since), health=health_summary(),
                   index={"lines": q("SELECT COUNT(*) n FROM seg", one=True)["n"],
                          "pictures": q("SELECT COUNT(*) n FROM vec", one=True)["n"]},
                   disk=dict(zip(("total", "used", "free"), shutil.disk_usage(MEDIA))), space=kv_get("space"))


# The other modules, imported last: they import this one too, and are only used at run time
from . import board, llm, tasks, weekly  # noqa: E402
