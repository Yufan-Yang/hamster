"""问拾光: a question answered from the library: what was said in the videos (subtitles, with the moments) and the
随记, cited so each claim opens at the moment it comes from."""
import json
import time

from .core import q

ASK_SYSTEM = """You answer the owner's question about their own video library and notes, in Simplified Chinese,
using ONLY the numbered excerpts given (subtitles of videos with their times, video summaries, notes).
Cite the excerpts you rely on right after the claim, as 【n】 (several: 【2】【5】). Say who said what when it matters
(the uploader, a guest). If the excerpts don't answer the question, say so plainly and say what they do cover.
Be concise: a short paragraph or a few bullet points.
Reply with one JSON object: {{"answer": "...", "used": [n, ...]}}"""


def window(ref, part, t, before=20, after=50):
    """What's said around moment t of a video: the subtitle lines from a little before to a minute after."""
    rows = q("SELECT t, text FROM seg WHERE kind='job' AND ref=? AND part=? AND src IN ('字幕','译文') AND t BETWEEN ? AND ? "
             "ORDER BY t", (ref, part, t - before, t + after))
    return " ".join(r["text"] for r in rows)


def answer(question, scope, scope_args, owner, uploaders):
    parsed = llm.parse_query(question, uploaders, time.strftime("%Y-%m-%d"))
    vids = search.smart_search(parsed, scope, scope_args, owner)[:6] if (parsed["keywords"] or parsed["visual"]) else []
    excerpts, cites, size = [], {}, 0
    for d in vids:
        a = d.get("analysis") or {}
        title = a.get("title") or d.get("title")
        who = a.get("creator") or ""
        n = len(excerpts) + 1
        excerpts.append(f"[{n}] 视频《{title}》{('（' + who + '）') if who else ''}{a.get('published', '')} 简介：{a.get('summary', '')}")
        cites[n] = {"kind": "job", "job": d["id"], "part": 0, "t": None, "title": title}
        source = d.get("ref") or d["id"]
        used_t = []
        for h in [h for h in d.get("hits", []) if h.get("src") in ("字幕", "译文") and h.get("t") is not None][:6]:
            if any(abs(h["t"] - u) < 60 for u in used_t):
                continue  # the same stretch
            used_t.append(h["t"])
            text = window(source, h["part"], h["t"])
            if not text or size > 16000:
                continue
            n = len(excerpts) + 1
            mm = f"{int(h['t'] // 60)}:{int(h['t'] % 60):02}"
            excerpts.append(f"[{n}] 视频《{title}》{mm} 起说的：{text}")
            cites[n] = {"kind": "job", "job": d["id"], "part": h["part"], "t": max(0, h["t"] - 3), "title": title}
            size += len(text)
    words = " ".join(parsed["keywords"]) or question
    for r, _, _ in search.notes_search(words)[:4]:
        n = len(excerpts) + 1
        said = " ".join(m.get("transcript", "") for m in json.loads(r["media"]))
        excerpts.append(f"[{n}] 随记（{time.strftime('%Y-%m-%d', time.localtime(r['created']))}）：{r['text']} {said}"[:1500])
        cites[n] = {"kind": "note", "note": r["id"], "title": (r["text"] or "随记")[:20]}
    if not excerpts:
        return {"answer": "片库和随记里没找到和这个问题有关的内容。", "cites": {}, "understood": search.understood(parsed)}
    out = llm.llm_json(ASK_SYSTEM, f"QUESTION: {question}\n\nEXCERPTS:\n" + "\n\n".join(excerpts), {"answer": "string", "used": "array"},
                       3000, {}, "ask")
    return {"answer": str(out.get("answer") or ""), "cites": {str(k): v for k, v in cites.items()},
            "understood": search.understood(parsed)}


# The other modules, imported last: they import this one too, and are only used at run time
from . import llm, search  # noqa: E402
