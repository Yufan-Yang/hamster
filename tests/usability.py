"""Regressions for the QA findings; all writes use smoke.py's disposable database."""
import json
import time
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from shiguang import board, core, library, live, live_bilibili, live_calibration, search, web


def run(check):
    def boundaries():
        client = core.app.test_client()
        owner = client.get('/api/jobs?limit=1').get_json()['storage_owner']
        now = time.time()
        core.q("INSERT INTO jobs(owner,url,title,status,analysis) VALUES (?,?,?,'done','{}')", (owner, 'https://qa.invalid/short', '短片'))
        jid = core.q('SELECT MAX(id) id FROM jobs', one=True)['id']
        core.q("INSERT INTO books(owner,title,status,fmt) VALUES (?,'QA','ready','txt')", (owner,))
        bid = core.q('SELECT MAX(id) id FROM books', one=True)['id']
        try:
            for body in ({'pos': 'NaN'}, {'pos': -1}, {'dur': -1}, {'part': .2}, {'secs': 'abc'}, {'pos': 22, 'dur': 20}, {'ended': 'false'}):
                assert client.post(f'/api/watch/{jid}', json=body).status_code == 400, body
            for body in ({'secs': 'abc'}, {'secs': {}}, {'at': 'NaN'}, {'pct': 2}, {'pos': {'ch': -1}}, {'pos': {'f': 'Infinity'}}, {'pos': 'bad'}):
                assert client.post(f'/api/books/{bid}/progress', json=body).status_code == 400, body
            assert not core.q('SELECT 1 FROM book_read WHERE book=?', (bid,), one=True)
            for position, done in ((5, False), (18.9, False), (19, True)):
                response = client.post(f'/api/watch/{jid}', json={'pos': position, 'dur': 20, 'secs': 5})
                assert response.status_code == 200 and response.get_json()['done'] is done
            assert client.post(f'/api/books/{bid}/progress', json={'pct': .2, 'secs': 999, 'at': now, 'pos': {'ch': 0, 'f': .4}}).status_code == 200
            assert core.q('SELECT seconds FROM book_read WHERE book=?', (bid,), one=True)['seconds'] == 300
            assert client.post(f'/api/books/{bid}/marks', json={'pos': {'page': .5}, 'pct': .5}).status_code == 400
            assert client.post(f'/api/watch/{jid}', json={'pos': 1, 'dur': 20}, headers={'X-Storage-Owner': 'user:another'}).status_code == 409
            assert client.post(f'/api/books/{bid}/progress', json={'pct': .3, 'storage_owner': 'user:another'}).status_code == 409
        finally:
            core.q('DELETE FROM watch WHERE job_id=?', (jid,))
            core.q('DELETE FROM jobs WHERE id=?', (jid,))
            for table, key in (('book_read','book'), ('book_marks','book'), ('books','id')):
                core.q(f'DELETE FROM {table} WHERE {key}=?', (bid,))
    check('QA: finite progress, short-video completion, stale-account writes', boundaries)

    def search_modes():
        client = core.app.test_client()
        owner = client.get('/api/jobs?limit=1').get_json()['storage_owner']
        core.q("INSERT INTO jobs(owner,url,title,status,analysis) VALUES (?,?,?,'done',?)", (owner, 'https://qa.invalid/search', 'QA检索词', json.dumps({'title': 'QA检索词', 'summary': '详情完整保留', 'key_points': ['详情']})))
        jid = core.q('SELECT MAX(id) id FROM jobs', one=True)['id']
        try:
            with patch.object(search, 'visual_hits', return_value={}) as visual:
                data = client.get('/api/jobs?q=QA检索词&compact=1').get_json()
                assert not visual.called
                item = next(j for j in data['jobs'] if j['id'] == jid)
                assert item['compact'] and 'summary' not in item['analysis']
                data = client.get(f'/api/jobs?only=1&ids={jid}').get_json()
                assert [j['id'] for j in data['jobs']] == [jid]
                assert data['jobs'][0]['analysis']['summary'] == '详情完整保留'
                client.get('/api/jobs?q=QA检索词&visual=1')
                assert visual.called
        finally:
            core.q('DELETE FROM jobs WHERE id=?', (jid,))
    check('QA: fast text search, explicit visual search, compact list with full detail', search_modes)

    def database_cards():
        client = core.app.test_client()
        owner = client.get('/api/jobs?metadata=1').get_json()['storage_owner']
        path = '/qa-database-card/video.mp4'
        core.q("INSERT INTO jobs(owner,url,title,status,files,analysis) VALUES (?,?,?,'done',?,'{}')",
               (owner, 'https://qa.invalid/database-card', '数据库卡片', json.dumps([path])))
        jid = core.q('SELECT MAX(id) id FROM jobs', one=True)['id']
        core.q('INSERT OR REPLACE INTO probes(path,mtime,duration,vcodec) VALUES (?,1,123,?)', (path, 'h264'))
        try:
            with patch.object(library, 'playable', side_effect=AssertionError('card scanned media folders')), \
                 patch.object(library, 'probe_of', side_effect=AssertionError('card inspected a video file')):
                data = client.get('/api/jobs?compact=1&grouped=1').get_json()
                item = next(j for j in data['jobs'] if j['id'] == jid)
                assert item['media'][0]['duration'] == 123 and item['media'][0]['vcodec'] == 'h264'
                assert 'src' not in item['media'][0]
                assert client.get('/api/jobs?compact=1&metadata=1').get_json()['jobs'] == []
        finally:
            core.q('DELETE FROM jobs WHERE id=?', (jid,))
            core.q('DELETE FROM probes WHERE path=?', (path,))
    check('QA: database-only video cards and lightweight workspace bootstrap', database_cards)

    def calibration_controls():
        name = 'qa-usability'
        core.q('INSERT OR IGNORE INTO users(name,pw,created) VALUES (?,?,?)', (name, '', time.time()))
        device = 'e' * 32
        core.q('INSERT OR REPLACE INTO devices(id,user) VALUES (?,?)', (device, name))
        client = core.app.test_client(); client.set_cookie(web.DEVICE_COOKIE, device)
        with client.session_transaction() as session:
            session['user'] = name
        owner = 'user:' + name
        core.q("INSERT INTO live_sources(owner,room_id,uid,url,name,settings) VALUES (?,991122,123,'https://live.bilibili.com/991122','原主播',?)", (owner, json.dumps(live.settings())))
        source = core.q('SELECT * FROM live_sources WHERE owner=?', (owner,), one=True)
        sid = source['id']
        origin = json.dumps({'name': '原主播', 'room_id': 991122, 'uid': 123, 'url': 'https://live.bilibili.com/991122'})
        core.q('INSERT INTO live_sessions(key,source_id,owner,title,origin) VALUES (?,?,?,?,?)', ('qa-origin', sid, owner, '原场次', origin))
        core.q("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state) VALUES (?,?,'qa-origin','topic',0,30,'片段','ready')", (owner, sid))
        cid = core.q('SELECT MAX(id) id FROM live_clips WHERE owner=?', (owner,), one=True)['id']
        sample = {'bvid': 'BV1234567890', 'title': '参考切片', 'author': '切片者', 'published': time.time()-86400*2, 'duration': 60, 'views': 2000}
        try:
            explicit_source = dict(source)
            explicit_source['settings'] = json.dumps({**live.settings(), 'reference_bvids': sample['bvid'], 'reference_mids': '456'})
            session = Mock(); session.get.return_value.json.return_value = {'code': -412}
            direct = {'title': sample['title'], 'owner': {'mid': 456, 'name': sample['author']}, 'pubdate': sample['published'],
                      'duration': 60, 'stat': {'view': 2000}}
            with patch.object(live_calibration.channels, 'bili_session', return_value=session), patch.object(live_calibration.channels, 'bili_signed', return_value={}), \
                 patch.object(live_calibration.channels, 'list_bilibili', return_value={'entries':[{'url':'https://www.bilibili.com/video/'+sample['bvid']}]}), \
                 patch.object(live_bilibili, 'account_path', return_value=Path('/tmp/nonexistent-qa-account')), patch.object(live_bilibili, 'client'), \
                 patch.object(live_bilibili, 'api', return_value=direct), patch.object(live_calibration.llm, 'llm_json', return_value={'accepted_bvids':[sample['bvid']]}):
                found = live_calibration.discover(explicit_source)
                assert len(found) == 1 and core.kv_get(f'live-search-audit:{sid}')['warning']
            with patch.object(live_calibration, 'discover', return_value=[sample]), patch.object(live_bilibili, 'api', return_value={'stat': {'view': 2000}}):
                result = live_calibration.collect({'payload': {'source': sid}}, lambda _: True)
                assert result['references'] == 1
            ref = core.q('SELECT * FROM live_references WHERE source_id=?', (sid,), one=True)
            stranger = core.app.test_client()
            assert stranger.post(f'/api/live/references/{ref["id"]}/exclude').status_code == 401
            assert client.post(f'/api/live/references/{ref["id"]}/exclude').status_code == 200
            assert core.q('SELECT excluded FROM live_references WHERE id=?', (ref['id'],), one=True)['excluded'] == 1
            tid = board.publish('live_refine', f'qa-refine:{sid}', payload={'source': sid, 'title': '校准'})
            core.q("UPDATE tasks SET state='failed',error='临时错误' WHERE id=?", (tid,))
            assert stranger.post(f'/api/tasks/{tid}/retry').status_code == 404
            assert client.post(f'/api/tasks/{tid}/retry').status_code == 200
            assert client.post(f'/api/tasks/{tid}/retry').status_code == 409
            with patch.object(live_bilibili, 'room_info', return_value={'room_id': 991123, 'uid': 124, 'url': 'https://live.bilibili.com/991123', 'name': '另一主播'}):
                assert client.post(f'/api/live/sources/{sid}', json={'url': 'https://live.bilibili.com/991123'}).status_code == 409
            render_id = board.publish('live_render', f'live-clip:{cid}:v1', payload={'clip':cid,'version':1})
            core.q("UPDATE tasks SET state='running',worker='qa-render' WHERE id=?", (render_id,))
            core.q("UPDATE live_clips SET state='rendering' WHERE id=?", (cid,))
            board.fail_task(render_id, 'qa-render', '制作失败', retry=False, expected=True)
            assert core.q('SELECT state FROM live_clips WHERE id=?', (cid,), one=True)['state'] == 'failed'
            with patch.object(live, 'queue_render') as retry_render:
                assert client.post(f'/api/tasks/{render_id}/retry').status_code == 200
                retry_render.assert_called_once_with(cid)
            upload_id = board.publish('live_upload', f'qa-upload:{cid}', payload={'clip':cid,'version':1})
            core.q("UPDATE tasks SET state='failed' WHERE id=?", (upload_id,))
            assert client.post(f'/api/tasks/{upload_id}/retry').status_code == 404
            core.q("UPDATE live_sources SET name='编辑后的别名' WHERE id=?", (sid,))
            clip = next(c for c in client.get('/api/live').get_json()['clips'] if c['id'] == cid)
            assert '原主播：原主播' in clip['publication_description'] and clip['source_name'] == '原主播'
            assert '991122' in clip['publication_description']
        finally:
            core.q('DELETE FROM tasks WHERE json_extract(payload,\'$.source\')=? OR json_extract(payload,\'$.reference\')=? OR json_extract(payload,\'$.clip\')=?', (sid, ref['id'], cid))
            for table in ('live_references', 'live_clips', 'live_sessions', 'live_sources'):
                core.q(f'DELETE FROM {table} WHERE owner=?', (owner,))
            core.q('DELETE FROM devices WHERE id=?', (device,));core.q('DELETE FROM users WHERE name=?', (name,))
    check('QA: sample accumulation, exclusion, authorized retries and immutable attribution', calibration_controls)
