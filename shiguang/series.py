"""Series names and pagination: a complete series occupies one place in the video list."""
import re
import unicodedata


def clean_name(name):
    name = unicodedata.normalize("NFKC", str(name or "")).strip()
    return re.sub(r"\s*(?:第[一二三四五六七八九十百\d]+[季部期]|season\s*\d+|s\d+)\s*$",
                  "", name, flags=re.I).strip()


def numbered_name(title):
    text = unicodedata.normalize("NFKC", str(title or "")).strip()
    match = re.match(r"^(.{2,}?)\s*(?:第[一二三四五六七八九十百零〇\d]+[集话話期]|[Ss]\d+\s*[Ee]\d+|"
                     r"([Rr][Ee])\s*\d+|[Ee][Pp]?\s*\d+)(?:\b|\s|[:·.\-]|$|[^\x00-\x7F])",
                     text, flags=re.ASCII)
    if match:
        return clean_name(match[1] + ("re" if match[2] else ""))
    match = re.match(r"^(.+?[^\d\s])\s*\d{1,4}\s*(?::|$)", text, flags=re.ASCII)
    return clean_name(match[1]) if match else ""


def series_name(job, subscriptions):
    analysis = job.get("analysis") or {}
    if analysis.get("library") == "TV" and analysis.get("show"):
        return clean_name(analysis["show"])
    source = job.get("source") or ""
    sub = subscriptions.get(source)
    if sub and sub["platform"] == "bangumi":
        return clean_name(sub["name"]) or source
    if analysis.get("library") and analysis["library"] not in ("TV", "Videos"):
        return ""
    return numbered_name(analysis.get("title") or job.get("title")) or numbered_name(job.get("title"))


def series_key(name):
    return re.sub(r"[\s·:：\-_]", "", name.lower())


def page_ids(candidates, limit, subscriptions):
    """Candidates are visible videos in the requested sort order; include every member of selected series."""
    groups = {}
    for job in candidates:
        name = series_name(job, subscriptions)
        key = ("series", series_key(name)) if name else ("job", job["id"])
        groups.setdefault(key, []).append(job["id"])
    selected = list(groups.values())[:limit]
    return [jid for group in selected for jid in group], len(groups) > limit
