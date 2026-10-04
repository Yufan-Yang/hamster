"""The LLM (DeepSeek): classification, summaries, tags, pricing."""
import fcntl
import json
import os
import re
import requests
import threading
import time
import traceback

from pathlib import Path
from .core import (AUDIO_EXT, LLM_API_KEY, LLM_BASE_URL, LLM_EFFORT, LLM_MODEL, STATE, VIDEO_EXT, VIDEO_FOLDERS, finishing_touch, kv_get, kv_set, log_usage, q, update)


# ---------------------------------------------------------------- analysis with an LLM (DeepSeek, OpenAI-compatible API)

LIBRARIES = ["Movies", "TV", "Music", "Videos", "Downloads"]
SUMMARY_LANG = os.environ.get("SUMMARY_LANG", "Simplified Chinese")

# Step 1: a cheap call on the name + metadata only. It files the item and decides whether the
# content is worth transcribing and summarising (talks, tutorials...) or is already described
# well enough by what it is (a film, an episode, a music video...).
CLASSIFY_FIELDS = {
    "title": "the media's own full title, in its original language, used as the file name: keep it as published and only "
             "remove site names, upload IDs and emoji noise; never shorten it to a code or rewrite/summarize it",
    "creator": "who made or performs it: channel/uploader, director, artist or main performer; null if unknown",
    "library": " | ".join(LIBRARIES),
    "folder": " | ".join(VIDEO_FOLDERS),
    "year": "integer or null",
    "show": "string or null",
    "season": "integer or null",
    "episode": "integer or null",
    "artist": "string or null",
    "album": "string or null",
    "language": "language of the media",
    "tags": "3-8 short strings describing the content (topic, genre, people, place); always give some",
    "brief": "1-2 sentences on what this is, from the metadata and what you know about it",
    "needs_transcript": "true only if what is said matters and the metadata doesn't already tell it "
                        "(talks, tutorials, news, interviews, vlogs, documentaries); "
                        "false for movies, TV episodes, music, music videos, short clips, gaming/sports footage",
}
CLASSIFY_SYSTEM = """You file downloaded media into a Plex library.
Libraries:
- Movies: feature films (needs title + year).
- TV: episodes of a TV series (needs show, season, episode).
- Music: audio-only music (artist, album).
- Videos: everything else that is a video (online videos, clips, talks, music videos...). Pick the best folder.
- Downloads: not media at all (software, documents, archives).
`folder` is only used for Videos; set it to "Other" otherwise.
Write `brief` and `tags` in {lang}.
When a tag means the same as one already in the library (listed in the request), use that exact spelling.
Describe adult content plainly and factually like any other content; don't leave fields empty because of it.
Reply with one JSON object with exactly these keys:
{fields}"""

# Step 2, only when step 1 says so: summarise the transcript.
SUMMARY_FIELDS = {
    "summary": "2-4 sentences: what it is about and what is said",
    "key_points": "3-6 short strings",
}
SUMMARY_SYSTEM = """You summarise a video for its owner from its transcript.
Write in {lang}, even when the video is in another language.
Reply with one JSON object with exactly these keys:
{fields}"""


# DeepSeek's list prices, CNY per 1M tokens at peak: (input from cache, input not from cache, output incl. reasoning).
# Off-peak is half; peak is 01:00-04:00 and 06:00-10:00 UTC on weekdays (Chinese public holidays are off-peak all
# day; not known here, so those days are priced as peak). https://api-docs.deepseek.com/quick_start/pricing, 2026-10.
# LLM_PRICE="hit,miss,output" overrides. The DeepSeek balance (record_balance) shows what was really charged.
LLM_PRICES = {"deepseek-flash": (0.04, 2.0, 8.0), "deepseek-v4-pro": (0.30, 9.0, 27.0)}


def llm_cost(model, hit, miss, out, when):
    try:
        price = tuple(float(x) for x in os.environ["LLM_PRICE"].split(","))
    except (KeyError, ValueError):
        price = LLM_PRICES.get(model) or LLM_PRICES.get(LLM_MODEL)
    if not price:
        return None
    t = time.gmtime(when)
    if not (t.tm_wday < 5 and (1 <= t.tm_hour < 4 or 6 <= t.tm_hour < 10)):
        price = tuple(p / 2 for p in price)
    return round((hit * price[0] + miss * price[1] + out * price[2]) / 1e6, 6)


def record_balance():
    """DeepSeek account balance (free to ask), so 资源使用 can show what was really charged, not only list prices."""
    if not LLM_API_KEY or "deepseek" not in LLM_BASE_URL:
        return
    r = requests.get(f"{LLM_BASE_URL}/user/balance", headers={"Authorization": f"Bearer {LLM_API_KEY}"}, timeout=20)
    info = next((b for b in r.json().get("balance_infos", []) if float(b.get("total_balance") or 0) > 0),
                (r.json().get("balance_infos") or [None])[0])
    if info:
        last = q("SELECT total FROM balance ORDER BY ts DESC LIMIT 1", one=True)
        total = float(info["total_balance"])
        if not last or abs(last["total"] - total) > 1e-9 or time.time() - kv_get("balance_logged", 0) > 6 * 3600:
            q("INSERT INTO balance (ts, currency, total) VALUES (?,?,?)", (time.time(), info["currency"], total))
            kv_set("balance_logged", time.time())


def llm_json(system, user, fields, max_tokens, usage, purpose="", job_id=None, think=True):
    """max_tokens includes the model's reasoning tokens; only tokens actually used are billed.
    think=False: no reasoning at all, for mechanical work (translating lines, matching tags) where it only costs:
    thinking took ~5,000 of the ~6,000 output tokens of a 60-line subtitle batch."""
    thinking = ({"reasoning_effort": LLM_EFFORT} if LLM_EFFORT else {}) if think else \
        ({"thinking": {"type": "disabled"}} if "deepseek" in LLM_BASE_URL else {})
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=180,
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                      json={"model": LLM_MODEL, "max_tokens": max_tokens, **thinking,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": system.format(
                                    lang=SUMMARY_LANG, fields=json.dumps(fields, ensure_ascii=False, indent=1))},
                                {"role": "user", "content": user}]})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    body = r.json()
    u = body.get("usage") or {}
    usage["calls"] = usage.get("calls", 0) + 1
    usage["tokens"] = usage.get("tokens", 0) + (u.get("total_tokens") or 0)
    hit = u.get("prompt_cache_hit_tokens") or 0
    miss = u.get("prompt_cache_miss_tokens", (u.get("prompt_tokens") or 0) - hit)
    cost = llm_cost(body.get("model") or LLM_MODEL, hit, miss, u.get("completion_tokens") or 0, time.time())
    if cost is not None:
        usage["cost"] = round(usage.get("cost", 0) + cost, 6)
    log_usage("llm", purpose, job_id, amount=1, tokens_in=u.get("prompt_tokens") or 0,
              tokens_out=u.get("completion_tokens") or 0, cost=cost, cache_hit=hit)
    content = body["choices"][0]["message"].get("content") or ""
    if not content.strip():
        # Happens when reasoning uses up max_tokens or the JSON mode returns nothing
        raise RuntimeError(f"empty reply (finish_reason={body['choices'][0].get('finish_reason')})")
    out = json.loads(content)
    return {k: out.get(k) for k in fields}  # the API only promises valid JSON, not this shape


def as_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def heuristic_analysis(name, meta, guess):
    """Used when there is no API key or the API call fails."""
    a = {"title": meta.get("title") or name, "summary": (meta.get("description") or "").strip()[:400],
         "key_points": [], "tags": meta.get("tags") or [], "language": "", "library": "Videos", "folder": "Other",
         "year": None, "show": None, "season": None, "episode": None, "artist": None, "album": None,
         "needs_transcript": False}
    creator = meta.get("uploader") or meta.get("channel") or meta.get("artist")
    if creator:
        a["creator"] = str(creator)
        a["tags"] = [str(creator)] + [t for t in a["tags"] if t != creator]
    if guess:
        if guess.get("type") == "episode" and guess.get("episode"):
            a.update(library="TV", show=str(guess.get("title")), season=guess.get("season") or 1,
                     episode=guess["episode"] if isinstance(guess["episode"], int) else guess["episode"][0])
        elif guess.get("type") == "movie" and meta.get("source") in ("torrent", "file"):
            a.update(library="Movies", title=str(guess.get("title")), year=guess.get("year"))
    if meta.get("source") == "ytdlp":
        a["folder"] = "Music Videos" if (meta.get("track") or "Music" in (meta.get("categories") or [])) else "Other"
    return a


def classify(job_id, name, meta, guess):
    if not LLM_API_KEY:
        return heuristic_analysis(name, meta, guess)
    update(job_id, stage="classifying")
    meta = {k: v for k, v in meta.items() if v}
    if len(meta.get("description") or "") > 1500:  # the start of a description is enough to classify
        meta["description"] = meta["description"][:1500] + " …[cut]"
    parts = [f"Name: {name}", "Metadata:\n" + json.dumps(meta, ensure_ascii=False, indent=1)]
    known = library_tags()[:200]
    if known:
        parts.append("Tags already in the library: " + "、".join(known))
    if guess:
        parts.append("Filename parser guess:\n" + json.dumps({k: str(v) for k, v in guess.items()}, ensure_ascii=False))
    usage = {}
    for attempt in range(2):
        try:
            a = llm_json(CLASSIFY_SYSTEM, "\n\n".join(parts), CLASSIFY_FIELDS, 4000, usage, "classify", job_id)
            break
        except Exception as e:
            if attempt:
                a = heuristic_analysis(name, meta, guess)
                a["note"] = f"AI classification failed: {e}"
                return a
    a["title"] = str(a["title"] or meta.get("title") or name)
    a["tags"] = [str(x).strip() for x in a["tags"] if str(x).strip()] if isinstance(a["tags"], list) else []
    if not a["tags"]:  # the model left them out: fall back to the site's own tags
        a["tags"] = [str(t) for t in (meta.get("tags") or [])][:6]
    a["creator"] = str(a.get("creator") or meta.get("uploader") or meta.get("channel") or meta.get("artist") or "").strip() or None
    if a["creator"] and a["creator"] not in a["tags"]:
        a["tags"].insert(0, a["creator"])  # the creator is always a tag, so you can browse/search by it
    a["library"] = a["library"] if a["library"] in LIBRARIES else "Videos"
    a["folder"] = a["folder"] if a["folder"] in VIDEO_FOLDERS else "Other"
    for k in ("year", "season", "episode"):
        a[k] = as_int(a[k])
    a["needs_transcript"] = a["needs_transcript"] is True or str(a["needs_transcript"]).lower() == "true"
    a["summary"], a["key_points"] = str(a.pop("brief") or ""), []
    a["usage"] = usage
    return a


def library_tags():
    """Every tag in use, most used first."""
    counts = {}
    for r in q("SELECT analysis FROM jobs WHERE status='done'"):
        for t in json.loads(r["analysis"] or "{}").get("tags") or []:
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])


MERGE_SYSTEM = """You tidy the tags of a media library. Find tags that mean the same thing: synonyms, near-synonyms,
translations of each other, different spellings, case or plural variants (e.g. "TED演讲" / "TED Talk" / "TED Talks",
"音乐视频" / "MV" / "官方MV"). Do not merge tags that are merely related (e.g. "心理学" and "拖延症" stay separate).
For each group pick one canonical tag, preferring the {lang} form and the one already used most.
Reply with one JSON object: {{"merge": {{"<tag to replace>": "<canonical tag>", ...}}}} listing only tags that change."""

tag_lock = threading.Lock()


def merge_tags():
    """Fold synonym tags together across the whole library (the 合并标签 button; ~20k tokens, so not automatic)."""
    if not LLM_API_KEY:
        return {}
    with tag_lock:
        tags = library_tags()
        if len(tags) < 2:
            return {}
        out = llm_json(MERGE_SYSTEM, "Tags (most used first):\n" + "\n".join(tags), {"merge": "object"}, 32000, {}, "tags")  # room for the model's reasoning over the whole tag list
        mapping = {k: v for k, v in (out.get("merge") or {}).items()
                   if isinstance(k, str) and isinstance(v, str) and k != v and k in tags and v.strip()}
        # follow chains (a -> b -> c) so everything lands on the final canonical tag
        for k in list(mapping):
            seen = {k}
            while mapping[k] in mapping and mapping[k] not in seen:
                seen.add(mapping[k])
                mapping[k] = mapping[mapping[k]]
        if not mapping:
            kv_set("tags_merged", tags)
            return {}
        apply_tag_mapping(mapping)
        kv_set("tags_merged", library_tags())
    return mapping


def apply_tag_mapping(mapping):
    """Rename tags across the library (and in Plex); a search for an old name finds the new one."""
    if not mapping:
        return
    changed = []
    for r in q("SELECT id, analysis, files FROM jobs WHERE status='done'"):
        a = json.loads(r["analysis"] or "{}")
        old = a.get("tags") or []
        new = list(dict.fromkeys(mapping.get(t, t) for t in old))
        if new != old:
            a["tags"] = new
            update(r["id"], analysis=a)
            vids = [f for f in json.loads(r["files"] or "[]") if Path(f).suffix.lower() in VIDEO_EXT | AUDIO_EXT]
            changed += [(vids[0], a)] if vids else []
    aliases = kv_get("tag_aliases", {})
    aliases.update(mapping)
    kv_set("tag_aliases", aliases)
    if changed:
        finishing_touch(library.plex_set_metadata, changed)


MATCH_SYSTEM = """You keep a media library's tags tidy. For each NEW tag, say which EXISTING tag means exactly the same
thing (a synonym, translation, abbreviation, other spelling, case or plural variant, e.g. "LLM" = "大语言模型",
"TED Talks" = "TED演讲", "川普" = "特朗普"), or null if none does. Merely related tags are not the same ("伊朗" is not
"中东", "深度学习" is not "机器学习"); names of different people are never the same.
Reply with one JSON object: {{"same": {{"<new tag>": "<existing tag or null>", ...}}}}"""


def tag_key(t):
    """What's left of a tag once case, width, traditional characters, spaces, punctuation and a plural -s are gone."""
    import unicodedata
    try:
        from opencc import OpenCC
        t = OpenCC("t2s").convert(t)
    except Exception:
        pass
    k = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", t).casefold())
    return k[:-1] if re.fullmatch(r"[a-z]{4,}s", k) else k


def merge_tags_if_new():
    """After a download: fold its new tags into ones already in the library that mean the same.
    Only the new tags are looked at: same spelling once normalised is merged right here; the rest goes to the
    LLM as one narrow question ("which existing tag is this one?"), a few hundred tokens instead of
    re-reading the whole list pair by pair (which cost ~20k reasoning tokens per download)."""
    try:
        (STATE / "locks").mkdir(parents=True, exist_ok=True)
        with open(STATE / "locks" / "tags.lock", "w") as lock:  # one at a time across job processes
            fcntl.flock(lock, fcntl.LOCK_EX)
            tags = library_tags()
            known = [t for t in tags if t in set(kv_get("tags_merged", []))]
            new = [t for t in tags if t not in set(known)]
            if not new:
                return
            if not known:  # first run: nothing to compare with yet
                kv_set("tags_merged", tags)
                return
            by_key = {}
            for t in known:  # most used first, so the common spelling wins
                by_key.setdefault(tag_key(t), t)
            mapping = {t: by_key[tag_key(t)] for t in new if tag_key(t) in by_key and by_key[tag_key(t)] != t}
            ask = [t for t in new if t not in mapping]
            if ask and LLM_API_KEY:
                out = llm_json(MATCH_SYSTEM, "EXISTING tags (most used first):\n" + "\n".join(known) +
                               "\n\nNEW tags:\n" + "\n".join(ask), {"same": "object"}, 4000, {}, "tags", think=False)
                for t, same in (out.get("same") or {}).items():
                    if t in ask and isinstance(same, str) and same in known and same != t:
                        mapping[t] = same
            apply_tag_mapping(mapping)
            kv_set("tags_merged", list(dict.fromkeys(known + [t for t in new if t not in mapping])))
    except Exception:
        traceback.print_exc()


def summarize(job_id, a, transcript, transcript_note):
    update(job_id, stage="summarizing")
    if len(transcript) > 60_000:  # stay well inside DeepSeek's context window
        transcript_note = (transcript_note + " ").lstrip() + "[transcript cut at 60k characters]"
        transcript = transcript[:60_000]
    user = (f"Title: {a['title']}\nWhat it is: {a['summary']}\n\n"
            f"Transcript {transcript_note}:\n{transcript}")
    try:
        out = llm_json(SUMMARY_SYSTEM, user, SUMMARY_FIELDS, 8000, a.setdefault("usage", {}), "summarize", job_id)
        a["summary"] = str(out["summary"] or a["summary"])
        points = out["key_points"]
        if isinstance(points, str):  # now and then a single string instead of a list
            points = [x.strip(" -•·") for x in re.split(r"[\n；;]+", points)]
        a["key_points"] = [str(x) for x in points if str(x).strip()] if isinstance(points, list) else []
    except Exception as e:
        a["note"] = f"AI summary failed: {e}"
    return a


def offpeak_from(when=None):
    """The next moment DeepSeek charges half (see llm_cost); `when` itself if it already does."""
    t = when or time.time()
    while (lambda g: g.tm_wday < 5 and (1 <= g.tm_hour < 4 or 6 <= g.tm_hour < 10))(time.gmtime(t)):
        t += 900
    return t


# The other modules, imported last: they import this one too, and are only used at run time
from . import library  # noqa: E402
