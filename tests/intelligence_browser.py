#!/usr/bin/env python3
"""Local browser acceptance with a disposable database and deterministic model.

Run with Python + Playwright installed. Uses Playwright Chromium by default;
CHROMIUM may name an existing browser executable. Screenshots/report remain in
the printed temporary directory. No production state or model service is used.
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(tempfile.mkdtemp(prefix="shiguang-intelligence-browser-"))
os.environ.update(
    DB_PATH=str(OUT / "test.db"), STATE_DIR=str(OUT / "state"),
    MEDIA_ROOT=str(OUT / "media"), LLM_API_KEY="", GRABBER_PASSWORD="",
    COMPUTE_TOKEN="browser-qa-only", TELEGRAM_BOT_TOKEN="",
)
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from shiguang import core, live, llm, web  # noqa: E402

core.STATE.mkdir(parents=True)
core.init_db()
web.setup_app()
logging.getLogger("werkzeug").setLevel(logging.ERROR)
server = make_server("127.0.0.1", 0, core.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_port}"
results, errors, model_calls, blocked_external = [], [], [], []


def record(name, fn):
    started = time.time()
    try:
        detail = fn()
        results.append({"case": name, "passed": True, "detail": detail,
                        "seconds": round(time.time() - started, 2)})
        print("PASS", name, flush=True)
    except Exception as exc:
        results.append({"case": name, "passed": False, "error": str(exc)})
        print("FAIL", name, str(exc), flush=True)
        raise


def mocked_model(system, prompt, fields, *args, **kwargs):
    purpose = args[2] if len(args) > 2 else kwargs.get("kind")
    body = json.loads(prompt)
    model_calls.append(purpose)
    if purpose == "learning-plan":
        if "失败" in body["goal"]:
            raise RuntimeError("Intentional browser QA model failure")
        chosen = next(item for item in body["candidate_videos"] if "Python" in item["title"])
        return {
            "overview": "先理解基本概念，再动手写一个简单的小工具。",
            "steps": [{"job_id": chosen["job_id"], "title": "第一步：掌握基础语法",
                       "reason": "片库简介包含变量、循环和函数，适合从基础开始。",
                       "exercise": "写一个计算本周学习时长的小程序。"}],
            "limitations": ["现有素材还没有完整的项目案例。"],
        }
    if purpose == "live-copy":
        return {"options": [{
            "title": "开店之前先算清租金与客流", "cover_title": "你的店需要多少客户？",
            "description": "主播分享开店前估算租金与每日客流的方法。",
            "tags": ["创业", "开店", "经营"],
            "reason": "把字幕中的两个具体准备事项放在一起。",
            "evidence": [item["id"] for item in body["excerpts"][:2]],
        }], "limitation": "请结合原片核对语气与上下文。"}
    raise AssertionError(f"Unexpected model call: {purpose!r}")


def seed(owner):
    now = time.time()
    core.MEDIA.mkdir(parents=True, exist_ok=True)
    for i, (title, folder) in enumerate([
        ("Python 基础：变量、循环与函数", "Tutorials"),
        ("做一条日常短视频：构图与节奏", "Tutorials"),
        ("周末山间散步记录", "Vlogs"),
        ("人工智能的基本原理", "Talks"),
    ]):
        analysis = {"title": title, "library": "Videos", "folder": folder,
                    "summary": f"{title}的入门讲解与实践方法。",
                    "key_points": ["理解核心概念", "通过小练习巩固"],
                    "tags": [folder], "creator": "拾光测试课堂"}
        thumb = core.MEDIA / f"browser-thumb-{i}.jpg"
        image = Image.new("RGB", (640, 360), (35 + i * 30, 75, 125))
        ImageDraw.Draw(image).text((32, 160), f"VIDEO {i + 1}", fill="white", font_size=40)
        image.save(thumb)
        core.q("INSERT INTO jobs(owner,url,title,status,analysis,created,thumb) VALUES (?,?,?,'done',?,?,?)",
               (owner, f"https://example.invalid/browser-qa/{i}", title,
                json.dumps(analysis, ensure_ascii=False), now - i, str(thumb)))
        jid = core.q("SELECT MAX(id) id FROM jobs", one=True)["id"]
        core.q("INSERT INTO watch(owner,job_id,pos,dur,updated) VALUES (?,?,120,1200,?)", (owner, jid, now-i))
    core.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for i, title in enumerate(("理解亲密关系", "乡土生活与社会", "计算机视觉导读")):
        core.q("INSERT INTO books(owner,title,author,fmt,view,status,cover,created,updated) "
               "VALUES (?,?,?,'epub','flow','ready',1,?,?)", (owner, title, "测试书架", now, now))
        bid = core.q("SELECT MAX(id) id FROM books", one=True)["id"]
        cover = Image.new("RGB", (300, 400), (145 + i * 25, 98 + i * 20, 50))
        ImageDraw.Draw(cover).text((30, 180), f"BOOK {i + 1}", fill="white", font_size=35)
        cover.save(core.BOOKS_DIR / f"{bid}.jpg")
        core.q("INSERT INTO book_read(owner,book,pct,updated) VALUES (?,?,.25,?)", (owner, bid, now-i))
    core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,enabled,settings) "
           "VALUES (?,123456,11,'https://live.bilibili.com/123456','测试主播',0,?)",
           (owner, json.dumps(live.settings())))
    sid = core.q("SELECT MAX(id) id FROM live_sources", one=True)["id"]
    origin = json.dumps({"name": "测试主播", "room_id": 123456, "uid": 11,
                         "url": "https://live.bilibili.com/123456"})
    core.q("INSERT INTO live_sessions(key,source_id,owner,title,origin,status,started,ended) "
           "VALUES ('browser-session',?,?,?,?,'ended',?,?)",
           (sid, owner, "开店前的准备", origin, now - 3600, now - 3000))
    segments = json.dumps([[0, 10, "开店前先计算租金。"],
                           [10, 30, "再估算每天需要多少客户。"]], ensure_ascii=False)
    core.q("INSERT INTO live_recordings(source_id,session_key,owner,seq,path,offset,duration,state,segments) "
           "VALUES (?,'browser-session',?,0,?,0,60,'ready',?)",
           (sid, owner, str(OUT / "unused.mkv"), segments))
    preview = live.workspace() / "browser-preview.mp4"
    preview.write_bytes(b"")  # Preview workflow fixture; this test does not assess video decoding.
    core.q("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state,file,version,render_version,created,updated) "
           "VALUES (?,?,'browser-session','highlight',0,30,'我的原始文案','ready',?,1,1,?,?)",
           (owner, sid, str(preview), now, now))
    primary = core.q("SELECT MAX(id) id FROM live_clips", one=True)["id"]
    core.q("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state,file,version,render_version,created,updated) "
           "VALUES (?,?,'browser-session','topic',0,50,'完整话题待处理','ready',?,1,1,?,?)",
           (owner, sid, str(preview), now, now))
    return primary


def assert_layout(page):
    metrics = page.evaluate("""() => ({
      viewport: innerWidth, page: document.documentElement.scrollWidth,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      dialogs: [...document.querySelectorAll('.sheet-bg:not([hidden]) .sheet')]
        .map(el => ({width:el.clientWidth, content:el.scrollWidth}))
    })""")
    assert not metrics["overflow"], metrics
    assert all(item["content"] <= item["width"] + 1 for item in metrics["dialogs"]), metrics
    return metrics


try:
    with patch.object(llm, "llm_json", side_effect=mocked_model), sync_playwright() as p:
        launch = {"headless": True}
        if os.environ.get("CHROMIUM"):
            launch["executable_path"] = os.environ["CHROMIUM"]
        browser = p.chromium.launch(**launch)
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      reduced_motion="reduce", service_workers="block")

        def route_local_only(route):
            request_url = route.request.url
            if urlsplit(request_url).scheme in ("http", "https") and not request_url.startswith(URL + "/"):
                blocked_external.append(request_url)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_local_only)
        page = context.new_page()
        clip_writes = []
        page.on("request", lambda request: clip_writes.append(request.url)
                if request.method == "POST" and "/api/live/clips" in request.url else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(URL, wait_until="networkidle")
        page.wait_for_function("storageOwner !== 'unidentified'")
        page.evaluate("openAccount();renderAuth('register')")
        page.fill("#username", "browser-qa")
        page.fill("#password", "browser-qa-password-123")
        page.fill("#password2", "browser-qa-password-123")
        page.click("#authBtn")
        page.wait_for_function("state.user === 'browser-qa'")
        page.wait_for_selector("#accountSheet", state="hidden")
        clip_id = seed("user:browser-qa")
        page.evaluate("refresh()")
        page.wait_for_function("state.jobs.length === 4")

        def library_proportions():
            detail = []
            for width in (2048, 1440, 1280, 1024, 768, 390, 360):
                page.set_viewport_size({"width":width, "height":1000})
                page.evaluate("setChip('all');window.scrollTo(0,0)")
                page.wait_for_selector("#controwRead .bcover img")
                video = page.locator("#controw .cover").first.bounding_box()
                grid = page.locator("#grid .cover").first.bounding_box()
                book = page.locator("#controwRead .bcover").first.bounding_box()
                assert abs(video["width"] / video["height"] - 16/9) < .02, video
                assert abs(book["width"] / book["height"] - 3/4) < .02, book
                assert book["width"] <= 145 and book["height"] > book["width"], book
                if width > 640:
                    assert abs(video["width"] - grid["width"]) < 3, (width,video,grid)
                one_width = page.evaluate("""() => {
                    const original = state.jobs; state.jobs = original.slice(0,1); render();
                    const width = document.querySelector('#controw .cover').getBoundingClientRect().width;
                    state.jobs = original; render(); return width;
                }""")
                assert abs(one_width - video["width"]) < 1, (width,one_width,video)
                assert_layout(page)
                if width in (2048,1440,390):
                    page.screenshot(path=str(OUT / f"library-proportions-{width}.png"), full_page=True)
                detail.append({"width":width,"video_width":round(video["width"]),
                               "book_width":round(book["width"]),"book_height":round(book["height"])})
            return detail
        record("landscape video rails, portrait books and stable card sizes across content counts", library_proportions)

        def navigation():
            detail = []
            for width in (1440, 390, 360):
                page.set_viewport_size({"width": width, "height": 900})
                for name, key in (("片库", "all"), ("随记", "notes"), ("书架", "books"),
                                  ("追更", "subs"), ("直播切片", "live")):
                    page.locator("#workspaceNav").get_by_role("button", name=name, exact=True).click()
                    page.wait_for_function("(key)=>chip===key && !workspaceLoading", arg=key)
                    if key == "live":
                        page.wait_for_function("liveData !== null")
                        assert page.locator("#search").is_disabled()
                        assert page.evaluate("liveTab") == "review"
                        assert page.evaluate("liveData.counts.review") == 2
                        assert page.locator(".lv-clip .lv-next").count() == 2
                        page.evaluate("window.scrollTo(0, 0)")
                        first_card_top = page.locator(".lv-clip").first.bounding_box()["y"]
                        page.screenshot(path=str(OUT / f"live-{width}.png"), full_page=True)
                        assert first_card_top <= (450 if width > 640 else 650), (width, first_card_top)
                        if width <= 640:
                            assert page.locator(".search").is_hidden()
                            filters = page.locator(".lv-filters label").all()
                            assert abs(filters[0].bounding_box()["y"] - filters[1].bounding_box()["y"]) <= 1
                    metrics = assert_layout(page)
                    assert page.locator("#workspaceNav button").count() == 5
                    if key == "all":
                        assert page.locator("#chips").is_visible()
                        assert page.get_by_role("button", name="智能学习清单", exact=True).is_visible()
                        page.screenshot(path=str(OUT / f"library-{width}.png"), full_page=True)
                    else:
                        assert page.locator("#chips").is_hidden()
                    detail.append({"width": width, "workspace": key, **metrics,
                                   **({"first_card_top": first_card_top} if key == "live" else {})})
                page.screenshot(path=str(OUT / f"live-{width}.png"), full_page=True)
            return detail
        record("real navigation at desktop/mobile widths without page overflow", navigation)

        def learning_plan():
            page.set_viewport_size({"width": 390, "height": 900})
            page.locator("#workspaceNav").get_by_role("button", name="片库", exact=True).click()
            page.get_by_role("button", name="智能学习清单", exact=True).click()
            before = len(model_calls)
            page.get_by_role("button", name="从零学习 Python", exact=True).click()
            assert len(model_calls) == before
            page.click("#learningGo")
            page.wait_for_selector("#learningResult .learning-source")
            assert "Python 基础：变量、循环与函数" in page.locator("#learningResult").inner_text()
            assert "学习时长" in page.locator("#learningResult").inner_text()
            assert len(model_calls) == before + 1
            assert_layout(page)
            page.screenshot(path=str(OUT / "learning-mobile.png"), full_page=True)
            page.locator("#learningResult .learning-source").click()
            page.wait_for_selector("#watch:not([hidden]) .wtitle")
            assert "Python 基础" in page.locator("#watch .wtitle").inner_text()
            page.locator("#watch .wbar button").click()
            page.wait_for_selector("#watch", state="hidden")
            page.get_by_role("button", name="智能学习清单", exact=True).click()
            page.fill("#learningGoal", "失败测试：学习 Python 的变量")
            page.click("#learningGo")
            page.wait_for_selector("#learningError:not([hidden])")
            assert page.input_value("#learningGoal") == "失败测试：学习 Python 的变量"
            assert page.locator("#learningGo").is_enabled()
            page.screenshot(path=str(OUT / "learning-error-mobile.png"), full_page=True)
            page.keyboard.press("Escape")
            page.wait_for_selector("#learningSheet", state="hidden")
            return {"model_calls": len(model_calls) - before, "source_opened": True, "input_preserved": True}
        record("learning plan examples, generation, real source opening and recoverable failure", learning_plan)

        def live_copy():
            page.locator("#workspaceNav").get_by_role("button", name="直播切片", exact=True).click()
            page.wait_for_function("(id)=>liveData?.clips.some(c=>c.id===id)", arg=clip_id)
            page.locator(".lv-clip").first.get_by_role("button", name="预览成片", exact=True).click()
            page.wait_for_selector(".lv-preview-footer")
            assert "1 / 2" in page.locator(".lv-preview-footer").inner_text()
            assert page.get_by_role("button", name="确认投稿 B站", exact=True).count() == 0
            page.get_by_role("button", name="下一条", exact=True).click()
            assert "2 / 2" in page.locator(".lv-preview-footer").inner_text()
            assert page.locator("#liveModal h2").inner_text() == "我的原始文案"
            page.get_by_role("button", name="上一条", exact=True).click()
            assert page.locator("#liveModal h2").inner_text() == "完整话题待处理"
            assert_layout(page)
            page.screenshot(path=str(OUT / "live-review-preview-mobile.png"), full_page=True)
            page.keyboard.press("Escape")
            page.wait_for_selector("#liveModal", state="detached")
            card = page.locator(f'[data-lv-clip="{clip_id}"]')
            card.locator(".lv-card-details > summary").click()
            card.get_by_role("button", name="编辑", exact=True).click()
            page.locator("#liveModal summary").filter(has_text="AI 文案助手").click()
            before = len(model_calls)
            page.get_by_role("button", name="生成文案建议", exact=True).click()
            page.wait_for_selector(".lv-copy-card")
            assert len(model_calls) == before + 1
            page.get_by_text("查看字幕依据", exact=True).click()
            assert "开店前先计算租金" in page.locator(".lv-copy-output").inner_text()
            assert "再估算每天需要多少客户" in page.locator(".lv-copy-output").inner_text()
            assert_layout(page)
            page.screenshot(path=str(OUT / "live-copy-evidence-mobile.png"), full_page=True)
            page.get_by_role("button", name="填入这套文案", exact=True).click()
            assert page.input_value("#liveModal [name=title]") == "开店之前先算清租金与客流"
            assert core.q("SELECT title FROM live_clips WHERE id=?", (clip_id,), one=True)["title"] == "我的原始文案"
            page.get_by_role("button", name="撤回填入", exact=True).click()
            assert page.input_value("#liveModal [name=title]") == "我的原始文案"
            page.get_by_role("button", name="生成文案建议", exact=True).click()
            page.wait_for_selector(".lv-copy-card")
            page.fill("#liveModal [name=end]", "00:00:20")
            page.get_by_role("button", name="填入这套文案", exact=True).click()
            assert "范围或侧重已改变" in page.locator(".lv-copy-output").inner_text()
            assert page.input_value("#liveModal [name=title]") == "我的原始文案"
            page.keyboard.press("Escape")
            page.wait_for_selector("#liveModal", state="detached")
            assert core.q("SELECT title FROM live_clips WHERE id=?", (clip_id,), one=True)["title"] == "我的原始文案"
            assert core.q("SELECT COUNT(*) n FROM tasks", one=True)["n"] == 0
            assert not clip_writes, clip_writes
            return {"evidence_visible": True, "apply_undo": True, "stale_range_rejected": True,
                    "continuous_review_preview": True, "saved_or_published": False}
        record("live review previews, copy evidence, apply/undo and stale range protection without save/publication", live_copy)
        assert not errors, errors
        assert not blocked_external, blocked_external
        record("no unhandled browser errors or external requests", lambda: {"browser_errors": errors})
        browser.close()
finally:
    server.shutdown()
    core.DB.close()
    (OUT / "report.json").write_text(json.dumps(
        {"results": results, "browser_errors": errors, "model_calls": model_calls,
         "blocked_external": blocked_external}, ensure_ascii=False, indent=2))
    print("ARTIFACTS", OUT, flush=True)
