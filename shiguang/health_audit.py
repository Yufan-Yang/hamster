"""Proactive page/API checks and bounded, content-free browser error reports."""
import json
from pathlib import Path
import re
import shutil
import time
import urllib.request

from flask import g, request

from . import board, core, heal
from .core import app, q

INTERVAL = 1800


def audit(beat=lambda *_: None):
    root = Path(core.ENTRY).resolve().parent
    failures = []
    page = (root / 'index.html').read_text()
    assets = set(re.findall(r'(?:src|href)=["\'](/static/[^"\'?]+)', page))
    for asset in sorted(assets):
        path = root / asset.lstrip('/')
        if not path.resolve().is_relative_to(root / 'static') or not path.is_file() or not path.stat().st_size:
            failures.append(('assets', '页面资源缺失：' + asset))
    for endpoint in ('/', '/api/tasks'):
        beat({'phase':'checking', 'endpoint':endpoint})
        try:
            with urllib.request.urlopen('http://127.0.0.1:8088' + endpoint, timeout=8) as response:
                body = response.read(2 << 20)
                if endpoint == '/' and b'<html' not in body.lower():
                    raise ValueError('主页没有返回 HTML')
                if endpoint == '/api/tasks' and not isinstance(json.loads(body).get('workers'), list):
                    raise ValueError('任务接口缺少工人状态')
        except Exception as error:
            failures.append(('backend', '本机接口检查失败：' + endpoint + ' ' + type(error).__name__))
    previous = core.kv_get('health-audit:last', {})
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    browser_checked = False
    if browser and core.mem_available_mb() > 500 and time.time() - previous.get('browser_at',0) > 6 * 3600:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as engine:
                instance = engine.chromium.launch(executable_path=browser, headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
                try:
                    tab = instance.new_page()
                    tab.on('pageerror', lambda error: failures.append(('frontend', '浏览器执行异常：' + type(error).__name__)))
                    tab.goto('http://127.0.0.1:8088/', wait_until='domcontentloaded', timeout=20000)
                    tab.wait_for_timeout(2000)
                    browser_checked = True
                    if not tab.locator('#grid').count():
                        failures.append(('frontend', '首页缺少片库容器'))
                finally:
                    instance.close()
        except Exception as error:
            failures.append(('browser_audit', '浏览器巡检未完成：' + type(error).__name__))
    # Require recurrence for transient failures; evidence contains no page body, user title or URL query.
    confirmed = set(tuple(item) for item in previous.get('failures', [])) & set(failures)
    for kind, error in confirmed:
        heal.report(kind, '主动巡检', error, context={'check':'scheduled public page/API audit'})
    receipt = {'checked':time.time(), 'failures':failures, 'confirmed':len(confirmed), 'browser_checked':browser_checked,
               'browser_at':time.time() if browser_checked else previous.get('browser_at',0)}
    core.kv_set('health-audit:last', receipt)
    return receipt


@board.task('steward_health', '拾光前后端主动巡检', 'steward')
def run(task, beat):
    return audit(beat)


def tick():
    last = q("SELECT state,updated FROM tasks WHERE kind='steward_health' AND target='system'", one=True)
    if not last or last['state'] not in ('queued','running') and time.time() - last['updated'] > INTERVAL:
        board.publish('steward_health', 'system', 1, force=True, payload={'title':'拾光前后端主动巡检'})


@app.post('/api/health/client')
def client_error():
    request.max_content_length = 2048
    body = request.get_json(silent=True)
    if not g.user:
        return {'error':'请先登录'}, 401
    if not isinstance(body, dict) or set(body) != {'file','line','name'}:
        return {'error':'错误报告无效'}, 400
    file = body['file']
    if not isinstance(file, str) or not (file == '/' or re.fullmatch(r'/static/[a-z_]+\.js',file)):
        return {'error':'脚本位置无效'}, 400
    if type(body['line']) is not int or not 1 <= body['line'] <= 100000 or body['name'] not in ('Error','TypeError','ReferenceError','SyntaxError','RangeError'):
        return {'error':'错误位置无效'}, 400
    root = Path(core.ENTRY).resolve().parent
    if file != '/' and not (root / file.lstrip('/')).is_file():
        return {'error':'脚本不存在'}, 400
    source=root/'index.html' if file=='/' else root/file.lstrip('/')
    if body['line']>len(source.read_text().splitlines()):
        return {'error':'错误位置超出脚本范围'},400
    day = int(time.time() // 86400)
    with board.mutation() as db:
        key = 'health-client:' + g.owner
        row = db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
        data = json.loads(row['v']) if row else {}
        sent = data.get('sent', []) if data.get('day') == day else []
        fingerprint = [file, body['line'], body['name']]
        if fingerprint in sent or len(sent) >= 3:
            return {'accepted':False}, 200
        sent.append(fingerprint)
        db.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (key,json.dumps({'day':day,'sent':sent})))
    kind = 'frontend:' + file.rsplit('/',1)[-1] + ':' + str(body['line'])
    heal.report(kind, '前端执行异常', body['name'] + ' at ' + file + ':' + str(body['line']),
                context={'file':file,'line':body['line'],'name':body['name']})
    return {'accepted':True}
