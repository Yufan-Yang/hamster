#!/opt/grabber/venv/bin/python
"""One-off: re-run the classifier on finished videos with the current rules (full original titles,
creator tag, never-empty tags), rename files whose title changed, and sync Plex.

Fresh metadata comes from yt-dlp for sites it supports; for everything else (sniffed pages) only what
is already stored is used — those pages aren't fetched again."""
import json
import sys
import urllib.parse
from pathlib import Path

import grabber as G

REFETCH_HOSTS = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv", "archive.org", "pornhub.com")


def fresh_meta(url):
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    if not any(host == h or host.endswith("." + h) for h in REFETCH_HOSTS):
        return None
    info = G.ytdlp_probe(url)
    if not info:
        return None
    if info.get("_type") == "playlist":
        info = next(e for e in info["entries"] if e)
    meta = {k: info.get(k) for k in ("title", "uploader", "channel", "upload_date", "description", "extractor_key",
                                     "tags", "categories", "artist", "track", "album", "series")}
    if meta.get("description") and len(meta["description"]) > 1500:
        meta["description"] = meta["description"][:1500] + " …[cut]"
    return meta


def rename(files, new_base):
    """Rename the main media file and its same-stem sidecars in place; return the new file list."""
    media = [Path(f) for f in files if Path(f).suffix.lower() in G.VIDEO_EXT | G.AUDIO_EXT and Path(f).exists()]
    if len(media) != 1 or media[0].stem == new_base:
        return files  # multi-video pages keep their numbered names
    moved = G.move_with_sidecars(media[0], media[0].parent, new_base)
    old_side = {f for f in files if Path(f).name.startswith(media[0].stem + ".")}
    return [str(p) for p in moved] + [f for f in files if f not in old_side and f != str(media[0])]


def main(only=None):
    G.init_db()
    G.update = G.update  # write straight to the database
    changed = []
    rows = G.q("SELECT * FROM jobs WHERE status='done' AND ref IS NULL ORDER BY id")
    for r in rows:
        if only and r["id"] not in only:
            continue
        old = json.loads(r["analysis"] or "{}")
        files = json.loads(r["files"] or "[]")
        main_file = next((f for f in files if Path(f).suffix.lower() in G.VIDEO_EXT | G.AUDIO_EXT), None)
        meta = fresh_meta(r["url"])
        source = "yt-dlp" if meta else "stored"
        if not meta:
            meta = {"title": old.get("title") or r["title"], "description": old.get("summary") or ""}
        meta.update(file=Path(main_file).name if main_file else "", source=source)
        new = G.classify(r["id"], meta["file"], meta, {})
        a = dict(old)
        # keep where it's filed and the transcript-based summary; take the new title, creator and tags
        a["title"] = new["title"] if source == "yt-dlp" else (old.get("title") or new["title"])
        a["creator"] = new.get("creator")
        a["tags"] = new["tags"] or old.get("tags") or []
        if not old.get("summary"):
            a["summary"] = new.get("summary") or ""
        a.setdefault("usage", {})
        new_files = files
        base = G.safe_name(a["title"])
        if main_file and base != Path(main_file).stem and source == "yt-dlp":
            new_files = rename(files, base)
        thumb = next((f for f in new_files if f.endswith(".jpg")), r["thumb"])
        G.update(r["id"], analysis=a, title=a["title"], files=new_files, thumb=thumb, stage="")
        for linked in G.q("SELECT id FROM jobs WHERE ref=? AND status='done'", (r["id"],)):
            G.update(linked["id"], analysis=a, title=a["title"], files=new_files, thumb=thumb)
        vid = next((f for f in new_files if Path(f).suffix.lower() in G.VIDEO_EXT | G.AUDIO_EXT), None)
        if vid:
            changed.append((vid, a))
        print(f"#{r['id']} [{source}] title {'renamed' if new_files != files else 'kept'}, "
              f"{len(old.get('tags') or [])} -> {len(a['tags'])} tags, creator: {'yes' if a.get('creator') else 'no'}",
              flush=True)
    G.plex_refresh()
    G.plex_set_metadata(changed)
    try:
        print("tag merge:", G.merge_tags())
    except Exception as e:
        print("tag merge failed:", e)


if __name__ == "__main__":
    main({int(x) for x in sys.argv[1:]} or None)
