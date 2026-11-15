#!/usr/bin/env python3
"""Real player, HTTP, task leases and synthetic media; no production/model service."""
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

ROOT=Path(__file__).resolve().parents[1]
OUT=Path(tempfile.mkdtemp(prefix='shiguang-playback-agents-',dir=os.environ.get('QA_TMPDIR')))
os.environ.update(DB_PATH=str(OUT/'test.db'),STATE_DIR=str(OUT/'state'),MEDIA_ROOT=str(OUT/'media'),
                  LLM_API_KEY='',GRABBER_PASSWORD='',COMPUTE_TOKEN='playback-qa-only',TELEGRAM_BOT_TOKEN='')
sys.path.insert(0,str(ROOT))
from shiguang import board,core,llm,media_quality,resume_recap,steward,web  # noqa:E402

core.MEDIA.mkdir(parents=True);core.init_db();web.setup_app()
logging.getLogger('werkzeug').setLevel(logging.ERROR)
server=make_server('127.0.0.1',0,core.app,threaded=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
URL=f'http://127.0.0.1:{server.server_port}'
stop=threading.Event();errors=[];results=[];calls=[]
template=core.MEDIA/'template.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=160x90:r=1','-f','lavfi','-i','anullsrc=r=16000:cl=mono',
                '-t','400','-c:v','libx264','-preset','ultrafast','-c:a','aac','-y',str(template)],check=True,timeout=90)


def seed(owner,title,private=False):
    now=time.time();analysis={'title':title,'library':'Videos','folder':'Tutorials','needs_transcript':True,
        'summary':'FUTURE_FULL_SUMMARY','key_points':['Readable untimed point'],'point_times':[999],
        'chapters':{'0':[{'t':0,'title':'Start'},{'t':999,'title':'Beyond duration'}]}}
    core.q("INSERT INTO jobs(owner,url,title,status,analysis,created,updated) VALUES(?,?,?,'done',?,?,?)",
           (owner,'https://example.test/'+str(time.time_ns()),title,json.dumps(analysis),now,now))
    jid=core.q('SELECT max(id) FROM jobs',one=True)[0]
    path=core.MEDIA/f'qa-{jid}.mp4';shutil.copyfile(template,path)
    core.q('UPDATE jobs SET files=? WHERE id=?',(json.dumps([str(path)]),jid))
    core.q('INSERT INTO probes(path,mtime,duration,vcodec) VALUES(?,?,400,?)',(str(path),path.stat().st_mtime,'h264'))
    def stamp(sec):return f'00:{sec//60:02}:{sec%60:02},000'
    path.with_suffix('.zh.srt').write_text('\n\n'.join(f'{i}\n{stamp(i*10)} --> {stamp(i*10+9)}\n'
        +(f'PRIVATE_SOURCE useful explanation number {i}.' if private else f'Lighting source information number {i} with a complete explanation.' if i<18 else f'AFTER_CHECKPOINT secret material number {i}.') for i in range(39)))
    core.q('INSERT INTO watch(owner,job_id,part,pos,dur,done,updated) VALUES(?,?,0,180,400,0,?)',(owner,jid,now-4000))
    return jid


def model(system,user,*args,**kwargs):
    data=json.loads(user);assert 'FUTURE_FULL_SUMMARY' not in user and 'AFTER_CHECKPOINT' not in user
    assert all(x['end']<=data['checkpoint'] for x in data['excerpts'])
    calls.append(data['checkpoint']);item=data['excerpts'][0]
    return {'quotes':[{'source_id':item['id'],'quote':item['text'][:150]}]}


def worker():
    while not stop.wait(.05):
        try:
            task=board.claim_task('playback-browser',['resume_recap','media_quality_scan'])
            if task:board.run_claimed(task,'playback-browser')
        except Exception as error:errors.append(type(error).__name__)


def record(name,fn):
    started=time.time();fn();results.append({'name':name,'passed':True,'seconds':round(time.time()-started,2)});print('PASS',name,flush=True)


try:
    with patch.object(llm,'llm_json',side_effect=model),sync_playwright() as engine:
        threading.Thread(target=worker,daemon=True).start()
        executable=os.environ.get('CHROMIUM') or shutil.which('chromium')
        browser=engine.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-gpu'])
        try:
            context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            context.route('**/*',lambda route:route.continue_() if urlsplit(route.request.url).hostname=='127.0.0.1' else route.abort())
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(URL,wait_until='networkidle');page.wait_for_function("storageOwner !== 'unidentified'")
            page.evaluate("openAccount();renderAuth('register')")
            page.fill('#username','playback-qa');page.fill('#password','disposable-pass');page.fill('#password2','disposable-pass')
            page.click('#authBtn');page.wait_for_function("state.user==='playback-qa'");page.evaluate("hide($('#accountSheet'))")
            owner=page.evaluate('storageOwner');public=seed(owner,'Playback source');private=seed(owner,'PRIVATE_TITLE',True)
            core.q('INSERT OR REPLACE INTO privacy(owner,tags,ids) VALUES(?,?,?)',(owner,'[]',json.dumps([private])))
            core.kv_set(steward.POLICY_PREFIX+owner,{**steward.DEFAULT_POLICY,'automatic':True})
            before=core.q('SELECT analysis FROM jobs WHERE id=?',(public,),one=True)[0]

            def background():
                resume_recap.tick();media_quality.tick();deadline=time.time()+15
                while time.time()<deadline:
                    if core.q("SELECT count(*) FROM resume_recaps WHERE state='ready'",one=True)[0]==2 and core.q('SELECT count(*) FROM media_quality',one=True)[0]==2:break
                    time.sleep(.1)
                else:raise AssertionError('background recaps incomplete')
                assert len(calls)==2
                page.evaluate(f'openWatch({public})')
                try:
                    page.wait_for_function("recapView?.state==='ready'",timeout=20000)
                except Exception:
                    print('recap debug',page.evaluate('({recap:recapView,watching,watch:state.jobs.find(j=>j.id==='+str(public)+')?.watch,html:$("#resumeRecap")?.innerHTML})'),flush=True)
                    raise
                text=page.locator('#resumeRecap').inner_text()
                assert 'Lighting source' in text and 'AFTER_CHECKPOINT' not in text and 'PRIVATE_SOURCE' not in text
                assert len(calls)==2
            record('autonomous preparation and cached personal recall on actual resume',background)

            def quality_and_rewind():
                page.wait_for_selector('#chapters .timeline-unavailable')
                assert page.locator('#chapters .timeline-unavailable').inner_text()=='Beyond duration · 无可用跳转'
                assert not page.locator('#chapters button[onclick*="999"]').count()
                assert core.q('SELECT analysis FROM jobs WHERE id=?',(public,),one=True)[0]==before
                page.evaluate(f'seekTo({public},0,999)')
                assert page.evaluate("$('#watchBody video').currentTime")<400
                page.locator('#resumeRecap').get_by_role('button',name='0:00 回看',exact=True).click()
                page.wait_for_function("$('#watchBody video').currentTime<15")
                assert page.locator('#resumeRecap').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            record('verified timeline preserves invalid text, blocks jumps and enables quoted rewind',quality_and_rewind)

            def revoke():
                hidden=core.q('SELECT id FROM resume_recaps WHERE job_id=?',(private,),one=True)[0]
                assert context.request.get(URL+f'/api/recap/{private}?id={hidden}').status==404
                page.evaluate(f'clearWatch({public})')
                page.wait_for_function("recapView===null")
                assert page.locator('#resumeRecap').inner_html()==''
                assert not core.q('SELECT id FROM resume_recaps WHERE job_id=?',(public,),one=True)
                page.evaluate('resetPrivateViews()')
                assert page.locator('#resumeRecap').inner_html()==''
            record('private artifacts are inaccessible and clearing progress removes the displayed recall',revoke)
            assert not errors,errors
        finally:browser.close()
finally:
    stop.set();server.shutdown()
    report={'results':results,'page_errors':errors,'model_calls':len(calls),'passed':len(results)==3 and not errors}
    (OUT/'report.json').write_text(json.dumps(report,indent=2));print('Browser report:',OUT,flush=True)
