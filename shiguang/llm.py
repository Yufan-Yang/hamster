"""The LLM: Claude through the Mac mini first, DeepSeek as the backup; classification, summaries, tags, pricing."""
import fcntl
import functools
import json
import os
import re
import requests
import secrets
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
    "tags": "3-6 short tags for what is actually discussed: topics, people, places, events",
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


# ---- Claude through the Mac mini
#
# The Mac's Claude Code is logged in with the Claude subscription, so asking it costs nothing per call (it uses the
# subscription's limits). A request becomes an `llm` task on the board; a claim loop on the Mac (mac_worker.py) runs
# `claude -p` on it and hands back the JSON. When the Mac isn't there (off, asleep, its limits used up: it then says
# it's paused) or doesn't take the request within CLAUDE_CLAIM_WAIT seconds, DeepSeek answers as before.
CLAUDE_VIA_MAC = os.environ.get("CLAUDE_VIA_MAC", "1") != "0"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_MODEL_LIGHT = os.environ.get("CLAUDE_MODEL_LIGHT", "haiku")  # short mechanical answers
CLAUDE_LIGHT = {"classify", "tags", "failure", "notes"}
CLAUDE_SKIP = {"search", "ask"}  # someone is looking at the screen: a few seconds matter, so DeepSeek first
CLAUDE_CLAIM_WAIT = int(os.environ.get("CLAUDE_CLAIM_WAIT", "90"))
CLAUDE_RUN_WAIT = 900  # once the Mac has it


def claude_ready():
    """A Claude claim loop on the Mac asked for work lately and isn't paused (limits used up)."""
    if not CLAUDE_VIA_MAC:
        return False
    return any("claude" in json.loads(r["caps"]) and not r["paused"]
               for r in q("SELECT caps, paused FROM workers WHERE seen > ?", (time.time() - 300,)))


def claude_json(system, user, fields, purpose, job_id, usage, think):
    """The request answered by Claude on the Mac, or None (it wasn't taken in time, or failed): DeepSeek then."""
    from . import board
    now = time.time()
    q("DELETE FROM tasks WHERE kind='llm' AND state != 'running' AND created < ?", (now - 3600,))  # askers that died
    payload = {"system": system, "user": user, "fields": list(fields), "purpose": purpose, "title": purpose,
               "model": CLAUDE_MODEL_LIGHT if purpose in CLAUDE_LIGHT else CLAUDE_MODEL, "effort": "medium" if think else "low"}
    tid = board.publish("llm", f"llm:{purpose}:{secrets.token_hex(6)}", 60 if purpose == "classify" else 50, payload=payload)
    try:
        checked = now
        while True:
            row = q("SELECT state, result, error FROM tasks WHERE id=?", (tid,), one=True)
            if not row:
                return None
            if row["state"] == "done":
                res = json.loads(row["result"] or "{}")
                out = res.get("out")
                if not isinstance(out, dict):
                    return None
                usage["calls"] = usage.get("calls", 0) + 1
                usage["tokens"] = usage.get("tokens", 0) + int(res.get("tokens_in") or 0) + int(res.get("tokens_out") or 0)
                usage["claude"] = usage.get("claude", 0) + 1
                log_usage("claude", purpose, job_id, amount=1, tokens_in=int(res.get("tokens_in") or 0),
                          tokens_out=int(res.get("tokens_out") or 0), seconds=float(res.get("seconds") or 0),
                          cache_hit=int(res.get("cache_read") or 0))
                return {k: out.get(k) for k in fields}
            if row["state"] == "failed":
                print(f"claude {purpose}: {row['error'][:200]} -> DeepSeek", flush=True)
                return None
            late = time.time() - now
            if row["state"] == "queued" and (late > CLAUDE_CLAIM_WAIT or (time.time() - checked > 15 and not claude_ready())):
                if board._write("UPDATE tasks SET state='failed', error='not taken in time' WHERE id=? AND state='queued'", (tid,)):
                    return None
                continue  # taken just now
            if late > CLAUDE_CLAIM_WAIT + CLAUDE_RUN_WAIT:
                board._write("UPDATE tasks SET state='failed', error='took too long' WHERE id=?", (tid,))
                return None
            if time.time() - checked > 15:
                checked = time.time()
            time.sleep(2)
    finally:
        q("DELETE FROM tasks WHERE id=?", (tid,))


def llm_json(system, user, fields, max_tokens, usage, purpose="", job_id=None, think=True):
    """One request with a JSON answer: Claude on the Mac when it's there (see claude_json), else DeepSeek.
    max_tokens includes the model's reasoning tokens; only tokens actually used are billed.
    think=False: no reasoning at all, for mechanical work (translating lines, matching tags) where it only costs:
    thinking took ~5,000 of the ~6,000 output tokens of a 60-line subtitle batch."""
    system = system.format(lang=SUMMARY_LANG, fields=json.dumps(fields, ensure_ascii=False, indent=1))
    if purpose not in CLAUDE_SKIP and claude_ready():
        try:
            out = claude_json(system, user, fields, purpose, job_id, usage, think)
            if out is not None:
                return out
        except Exception:
            traceback.print_exc()
    try:
        return deepseek_json(system, user, fields, max_tokens, usage, purpose, job_id, think)
    except Exception:
        # the quick kinds (search, 问拾光) try DeepSeek first; when it can't answer (no balance left...), Claude does
        if purpose in CLAUDE_SKIP and claude_ready():
            out = claude_json(system, user, fields, purpose, job_id, usage, False)
            if out is not None:
                return out
        raise


def deepseek_json(system, user, fields, max_tokens, usage, purpose, job_id, think):
    if not LLM_API_KEY:
        raise RuntimeError("Claude on the Mac didn't answer and there's no DeepSeek key")
    thinking = ({"reasoning_effort": LLM_EFFORT} if LLM_EFFORT else {}) if think else \
        ({"thinking": {"type": "disabled"}} if "deepseek" in LLM_BASE_URL else {})
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=180,
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                      json={"model": LLM_MODEL, "max_tokens": max_tokens, **thinking,
                            "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
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
    # The tag list first: it's the same from one video to the next, and DeepSeek charges 1/50 for input it has seen
    # (a request's opening that matches an earlier one); what differs per video comes after it
    known = library_tags()[:200]
    parts = ["Tags already in the library: " + "、".join(known)] if known else []
    parts += [f"Name: {name}", "Metadata:\n" + json.dumps(meta, ensure_ascii=False, indent=1)]
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


# Summary and chapters of a video both read all of its subtitles. They send them in exactly the same words and the
# same place (this system prompt, then the subtitles, then the task), so the second call finds that opening in
# DeepSeek's cache and pays 1/50 for it.
TRANSCRIPT_SYSTEM = """You work on one video for its owner, who reads Simplified Chinese. The user message gives the
video's subtitles first, in blocks that start with their time [h:mm:ss], then the task. Reply with one JSON object,
as the task says."""
TRANSCRIPT_LIMIT = 80_000  # characters: well inside DeepSeek's context window


def timed_transcript(jid, part=0):
    """(text, lines, last line's time) of a video's subtitles, as ~30-second blocks: what summary and chapters send."""
    rows = q("SELECT t, text FROM seg WHERE kind='job' AND ref=? AND part=? AND src='字幕' AND t IS NOT NULL ORDER BY t",
             (jid, part))
    blocks, cur, start = [], [], None

    def stamp(t):
        return f"[{int(t // 3600)}:{int(t % 3600 // 60):02}:{int(t % 60):02}]"
    for r in rows:
        if start is None:
            start = r["t"]
        cur.append(r["text"])
        if r["t"] - start >= 30:
            blocks.append(f"{stamp(start)} {' '.join(cur)}")
            cur, start = [], None
    if cur:
        blocks.append(f"{stamp(start)} {' '.join(cur)}")
    return "SUBTITLES:\n" + "\n".join(blocks)[:TRANSCRIPT_LIMIT], len(rows), (rows[-1]["t"] if rows else 0)


SUMMARY_TASK = """TASK: summarise this video for its owner from the subtitles above.
Its title: {title}
What it was taken to be before the subtitles were read: {brief}
Write in {lang}, even when the video is in another language. Reply with one JSON object with exactly these keys:
{fields}"""


def summarize_timed(job_id, a, transcript):
    """The summary from timed_transcript() text: same result as summarize(), cache-friendly for chapters after it."""
    update(job_id, stage="summarizing")
    task = SUMMARY_TASK.format(title=a.get("title") or "", brief=a.get("summary") or "", lang=SUMMARY_LANG,
                               fields=json.dumps(SUMMARY_FIELDS, ensure_ascii=False, indent=1))
    try:
        out = llm_json(TRANSCRIPT_SYSTEM, f"{transcript}\n\n{task}", SUMMARY_FIELDS, 8000, a.setdefault("usage", {}),
                       "summarize", job_id)
        apply_summary(a, out)
    except Exception as e:
        a["note"] = f"AI summary failed: {e}"
    return a


def summarize(job_id, a, transcript, transcript_note):
    update(job_id, stage="summarizing")
    if len(transcript) > 60_000:  # stay well inside DeepSeek's context window
        transcript_note = (transcript_note + " ").lstrip() + "[transcript cut at 60k characters]"
        transcript = transcript[:60_000]
    user = (f"Title: {a['title']}\nWhat it is: {a['summary']}\n\n"
            f"Transcript {transcript_note}:\n{transcript}")
    try:
        out = llm_json(SUMMARY_SYSTEM, user, SUMMARY_FIELDS, 8000, a.setdefault("usage", {}), "summarize", job_id)
        apply_summary(a, out)
    except Exception as e:
        a["note"] = f"AI summary failed: {e}"
    return a


def apply_summary(a, out):
    """A summary reply into the analysis: summary, key points, and tags from what's actually said."""
    a["summary"] = str(out["summary"] or a["summary"])
    points = out["key_points"]
    if isinstance(points, str):  # now and then a single string instead of a list
        points = [x.strip(" -•·") for x in re.split(r"[\n；;]+", points)]
    # without the "1. " some replies number them with (the page lists them already)
    a["key_points"] = [re.sub(r"^\s*\d+\s*[.、)）]\s*", "", str(x)) for x in points if str(x).strip()] \
        if isinstance(points, list) else []
    # tags from what's said (the first ones came from the title and description only); the old ones stay
    # first: the creator and the people in it
    new = [str(t).strip() for t in out.get("tags") or [] if str(t).strip()] if isinstance(out.get("tags"), list) else []
    a["tags"] = list(dict.fromkeys((a.get("tags") or []) + new))[:12]


def offpeak_from(when=None):
    """The next moment DeepSeek charges half (see llm_cost); `when` itself if it already does, or when Claude on the
    Mac is there to do the work (no price to wait for)."""
    t = when or time.time()
    if claude_ready():
        return t
    while (lambda g: g.tm_wday < 5 and (1 <= g.tm_hour < 4 or 6 <= g.tm_hour < 10))(time.gmtime(t)):
        t += 900
    return t


# The other modules, imported last: they import this one too, and are only used at run time
from . import library  # noqa: E402


PARSE_SYSTEM = """You turn what someone typed into the search box of their own video library into filters.
Today is {today}. Uploaders they follow: {uploaders}.
Reply with one JSON object: {{"keywords": [...], "visual": ..., "uploader": ..., "since": ..., "until": ...}}
- keywords: 1-4 short words or names (in Chinese unless the query is English) that would be in a title, summary or
  what's said: the topics, people, places. Not filler ("片段", "视频", "讲").
- visual: only when the query says what should be seen on screen (画面里, 镜头, 出现...): a short noun phrase, else null.
- uploader: only when the query names one of the uploaders above: that name exactly, else null.
- since / until: "YYYY-MM-DD" when the query gives a time (上个月, 去年, 9月, 最近一周), resolved against today; else null."""


@functools.lru_cache(maxsize=256)
def parse_query(text, uploaders, today):
    """A natural-language search, as filters. ~300 tokens without thinking (≈¥0.001); cached per query and day."""
    out = llm_json(PARSE_SYSTEM.replace("{today}", today).replace("{uploaders}", uploaders or "（无）"), text,
                   {"keywords": "array", "visual": "string or null", "uploader": "string or null",
                    "since": "string or null", "until": "string or null"}, 400, {}, "search", think=False)
    day = re.compile(r"\d{4}-\d\d-\d\d")
    return {"keywords": [str(k).strip() for k in out.get("keywords") or [] if str(k).strip()][:4],
            "visual": str(out["visual"]).strip() if out.get("visual") else None,
            "uploader": str(out["uploader"]).strip() if out.get("uploader") else None,
            "since": out["since"] if isinstance(out.get("since"), str) and day.fullmatch(out["since"]) else None,
            "until": out["until"] if isinstance(out.get("until"), str) and day.fullmatch(out["until"]) else None}
