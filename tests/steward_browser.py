#!/usr/bin/env python3
"""Real HTTP/browser acceptance on disposable media; no production state or model service.

CHROMIUM may name an installed browser. QA_TMPDIR controls the output location.
"""
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(tempfile.mkdtemp(prefix='shiguang-steward-browser-', dir=os.environ.get('QA_TMPDIR')))
os.environ.update(DB_PATH=str(OUT / 'test.db'), STATE_DIR=str(OUT / 'state'),
                  MEDIA_ROOT=str(OUT / 'media'), LLM_API_KEY='', GRABBER_PASSWORD='',
                  COMPUTE_TOKEN='steward-browser-only', TELEGRAM_BOT_TOKEN='')
sys.path.insert(0, str(ROOT))
from shiguang import board, core, llm, steward, web  # noqa: E402

core.MEDIA.mkdir(parents=True)
core.init_db()
web.setup_app()
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, core.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f'http://127.0.0.1:{server.server_port}'
results, errors, calls, executed = [], [], [], []
stop = threading.Event()


def record(name, function):
    started = time.time()
    try:
        function()
        results.append({'name': name, 'passed': True, 'seconds': round(time.time() - started, 2)})
        print('PASS', name, flush=True)
    except Exception as error:
        results.append({'name': name, 'passed': False, 'error': str(error)})
        raise


def model(system, user, fields, max_tokens, usage, purpose='', job_id=None, think=True):
    calls.append(purpose)
    if purpose == 'media-steward':
        return {'order': [item['key'] for item in reversed(json.loads(user)['candidates'])]}
    if purpose == 'summarize':
        return {'summary': '片段介绍室内布光的基本步骤。', 'key_points': ['先确定主光方向'],
                'tags': ['摄影'], 'chapters': [{'t': 0, 'title': '布光准备'}, {'t': 12, 'title': '调整主光'}],
                'point_times': [0]}
    raise AssertionError('Unexpected model purpose: ' + purpose)


def worker():
    kinds = ['steward_scan', 'steward_index', 'summarize', 'chapters', 'steward_diagnose', 'steward_care_apply', 'steward_care_undo']
    while not stop.wait(.05):
        try:
            task = board.claim_task('qa-steward-browser', kinds)
            if task:
                executed.append(task['kind'])
                board.run_claimed(task, 'qa-steward-browser')
        except Exception as error:
            errors.append('worker: ' + type(error).__name__ + ': ' + str(error))


template = core.MEDIA / 'template.mp4'
subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=1',
                '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', '30', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-c:a', 'aac', '-y', str(template)], check=True, timeout=45)


def seed(owner, title):
    now = time.time()
    core.q("INSERT INTO jobs(owner,url,title,status,analysis,created,updated) VALUES(?,?,?,'done',?,?,?)",
           (owner, 'https://example.test/qa/' + str(time.time_ns()), title,
            json.dumps({'library': 'Videos', 'needs_transcript': True, 'language': 'zh'}), now, now))
    jid = core.q('SELECT MAX(id) id FROM jobs', one=True)['id']
    path = core.MEDIA / f'qa-{jid}.mp4'
    shutil.copyfile(template, path)
    path.with_suffix('.zh.srt').write_text('\n\n'.join(
        f'{i+1}\n00:00:{i:02},000 --> 00:00:{i+1:02},000\n摄影布光示例第{i+1}步' for i in range(24)) + '\n')
    core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(path)]), jid))
    return jid


browser = None
try:
    with patch.object(llm, 'llm_json', side_effect=model), patch.object(llm, 'merge_tags_if_new'):
        # Speed up the real scheduler in this isolated acceptance process only.
        with patch.object(steward, 'SCAN_INTERVAL', .1, create=True):
            threading.Thread(target=worker, daemon=True).start()
            threading.Thread(target=steward.loop, daemon=True).start()
            with sync_playwright() as playwright:
                executable = os.environ.get('CHROMIUM') or shutil.which('chromium')
                browser = playwright.chromium.launch(executable_path=executable, headless=True,
                                                       args=['--no-sandbox', '--disable-gpu'])
                context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                context.route('**/*', lambda route: route.continue_() if urlsplit(route.request.url).hostname == '127.0.0.1' else route.abort())
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(URL, wait_until='networkidle')
                page.wait_for_function("storageOwner !== 'unidentified'")

                def register():
                    page.evaluate('openAccount()')
                    page.wait_for_selector('#accountSheet:not([hidden])')
                    page.evaluate("renderAuth('register')")
                    page.fill('#username', 'steward-qa')
                    page.fill('#password', 'disposable-qa-password')
                    page.fill('#password2', 'disposable-qa-password')
                    page.click('#authBtn')
                    page.wait_for_function("state.user === 'steward-qa'")
                    page.evaluate("hide($('#accountSheet'))")
                record('register through real UI', register)
                owner = page.evaluate('storageOwner')
                jid = seed(owner, '公开摄影教程')
                private = seed(owner, 'QA_PRIVATE_TITLE')
                core.q('INSERT OR REPLACE INTO privacy(owner,tags,ids) VALUES(?,?,?)', (owner, '[]', json.dumps([private])))

                def inventory():
                    page.evaluate('openSteward()')
                    page.wait_for_selector('#stewardProfile')
                    page.locator('#stewardBody').evaluate('(e)=>Promise.all(e.getAnimations().map(a=>a.finished))')
                    assert not calls and not set(executed) & {'steward_index','summarize','transcribe','steward_care_apply'}
                    assert not page.locator('#stewardAutomatic').is_checked()
                    page.get_by_role('button', name='盘点当前范围', exact=True).click()
                    page.wait_for_function("stewardView.data?.items.some(x=>x.job_id === " + str(jid) + " && x.state === 'needs_work')")
                    assert 'steward_index' not in executed and 'summarize' not in executed
                    assert page.evaluate('Object.values(stewardView.data.counts).reduce((a,b)=>a+b,0)') == 1
                    assert 'QA_PRIVATE_TITLE' not in page.locator('#stewardBody').inner_text()
                record('read-only inventory, default-off policy and private counts', inventory)

                def manual_completion():
                    page.get_by_role('button', name='补全当前范围', exact=True).click()
                    page.wait_for_function("stewardView.data?.items.some(x=>x.job_id === " + str(jid) + " && x.state === 'ready')", timeout=25000)
                    assert {'steward_index','summarize'}.issubset(executed)
                    assert 'transcribe' not in executed
                    assert calls == ['summarize'], calls
                    assert not steward.policy(owner)['automatic']
                    subtitle = core.MEDIA / f'qa-{jid}.zh.srt'
                    subtitle.write_text(subtitle.read_text() + '\n25\n00:00:24,000 --> 00:00:25,000\n补充调整主光的说明\n')
                    requested = time.time()
                    page.get_by_role('button', name='补全当前范围', exact=True).click()
                    page.wait_for_function('!stewardView.busy')
                    page.locator('#stewardBody').get_by_role('button', name='关闭', exact=True).click()
                    deadline = time.time() + 25
                    while time.time() < deadline:
                        row = core.q('SELECT state,checked FROM media_steward_assets WHERE job_id=?', (jid,), one=True)
                        if row and row['state'] == 'ready' and row['checked'] > requested and calls.count('summarize') == 2:
                            break
                        time.sleep(.1)
                    else:
                        raise AssertionError('manual batch stopped when the panel closed')
                    page.evaluate('openSteward()')
                    page.wait_for_function("stewardView.data?.items.some(x=>x.job_id === " + str(jid) + " && x.state === 'ready')")
                    assert page.locator('#stewardBody').evaluate('(e)=>e.scrollWidth <= e.clientWidth')
                    page.screenshot(path=str(OUT/'mobile.png'), full_page=True)
                record('one manual request completes import, summary and verification', manual_completion)

                def organize_and_undo():
                    page.wait_for_function('stewardView.data?.care?.items.some(x=>x.job_id === '+str(jid)+')')
                    original = json.loads(core.q('SELECT files FROM jobs WHERE id=?',(jid,),one=True)['files'])[0]
                    page.get_by_role('button',name='按当前分类归档',exact=True).click()
                    page.wait_for_function("stewardView.data?.care?.operations.some(x=>x.state === 'done')",timeout=20000)
                    current = json.loads(core.q('SELECT files FROM jobs WHERE id=?',(jid,),one=True)['files'])[0]
                    assert Path(current).is_file() and not Path(original).exists()
                    page.get_by_role('button',name='撤销归档',exact=True).click()
                    page.wait_for_function("stewardView.data?.care?.operations.some(x=>x.state === 'undone')",timeout=20000)
                    assert Path(original).exists()
                    assert not page.evaluate('stewardView.data.care.items.some(x=>x.job_id === '+str(jid)+')')
                record('real file organization, receipt, undo and retained layout preference',organize_and_undo)

                def automatic_and_ai():
                    first = seed(owner, '自动整理一')
                    second = seed(owner, '自动整理二')
                    page.locator('#stewardAI').check()
                    page.locator('#stewardAutomatic').check()
                    page.get_by_role('button', name='保存处理规则').click()
                    page.wait_for_function('stewardView.data?.policy.automatic === true')
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        rows = core.q("SELECT job_id,state FROM media_steward_assets WHERE job_id IN (?,?,?)", (first,second,private))
                        if len(rows) == 3 and all(row['state'] == 'ready' for row in rows):
                            break
                        time.sleep(.1)
                    else:
                        raise AssertionError('automatic workflow did not reach ready')
                    assert 'media-steward' in calls
                    page.evaluate('loadSteward()')
                    page.wait_for_function('stewardView.data.items.length === 3')
                    assert 'QA_PRIVATE_TITLE' not in page.locator('#stewardBody').inner_text()
                record('background loop processes real artifacts and validated AI ordering', automatic_and_ai)

                def pause():
                    page.locator('#stewardAutomatic').uncheck()
                    page.get_by_role('button', name='保存处理规则').click()
                    page.wait_for_function('stewardView.data?.policy.automatic === false')
                    added = seed(owner, '暂停后新增')
                    page.wait_for_timeout(600)
                    assert not core.q('SELECT 1 FROM media_steward_actions WHERE job_id=?', (added,), one=True)
                record('pause stops new automatic dispatch', pause)

                def desktop_and_logout():
                    page.set_viewport_size({'width': 1280, 'height': 900})
                    page.evaluate('loadSteward()')
                    page.wait_for_timeout(300)
                    assert page.locator('#stewardBody').evaluate('(e)=>e.scrollWidth <= e.clientWidth')
                    page.screenshot(path=str(OUT/'desktop.png'), full_page=True)
                    page.evaluate('resetSteward();logout()')
                    page.wait_for_function("!state.user && !storageOwner.startsWith('user:')")
                    assert page.locator('#stewardSheet').is_hidden()
                    assert page.locator('#stewardBody').inner_html() == ''
                record('desktop layout and logout clear personal state', desktop_and_logout)
                assert not errors, errors
                browser.close()
                browser = None
finally:
    stop.set()
    if browser:
        try:
            browser.close()
        except Exception:
            pass
    server.shutdown()
    report = {'results': results, 'page_errors': errors, 'model_calls': calls, 'executed': executed,
              'passed': len(results) == 7 and all(item['passed'] for item in results) and not errors}
    (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Browser report:', OUT, flush=True)
