#!/usr/bin/env python3
"""Run on the Pi with Playwright/Chromium. Fresh database, no external downloads or publications."""
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(tempfile.mkdtemp(prefix='shiguang-usability-', dir='/var/tmp'))
os.environ.update(DB_PATH=str(OUT/'test.db'), STATE_DIR=str(OUT/'state'), MEDIA_ROOT=str(OUT/'media'),
                  LLM_API_KEY='', GRABBER_PASSWORD='', COMPUTE_TOKEN='qa-only')
sys.path.insert(0, str(ROOT))
from shiguang import books, core, live, web  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

core.STATE.mkdir(parents=True); core.init_db(); web.setup_app()
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server = make_server('127.0.0.1', 0, core.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{server.server_port}'
results, errors = [], []


def record(name, fn):
    started = time.time()
    try:
        detail = fn()
        results.append({'case': name, 'passed': True, 'seconds': round(time.time()-started, 2), 'detail': detail})
        print('PASS', name, flush=True)
    except Exception as exc:
        results.append({'case': name, 'passed': False, 'error': str(exc)})
        print('FAIL', name, str(exc), flush=True)
        raise


try:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox','--disable-gpu'])
        context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url, wait_until='networkidle'); page.wait_for_function("storageOwner !== 'unidentified'")

        def register(name):
            page.evaluate("openAccount()")
            page.wait_for_selector('#accountSheet:not([hidden])')
            page.evaluate("renderAuth('register')")
            page.fill('#username', name); page.fill('#password','qa-password-123'); page.fill('#password2','qa-password-123')
            page.click('#authBtn'); page.wait_for_function('(name)=>state.user===name', arg=name)
            page.wait_for_timeout(250)
        record('real UI registration and verified storage owner', lambda: register('qa-one'))
        assert page.evaluate('storageOwner') == 'user:qa-one'
        core.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        core.q("INSERT INTO books(owner,title,fmt,file,status,created,updated) VALUES ('user:qa-one','账号一的书','txt','qa.txt','importing',?,?)", (time.time(), time.time()))
        bid = core.q('SELECT MAX(id) id FROM books', one=True)['id']
        (core.BOOKS_DIR/'qa.txt').write_text('第一章 回归检查\n'+('这是用于离线阅读测试的文字。'*120)+'\n第二章 继续阅读\n'+('阅读进度会按账号保存。'*120))
        books.import_book(bid)

        def offline_owner_one():
            page.evaluate("setChip('books')")
            page.wait_for_function('booksState.loaded && booksState.books.length===1')
            page.evaluate('(id)=>cacheBook(bookById(id),{pinned:true})', bid)
            page.evaluate("$('#noteText').value='账号一私密草稿';$('#noteText').dispatchEvent(new Event('input'))")
            page.evaluate('(id)=>queueProgress(id,{pct:.2,pos:{ch:0,f:.2},secs:5})', bid)
            assert page.evaluate('cacheIndex.size') == 1
            context.set_offline(True)
            page.evaluate('loadBooks()')
            assert page.evaluate('booksState.offline && booksState.books.length===1')
            page.evaluate('(id)=>openReader(id)', bid)
            page.wait_for_function('rd.pack && !rd.busy')
            assert page.locator('#rdFlow').inner_text().strip()
            page.evaluate('rdClose(true)')
            context.set_offline(False); page.evaluate('refresh()')
            page.wait_for_function('!booksState.offline')
        record('cached TXT reading offline and reconnect', offline_owner_one)

        def account_isolation():
            page.evaluate('logout()'); page.wait_for_function('!state.user && !storageOwner.startsWith("user:")')
            register('qa-two')
            assert page.input_value('#noteText') == ''
            assert page.evaluate('Object.keys(pendingAll()).length') == 0
            context.set_offline(True); page.evaluate("setChip('books');loadBooks()")
            page.wait_for_function('booksState.offline')
            assert page.evaluate('booksState.books.length') == 0
            assert page.evaluate('cacheIndex.size') == 0
            assert page.locator('#reader').is_hidden()
            context.set_offline(False); page.evaluate('refresh()'); page.wait_for_function('!booksState.offline')
            # Old scope is retained separately and can only be read by returning to that scope.
            assert page.evaluate("lsGet(privateKey('noteDraft','user:qa-one'),'')") == '账号一私密草稿'
            assert page.evaluate("idb.forOwner('user:qa-one').all('meta').then(x=>x.length)") == 1
        record('logout, second account, draft/pending/cache/reader isolation', account_isolation)

        def inflight_cache():
            return page.evaluate('''async () => {
              const owner=storageOwner;
              let release; const gate=new Promise(r=>release=r), original=window.fetch;
              const fake={id:888,title:'旧账号下载中的书',author:'QA',version:1,view:'flow',status:'ready',cover:true};
              const pack={view:'flow',chapters:[],res:['slow.png']};
              window.fetch=(u,o)=>String(u).includes('slow.png')?gate:original(u,o);
              const task=cacheBook(fake,{pack}).catch(()=>null);
              await new Promise(r=>setTimeout(r,120));
              setStorageOwner('user:temporary-qa');
              release(new Response(new Blob(['image'])));await task;
              const rows=await idb.all('meta');window.fetch=original;
              setStorageOwner(owner);await refresh();await cacheDrop(fake.id);
              if(rows.some(x=>x.id===888))throw Error('in-flight cache crossed account');
              return {new_owner_cache:rows.length};
            }''')
        record('in-flight cache writes remain bound to the original account', inflight_cache)

        def add_failure():
            page.evaluate('openAdd()'); page.fill('#addText','https://example.invalid/keep-this-link')
            page.route('**/api/add', lambda route: route.abort('failed'))
            page.click('#addForm button[type=submit]'); page.wait_for_function("!document.querySelector('#addForm button[type=submit]').disabled")
            assert page.input_value('#addText') == 'https://example.invalid/keep-this-link'
            assert page.locator('#addSheet').is_visible()
            page.unroute('**/api/add'); page.keyboard.press('Escape'); page.wait_for_timeout(250)
        record('failed add preserves input and restores the submit button', add_failure)

        def out_of_order():
            return page.evaluate('''async () => {
              const original=window.fetch, base=JSON.parse(await (await original('/api/jobs')).text());
              let release;const gate=new Promise(r=>release=r);
              window.fetch=(u,o)=>String(u).includes('q=old')?gate:Promise.resolve(new Response(JSON.stringify({...base,qaMarker:'new'})));
              query='old';const old=refresh();query='new';await refresh();
              release(new Response(JSON.stringify({...base,qaMarker:'old'})));await old;
              const marker=state.qaMarker;window.fetch=original;query='';await refresh();
              if(marker!=='new')throw Error('old response won');return marker;
            }''')
        record('old search responses cannot overwrite newer results', out_of_order)

        def navigation():
            page.evaluate("setChip('live');lvTab('calibration')")
            assert 'tab=live' in page.url and 'live=calibration' in page.url
            page.reload(wait_until='networkidle'); page.wait_for_function("chip==='live' && liveTab==='calibration'")
            page.evaluate("setChip('books')"); page.wait_for_function("chip==='books'")
            page.go_back(wait_until='domcontentloaded'); page.wait_for_function("chip==='live'&&liveTab==='calibration'")
            page.evaluate("query='保留搜索';saveNavigation();setVideoSort('author')")
            page.reload(wait_until='networkidle'); page.wait_for_function("query==='保留搜索' && videoSort==='author' && liveTab==='calibration'")
            page.evaluate("query='';$('#search').value='';saveNavigation();refresh()")
        record('reload, URL state, browser back and search/sort restoration', navigation)

        def full_details():
            core.q("INSERT INTO jobs(owner,url,title,status,analysis,created) VALUES ('user:qa-two','https://example.invalid/qa-details','测试视频','done',?,?)",
                   (json.dumps({'title':'测试视频','summary':'这里是完整的视频详情','key_points':['详情要点']}),time.time()))
            jid=core.q('SELECT MAX(id) id FROM jobs',one=True)['id']
            page.evaluate("query='';$('#search').value='';setChip('all');refresh()")
            page.wait_for_function('(id)=>state.jobs.some(j=>j.id===id&&j.compact)',arg=jid)
            page.evaluate('(id)=>openWatch(id)',jid)
            page.wait_for_selector('#watch:not([hidden]) .sum')
            assert '完整的视频详情' in page.locator('#watch .sum').inner_text()
            assert '详情要点' in page.locator('#watch #points').inner_text()
            page.evaluate('closeWatch()');page.wait_for_timeout(250)
        record('compact cards fetch full summary and key points when opened',full_details)

        core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,enabled,settings) VALUES ('user:qa-two',123456,11,'https://live.bilibili.com/123456','测试主播',0,?)", (json.dumps(live.settings()),))
        sid = core.q('SELECT MAX(id) id FROM live_sources', one=True)['id']
        core.q('INSERT INTO live_rubrics(owner,source_id,version,rubric,evidence) VALUES (?,?,?,?,?)', ('user:qa-two',sid,1,json.dumps({'summary':'候选方案','rules':['证据规则']}),json.dumps({'protocol':6,'validated':False,'sample_count':20,'training_count':8,'tuning_count':4,'validation_count':0,'validation_evaluated':False,'default_tuning_accuracy':0.5,'selected_candidate':0,'candidate_trials':[{'candidate':0,'tuning_accuracy':0.5,'rules':['证据规则'],'rule_evidence':[]}],'rejection_reasons':['调优没有胜过默认标准']})))

        def live_state():
            page.evaluate("chip='live';liveTab='calibration';render();loadLive()")
            page.wait_for_selector('#liveWorkspace details')
            page.locator('#liveWorkspace details').first.evaluate('(el)=>el.open=true')
            page.evaluate('loadLive()')
            assert page.locator('#liveWorkspace details').first.evaluate('(el)=>el.open')
            # Change data so this also tests a DOM replacement, not just the unchanged-render shortcut.
            core.q("UPDATE live_sources SET name='测试主播新别名' WHERE id=?", (sid,))
            page.evaluate('loadLive()')
            assert page.locator('#liveWorkspace details').first.evaluate('(el)=>el.open')
            page.evaluate('(id)=>lvEditSource(id,true)', sid)
            assert page.locator('[name=reference_bvids]').is_visible()
            assert page.locator('[name=reference_mids]').is_visible()
            assert page.locator('[name=source_aliases]').is_visible()
            assert '近期效果验证样本' in page.locator('#liveWorkspace').inner_text()
            assert '独立风格素材' in page.locator('#liveWorkspace').inner_text()
            assert '未进行最终验证' in page.locator('#liveWorkspace').inner_text()
            assert '调优 4 个' in page.locator('#liveWorkspace').inner_text()
            assert 'NaN' not in page.locator('#liveWorkspace').inner_text()
            page.keyboard.press('Escape'); page.wait_for_timeout(220)
            assert page.locator('#liveModal').count() == 0
        record('live details survive changed polling; sample controls and Escape', live_state)

        def live_management():
            core.q("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state,created,updated) VALUES ('user:qa-two',?,'qa-management','highlight',0,60,'批量管理测试','ready',?,?)", (sid,time.time(),time.time()))
            cid=core.q('SELECT MAX(id) id FROM live_clips',one=True)['id']
            page.evaluate("lvTab('sources')")
            page.get_by_role('button',name='取消追踪',exact=True).click()
            page.wait_for_function('(id)=>liveData.sources.find(s=>s.id===id)?.archived===1',arg=sid)
            assert core.q('SELECT enabled FROM live_sources WHERE id=?',(sid,),one=True)['enabled']==0
            page.get_by_text('已取消追踪的 UP',exact=True).click()
            page.get_by_role('button',name='管理已有切片',exact=True).click()
            page.wait_for_selector('input[aria-label="选择切片：批量管理测试"]')
            page.locator('input[aria-label="选择切片：批量管理测试"]').check()
            page.wait_for_function("document.querySelector('.lv-bulk').textContent.includes('已选 1 条')")
            page.get_by_role('button',name='批量忽略',exact=True).click()
            page.wait_for_function('(id)=>liveData.clips.find(c=>c.id===id)?.state===\"rejected\"',arg=cid)
            assert core.q('SELECT id FROM live_clips WHERE id=?',(cid,),one=True)
            page.locator('input[aria-label="选择切片：批量管理测试"]').check()
            page.once('dialog',lambda dialog:dialog.accept())
            page.get_by_role('button',name='批量删除',exact=True).click()
            page.wait_for_function('(id)=>!liveData.clips.some(c=>c.id===id)',arg=cid)
            assert not core.q('SELECT id FROM live_clips WHERE id=?',(cid,),one=True)
            page.evaluate("lvSheet('音量测试','<video controls></video>');document.querySelector('#liveModal video').volume=0.24;document.querySelector('#liveModal video').muted=true")
            page.wait_for_timeout(100);page.evaluate('lvClose()')
            page.evaluate("lvSheet('再次预览','<video controls></video>')")
            assert page.locator('#liveModal video').evaluate('(v)=>v.volume')==0.24
            assert page.locator('#liveModal video').evaluate('(v)=>v.muted') is True
            page.evaluate('lvClose()');page.reload(wait_until='networkidle')
            page.evaluate("lvSheet('刷新后预览','<video controls></video>')")
            assert page.locator('#liveModal video').evaluate('(v)=>v.volume')==0.24
            page.evaluate('lvClose();liveSourceFilter=""')
        record('unfollow preserves clips; bulk ignore/delete and preview volume survive reopen/reload',live_management)

        def modals():
            page.evaluate("document.querySelector('#avatar').focus();document.querySelector('#usageBody').innerHTML='<h2>测试资源</h2><button>第一个</button><button>第二个</button>';document.querySelector('#usageSheet').hidden=false")
            page.wait_for_timeout(50)
            assert page.locator('#usageBody').get_attribute('role') == 'dialog'
            page.locator('#usageBody button').last.focus(); page.keyboard.press('Tab')
            assert page.locator('#usageBody button').first.evaluate('(el)=>el===document.activeElement')
            page.keyboard.press('Escape'); page.wait_for_timeout(240)
            assert page.locator('#usageSheet').is_hidden()
            assert page.locator('#avatar').evaluate('(el)=>el===document.activeElement')
            for sheet in ('weeklySheet','askSheet','bookSheet'):
                page.evaluate('(id)=>document.getElementById(id).hidden=false',sheet)
                page.keyboard.press('Escape');page.wait_for_timeout(220)
                assert page.locator('#'+sheet).is_hidden()
        record('modal Escape, dialog labels, focus trap and focus restoration', modals)

        def responsive():
            detail=[]
            for width in (360,390,768,1440):
                page.set_viewport_size({'width':width,'height':900})
                for tab in ('all','notes','books','live'):
                    page.evaluate('(tab)=>setChip(tab)',tab); page.wait_for_timeout(120)
                    metrics=page.evaluate('''() => ({overflow:document.documentElement.scrollWidth>innerWidth+1,
                        short:[...document.querySelectorAll('button.link,a.link,.chip,.btn,.seg button,#bookTabs .t')].filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.height<39;}).map(el=>el.textContent.trim())})''')
                    assert not metrics['overflow'], (width,tab,metrics)
                    assert not metrics['short'], (width,tab,metrics)
                    detail.append({'width':width,'tab':tab,**metrics})
                if width==390:page.screenshot(path=str(OUT/'live-mobile.png'),full_page=True)
            return detail
        record('four responsive widths, four workspaces, touch targets and no horizontal overflow', responsive)
        def offline_reload():
            page.evaluate('logout()');page.wait_for_function('!state.user')
            page.evaluate("openAccount();renderAuth('login')")
            page.fill('#username','qa-one');page.fill('#password','qa-password-123');page.click('#authBtn')
            page.wait_for_function("state.user==='qa-one'");page.wait_for_timeout(250)
            page.evaluate("setChip('books')")
            page.wait_for_function('booksState.loaded&&booksState.books.length===1')
            page.evaluate("navigator.serviceWorker.ready.then(()=>navigator.serviceWorker.controller ? true : new Promise(resolve=>navigator.serviceWorker.addEventListener('controllerchange',()=>resolve(true),{once:true})))")
            context.set_offline(True)
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("chip==='books' && booksState.offline")
            assert page.evaluate('booksState.books.length') == 1
            page.evaluate('(id)=>openReader(id)',bid);page.wait_for_function('rd.pack&&!rd.busy')
            assert page.locator('#rdFlow').inner_text().strip()
            page.evaluate('rdClose(true)')
            context.set_offline(False);page.evaluate('refresh()');page.wait_for_function('!booksState.offline')
        record('secure-context app shell reloads offline with live scripts cached',offline_reload)
        record('no unhandled browser JavaScript errors', lambda: None if not errors else (_ for _ in ()).throw(AssertionError(errors)))
        browser.close()
finally:
    server.shutdown(); core.DB.close()
    (OUT/'report.json').write_text(json.dumps({'results':results,'browser_errors':errors},ensure_ascii=False,indent=2))
    print('ARTIFACTS', OUT, flush=True)
