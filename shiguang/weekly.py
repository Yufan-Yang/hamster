"""每周总结: one account's week in numbers — books read, videos watched and added, 随记 written — and what's still
waiting: 追更 videos not downloaded or not watched yet, books being read and how much of each is left. Counting only
(no AI, nothing to pay). Last week's is kept as it was on Monday morning; this week's is worked out when asked."""
import json
import time

from .core import q


DAY = 86400
MAX_SAVE_SECONDS = 300  # time sent with one progress save at most (the page sends every few seconds to a minute)


def day_str(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def week_start(t=None):
    """Monday 00:00 (local time) of the week `t` is in."""
    lt = time.localtime(t or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 0, 0, 0, 0, 0, -1))


def plus_days(t, n):
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + n, 0, 0, 0, 0, 0, -1))


def record(owner, kind, ref, seconds, pct_before, pct_now):
    """Time spent today on a book / video (sent by the page with each progress save) and how far it got."""
    try:
        seconds = max(0.0, min(float(seconds or 0), MAX_SAVE_SECONDS))
    except (TypeError, ValueError):
        seconds = 0.0
    q("INSERT INTO activity (owner, day, kind, ref, seconds, pct0, pct1) VALUES (?,?,?,?,?,?,?) "
      "ON CONFLICT(owner, day, kind, ref) DO UPDATE SET seconds=seconds+excluded.seconds, pct1=excluded.pct1",
      (owner, time.strftime("%Y-%m-%d"), kind, ref, seconds, pct_before, pct_now))


def reading_speed(owner):
    """(characters per second, pages per second) from the last 30 days of reading, with sensible defaults."""
    since = day_str(time.time() - 30 * DAY)
    chars = secs = pages = psecs = 0.0
    for r in q("SELECT a.seconds, a.pct0, a.pct1, b.chars, b.chapters, b.view FROM activity a JOIN books b ON b.id = a.ref "
               "WHERE a.owner=? AND a.kind='book' AND a.day >= ? AND a.seconds > 60", (owner, since)):
        gained = max(0.0, (r["pct1"] or 0) - (r["pct0"] or 0))
        if gained * (r["chars"] or 0) > r["seconds"] * 30 and r["view"] != "pdf":
            continue  # a jump through the book that day, not reading
        if r["view"] == "pdf":
            pages, psecs = pages + gained * r["chapters"], psecs + r["seconds"]
        else:
            chars, secs = chars + gained * r["chars"], secs + r["seconds"]
    cps = chars / secs if secs > 600 else 0
    pps = pages / psecs if psecs > 600 else 0
    # Chinese reading is ~300-600 characters a minute; outside 2-30 a second something else was going on
    return (cps if 2 <= cps <= 30 else 6.0), (pps if 1 / 600 <= pps <= 1 / 10 else 1 / 90)


def report(owner, start, end, backlog=True, skip=frozenset(), skip_subs=frozenset()):
    """The numbers for [start, end) (timestamps). `backlog`: also what's still waiting now (only meaningful for the
    week just over or this one). `skip`: videos left out entirely — in no list, count or bar (privacy mode);
    `skip_subs`: followed uploaders left out of the backlog the same way."""
    d0, d1 = day_str(start), day_str(end - 1)
    acts = [a for a in q("SELECT * FROM activity WHERE owner=? AND day >= ? AND day <= ? ORDER BY day", (owner, d0, d1))
            if not (a["kind"] == "video" and a["ref"] in skip)]
    days = []
    t = start
    while t < end and len(days) < 7:
        days.append({"day": day_str(t), "read": 0, "watch": 0, "notes": 0})
        t = plus_days(t, 1)
    by_day = {d["day"]: d for d in days}

    # books
    per_book = {}
    for a in acts:
        if a["kind"] != "book":
            continue
        b = per_book.setdefault(a["ref"], {"seconds": 0.0, "from": a["pct0"], "to": a["pct1"]})
        b["seconds"] += a["seconds"] or 0
        b["to"] = a["pct1"]
        if a["day"] in by_day:
            by_day[a["day"]]["read"] += a["seconds"] or 0
    finished = {r["book"]: r["finished"] for r in q("SELECT book, finished FROM book_read WHERE owner=? AND finished >= ? AND finished < ?",
                                                     (owner, start, end))}
    rows = {r["id"]: r for r in q(f"SELECT * FROM books WHERE owner=? AND id IN ({','.join('?' * len(set(per_book) | set(finished)))})",
                                  (owner, *(set(per_book) | set(finished))))} if per_book or finished else {}
    books, chars_read, pages_read = [], 0, 0
    for bid in dict.fromkeys([*per_book, *finished]):
        r = rows.get(bid)
        if not r:
            continue  # deleted since
        b = per_book.get(bid, {"seconds": 0, "from": None, "to": None})
        gained = max(0.0, (b["to"] or 0) - (b["from"] or 0))
        # how far the position moved, but no faster than anyone reads (jumping ahead in the contents isn't reading)
        if r["view"] == "pdf":
            pages_read += round(min(gained * r["chapters"], b["seconds"] / 20))
        else:
            chars_read += round(min(gained * r["chars"], b["seconds"] * 15))
        books.append({"id": bid, "title": r["title"], "author": r["author"], "seconds": round(b["seconds"]),
                      "from": b["from"], "to": b["to"], "done": bid in finished, "view": r["view"]})
    books.sort(key=lambda b: -b["seconds"])

    # videos: time watched (recorded since 每周总结 exists) and what was opened; added to the library
    per_video = {}
    for a in acts:
        if a["kind"] != "video":
            continue
        v = per_video.setdefault(a["ref"], {"seconds": 0.0, "to": a["pct1"]})
        v["seconds"] += a["seconds"] or 0
        v["to"] = a["pct1"]
        if a["day"] in by_day:
            by_day[a["day"]]["watch"] += a["seconds"] or 0
    seen = {r["job_id"]: r for r in q("SELECT * FROM watch WHERE owner=? AND updated >= ? AND updated < ?", (owner, start, end))
            if r["job_id"] not in skip}
    watched_ids = set(per_video) | set(seen)
    done_ids = {j for j, r in seen.items() if r["done"]} | {j for j, v in per_video.items() if (v["to"] or 0) >= 0.95}
    new_jobs = [r for r in q("SELECT id, source FROM jobs WHERE owner=? AND created >= ? AND created < ? "
                             "AND status IN ('done', 'linked')", (owner, start, end)) if r["id"] not in skip]
    added = {"n": len(new_jobs), "subs": sum((r["source"] or "").startswith("sub:") for r in new_jobs)}
    top = sorted(per_video.items(), key=lambda x: -x[1]["seconds"])[:5]
    titles = {r["id"]: json.loads(r["analysis"] or "{}").get("title") or r["title"]
              for r in q(f"SELECT id, title, analysis FROM jobs WHERE id IN ({','.join('?' * len(top))})", [j for j, _ in top])} if top else {}

    # 随记
    notes = {"count": 0, "photos": 0, "videos": 0, "voice": 0, "chars": 0}
    for r in q("SELECT text, media, created FROM notes WHERE owner=? AND created >= ? AND created < ?", (owner, start, end)):
        notes["count"] += 1
        notes["chars"] += len(r["text"] or "")
        for m in json.loads(r["media"] or "[]"):
            notes[{"image": "photos", "video": "videos", "audio": "voice"}.get(m.get("kind"), "photos")] += 1
        d = by_day.get(day_str(r["created"]))
        if d:
            d["notes"] += 1

    out = {"start": d0, "end": d1, "days": days,
           "books": {"list": books, "seconds": round(sum(b["seconds"] for b in books)), "count": len(books),
                     "finished": len(finished), "chars": chars_read, "pages": pages_read},
           "videos": {"watched": len(watched_ids), "finished": len(done_ids & watched_ids),
                      "seconds": round(sum(v["seconds"] for v in per_video.values())),
                      "added": added["n"] or 0, "added_subs": added["subs"] or 0,
                      "top": [{"id": j, "title": titles.get(j, ""), "seconds": round(v["seconds"])} for j, v in top if titles.get(j)]},
           "notes": notes}
    if backlog:
        out["backlog"] = without_subs(backlog_now(owner), skip_subs)
    return out


def without_subs(backlog, hide):
    """The backlog without these followed uploaders (and their numbers out of the totals)."""
    if not hide:
        return backlog
    subs = [s for s in backlog.get("subs") or [] if s["id"] not in hide]
    return {**backlog, "subs": subs, "sub_waiting": sum(s["waiting"] for s in subs),
            "sub_unwatched": sum(s["unwatched"] for s in subs), "sub_failed": sum(s["failed"] for s in subs)}


def backlog_now(owner):
    """What's still waiting: 追更 videos not downloaded yet / not watched to the end, books being read (and how long the
    rest takes at this person's speed), books marked 想读 and ones never opened."""
    subs = []
    for s in q("SELECT id, name FROM subs WHERE owner=? ORDER BY id", (owner,)):
        c = q("SELECT SUM(j.status IN ('queued','downloading','processing')) waiting, SUM(j.status='failed') failed, "
              "SUM(j.status IN ('done','linked') AND COALESCE(w.done, 0) = 0) unwatched, "
              "SUM(j.status IN ('done','linked') AND COALESCE(w.done, 0) = 0 AND COALESCE(w.pos, 0) > 15) started "
              "FROM jobs j LEFT JOIN watch w ON w.owner = ? AND w.job_id = j.id WHERE j.source = ?",
              (owner, f"sub:{s['id']}"), one=True)
        subs.append({"id": s["id"], "name": s["name"], "waiting": c["waiting"] or 0, "failed": c["failed"] or 0,
                     "unwatched": c["unwatched"] or 0, "started": c["started"] or 0})
    cps, pps = reading_speed(owner)
    reading = []
    for r in q("SELECT b.*, r.pct, r.updated at FROM book_read r JOIN books b ON b.id = r.book "
               "WHERE r.owner=? AND r.done=0 AND r.pct > 0 AND b.owner=? ORDER BY r.updated DESC", (owner, owner)):
        left = 1 - (r["pct"] or 0)
        if r["view"] == "pdf":
            amount, secs = round(left * r["chapters"]), left * r["chapters"] / pps
        else:
            amount, secs = round(left * r["chars"]), left * r["chars"] / cps
        reading.append({"id": r["id"], "title": r["title"], "pct": r["pct"], "view": r["view"], "left": amount,
                        "seconds_left": round(secs), "at": r["at"]})
    shelf = q("SELECT SUM(shelf='want' AND r.book IS NULL) want, SUM(r.book IS NULL) unopened, COUNT(*) total, "
              "SUM(COALESCE(r.done, 0)) done FROM books b LEFT JOIN book_read r ON r.owner = b.owner AND r.book = b.id "
              "WHERE b.owner=? AND b.status='ready'", (owner,), one=True)
    return {"subs": subs, "sub_waiting": sum(s["waiting"] for s in subs), "sub_unwatched": sum(s["unwatched"] for s in subs),
            "sub_failed": sum(s["failed"] for s in subs), "reading": reading, "want": shelf["want"] or 0,
            "unopened": shelf["unopened"] or 0, "books_total": shelf["total"] or 0, "books_done": shelf["done"] or 0,
            "speed": {"chars_per_min": round(cps * 60), "minutes_per_page": round(1 / pps / 60, 1)}}


def is_empty(r):
    b = r.get("backlog") or {}
    return not (r["books"]["count"] or r["videos"]["watched"] or r["videos"]["added"] or r["notes"]["count"]
                or b.get("subs") or b.get("reading") or b.get("books_total"))


def make_reports():
    """From Monday 8:00 on: last week's 每周总结 for every account that did or has something (once each)."""
    this_week = week_start()
    if time.time() < this_week + 8 * 3600:
        return
    start = plus_days(this_week, -7)
    d0, d1 = day_str(start), day_str(this_week - 1)
    owners = set()
    for sql, args in (("SELECT DISTINCT owner FROM activity WHERE day >= ? AND day <= ?", (d0, d1)),
                      ("SELECT DISTINCT owner FROM notes WHERE created >= ? AND created < ?", (start, this_week)),
                      ("SELECT DISTINCT owner FROM watch WHERE updated >= ? AND updated < ?", (start, this_week)),
                      ("SELECT DISTINCT owner FROM subs", ()), ("SELECT DISTINCT owner FROM books", ())):
        owners |= {r["owner"] for r in q(sql, args) if r["owner"]}
    done = {r["owner"] for r in q("SELECT owner FROM weekly WHERE start=?", (d0,))}
    for owner in owners - done:
        body = report(owner, start, this_week)
        if not is_empty(body):
            q("INSERT OR IGNORE INTO weekly (owner, start, end, body, created) VALUES (?,?,?,?,?)",
              (owner, d0, d1, json.dumps(body, ensure_ascii=False), time.time()))
