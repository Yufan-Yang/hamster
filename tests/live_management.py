"""Unfollowing preserves clips; bulk changes respect ownership and publishing."""
import json
import secrets
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from shiguang import core, live, live_bilibili


def run(check):
    def management():
        user = 'live-management-test'
        owner = 'user:' + user
        device = secrets.token_hex(16)
        core._write('INSERT INTO users(name,pw,created) VALUES (?,?,?)', (user, '', time.time()))
        core._write('INSERT INTO devices(id,user) VALUES (?,?)', (device, user))
        client = core.app.test_client();client.set_cookie('grabber_device', device)
        with client.session_transaction() as session:
            session['user'] = user
        core._write("INSERT INTO live_sources(owner,room_id,url,name,settings,enabled) VALUES (?,91919,'https://live.bilibili.com/91919','管理测试','{}',1)", (owner,))
        source = core.q('SELECT * FROM live_sources WHERE owner=?', (owner,), one=True)
        sid = source['id']
        ids = []
        def add(state='ready', item_owner=owner):
            core._write("INSERT INTO live_clips(owner,source_id,session_key,lane,start,end,title,state) VALUES (?,?,'manage-test','topic',0,60,'测试片段',?)", (item_owner, sid, state))
            cid = core.q('SELECT MAX(id) id FROM live_clips', one=True)['id'];ids.append(cid);return cid
        try:
            draft = add();foreign = add(item_owner='user:someone-else')
            protected = [add(state) for state in ('submitted','uploading','uncertain','awaiting_upload')]
            busy = add()
            core._write("INSERT INTO tasks(kind,target,state,payload) VALUES ('live_render','manage-test','running',?)", (json.dumps({'clip':busy}),))
            response = client.post(f'/api/live/sources/{sid}/delete', json={})
            assert response.status_code == 200
            row = core.q('SELECT * FROM live_sources WHERE id=?', (sid,), one=True)
            assert row['archived'] == 1 and row['enabled'] == 0
            assert core.q('SELECT id FROM live_clips WHERE id=?', (draft,), one=True)
            with patch.object(live_bilibili, 'room_info', return_value=dict(source)):
                assert client.post('/api/live/sources', json={'url':source['url']}).status_code == 200
            row = core.q('SELECT * FROM live_sources WHERE id=?', (sid,), one=True)
            assert row['archived'] == 0 and row['enabled'] == 1
            with tempfile.TemporaryDirectory() as directory, patch.object(live, 'ROOT', Path(directory)):
                assets = Path(directory)/'clips';assets.mkdir()
                video = assets/f'{draft}-v1.mp4';video.write_bytes(b'video')
                cover = assets/f'{draft}-v1-cover.jpg';cover.write_bytes(b'cover')
                kept = assets/f'{protected[0]}-v1.mp4';kept.write_bytes(b'posted')
                response = client.post('/api/live/clips/batch', json={'action':'reject','ids':[draft,*protected]})
                assert response.status_code == 200
                assert response.json['changed'] == [draft] and len(response.json['skipped']) == 4
                assert video.exists() and cover.exists()
                assert core.q('SELECT state FROM live_clips WHERE id=?', (draft,), one=True)['state'] == 'rejected'
                assert client.post('/api/live/clips/batch', json={'action':'delete','ids':[draft,foreign]}).status_code == 404
                assert video.exists()
                response = client.post('/api/live/clips/batch', json={'action':'delete','ids':[draft,busy,*protected]})
                assert response.status_code == 200 and response.json['changed'] == [draft]
                assert len(response.json['skipped']) == 5
                assert not video.exists() and not cover.exists() and kept.exists()
                assert core.q('SELECT state FROM live_clips WHERE id=?', (protected[0],), one=True)['state'] == 'submitted'
                parent = add();child = add(state='submitted')
                core.q('UPDATE live_clips SET merged_into=? WHERE id=?', (parent,child))
                parent_file=assets/f'{parent}-v1.mp4';parent_file.write_bytes(b'draft')
                child_file=assets/f'{child}-v1.mp4';child_file.write_bytes(b'publication')
                core.q('UPDATE live_clips SET file=? WHERE id=?', (str(parent_file),parent))
                response=client.post('/api/live/clips/batch',json={'action':'delete','ids':[parent]})
                assert response.status_code==200 and response.json['changed']==[parent] and not response.json['skipped']
                removed=core.q('SELECT * FROM live_clips WHERE id=?',(parent,),one=True)
                assert removed['removed_at'] and removed['state']=='rejected' and removed['file']==''
                assert not parent_file.exists() and child_file.exists()
                assert core.q('SELECT merged_into FROM live_clips WHERE id=?',(child,),one=True)['merged_into']==parent
                assert parent not in [c['id'] for c in client.get('/api/live?tab=topic').json['clips']]
                assert client.get(f'/api/live/clips/{parent}/media').status_code==404
                assert client.post(f'/api/live/clips/{parent}/render',json={}).status_code==404
            assert client.post('/api/live/clips/batch', json={'action':'delete','ids':[True]}).status_code == 400
        finally:
            core._write("DELETE FROM tasks WHERE target='manage-test'")
            for cid in ids:core._write('DELETE FROM live_clips WHERE id=?', (cid,))
            core._write('DELETE FROM live_sources WHERE id=?', (sid,))
            core._write('DELETE FROM devices WHERE id=?', (device,))
            core._write('DELETE FROM users WHERE name=?', (user,))
    check('live management: unfollow and restore, bulk ownership, ignore keeps files, delete protects publications', management)
