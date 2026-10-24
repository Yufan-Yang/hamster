"""Recycle-bin regressions, entirely inside an isolated temporary database/library."""
import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import core, library, migrations, pipeline, trash, web  # noqa: E402


def run(check):
    with tempfile.TemporaryDirectory(prefix='shiguang-trash-') as directory, contextlib.ExitStack() as stack:
        root = Path(directory).resolve()
        media = root / 'media'
        media.mkdir()
        for module, name, value in ((core, 'DB', None), (core, 'DB_PATH', root / 'test.db'),
                                   (core, 'STATE', root / 'state'), (core, 'MEDIA', media),
                                   (pipeline, 'MEDIA', media), (trash, 'MEDIA', media),
                                   (pipeline, 'INCOMPLETE', media / '.incomplete'),
                                   (library, 'PLAY_CACHE', media / '.cache' / 'play')):
            stack.enter_context(patch.object(module, name, value))
        stack.enter_context(patch.object(core, 'ring'))
        stack.enter_context(patch.object(library, 'plex_refresh'))
        previous_secret = core.app.secret_key
        core.app.secret_key = 'isolated-trash-test'
        stack.callback(setattr, core.app, 'secret_key', previous_secret)
        core.init_db()
        stack.callback(lambda: core.DB.close())

        def file(name):
            path = media / 'Videos' / 'Other' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test fixture, not a real video')
            return path

        def job(owner, name, paths=None, status='done'):
            paths = [str(path) for path in paths] if paths is not None else [str(file(name + '.mp4'))]
            url = 'https://example.test/watch/' + name
            core.q('INSERT INTO jobs (url,key,owner,title,status,files,analysis,created,updated) VALUES (?,?,?,?,?,?,?,?,?)',
                   (url, core.link_key(url), owner, name, status, json.dumps(paths), json.dumps({'title': name, 'library': 'Videos'}), time.time(), time.time()))
            return core.q('SELECT id FROM jobs WHERE url=? ORDER BY id DESC LIMIT 1', (url,), one=True)['id']

        def tid(jid):
            return core.q('SELECT id FROM job_trash WHERE job_id=?', (jid,), one=True)['id']

        def client(name, admin=False, char='a'):
            core.q('INSERT OR REPLACE INTO users (name,pw,admin,created) VALUES (?,?,?,?)', (name, '', int(admin), time.time()))
            device = char * 32
            core.q('INSERT OR REPLACE INTO devices (id,user,ip,seen) VALUES (?,?,?,?)', (device, name, '127.0.0.1', time.time()))
            result = core.app.test_client()
            result.set_cookie(web.DEVICE_COOKIE, device)
            with result.session_transaction() as session:
                session['user'] = name
            return result

        alice, bob, admin = client('alice'), client('bob', char='b'), client('admin', True, 'c')

        def soft_and_restore():
            jid = job('user:alice', 'restore-metadata')
            core.q('INSERT INTO watch (owner,job_id,pos,dur,done,updated) VALUES (?,?,?,?,?,?)', ('user:alice', jid, 30, 120, 0, time.time()))
            response = alice.post(f'/api/jobs/{jid}/delete', json={'files': True})
            assert response.status_code == 200 and response.json['result'] == 'trashed', response.json
            assert not core.q('SELECT 1 FROM jobs WHERE id=?', (jid,), one=True)
            assert (media / 'Videos/Other/restore-metadata.mp4').exists()
            entry = alice.get('/api/trash').json['items'][0]
            assert entry['job_id'] == jid and entry['expires_at'] - entry['deleted_at'] == 30 * 86400
            assert bob.get('/api/trash').json['items'] == []
            assert admin.get('/api/trash').json['items'] == []
            assert bob.post(f'/api/trash/{entry["id"]}/restore').status_code == 404
            assert bob.post(f'/api/trash/{entry["id"]}/delete').status_code == 404
            restored = alice.post(f'/api/trash/{entry["id"]}/restore')
            assert restored.status_code == 200 and restored.json['job_id'] == jid, restored.json
            row = core.q('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
            assert row['owner'] == 'user:alice' and row['status'] == 'done' and json.loads(row['analysis'])['title'] == 'restore-metadata'
            assert core.q('SELECT pos FROM watch WHERE owner=? AND job_id=?', ('user:alice', jid), one=True)['pos'] == 30
            assert alice.get('/api/trash').json['items'] == []
            assert alice.post('/api/jobs/remove', json={'ids': 'invalid'}).status_code == 400

        check('trash: deletion retains media, restores metadata/watch state, enforces owner isolation', soft_and_restore)

        def shared_files():
            video, subtitle = file('shared.mp4'), file('shared.zh.srt')
            a = job('user:alice', 'share-a', [video, subtitle])
            b = job('user:bob', 'share-b', [video])
            pipeline.remove_job(a)
            first = trash.purge(tid(a), 'user:alice')
            assert first['kept_shared'] and first['files'] == 0
            assert video.exists() and subtitle.exists()
            pipeline.remove_job(b)
            second = trash.purge(tid(b), 'user:bob')
            assert second['files'] == 2 and not video.exists() and not subtitle.exists()
            video = file('both-trashed.mp4')
            a = job('user:alice', 'both-a', [video])
            b = job('user:bob', 'both-b', [video])
            pipeline.remove_job(a)
            pipeline.remove_job(b)
            assert trash.purge(tid(a), 'user:alice')['kept_shared'] and video.exists()
            trash.purge(tid(b), 'user:bob')
            assert not video.exists()

        check('trash: active jobs and other owners recycle entries protect shared media and subtitles', shared_files)

        def retention():
            now = time.time()
            entries = []
            for name, age, orphan in (('expired', 30 * 86400 + 1, False), ('boundary', 30 * 86400, False),
                                       ('recent', 30 * 86400 - 1, False), ('legacy-protected', 90 * 86400, True)):
                jid = job('user:alice', name)
                pipeline.remove_job(jid)
                trash_id = tid(jid)
                core.q('UPDATE job_trash SET deleted_at=?,orphan=? WHERE id=?', (now - age, int(orphan), trash_id))
                entries.append(trash_id)
            assert trash.cleanup_expired(now=now) == 2
            remaining = {row['id'] for row in core.q('SELECT id FROM job_trash')}
            assert entries[0] not in remaining and entries[1] not in remaining
            assert entries[2] in remaining and entries[3] in remaining
            assert not (media / 'Videos/Other/expired.mp4').exists()
            assert (media / 'Videos/Other/legacy-protected.mp4').exists()

        check('trash: exact 30-day expiry and legacy scans never auto-delete unconfirmed files', retention)

        def safe_paths_and_failure():
            outside = root / 'outside.mp4'
            outside.write_bytes(b'outside')
            symlink = media / 'Videos/Other/link.mp4'
            symlink.symlink_to(outside)
            jid = job('user:alice', 'unsafe-paths', [outside, symlink, media])
            pipeline.remove_job(jid)
            trash.purge(tid(jid), 'user:alice')
            assert outside.exists() and symlink.exists() and media.exists()
            internal = file('unclaimed-directory/safe.mp4')
            internal_link = media / 'Videos/Other/internal-link'
            internal_link.symlink_to(internal.parent, target_is_directory=True)
            link_job = job('user:alice', 'directory-link', [internal_link])
            pipeline.remove_job(link_job)
            trash.purge(tid(link_job), 'user:alice')
            assert internal.exists() and not internal_link.exists()
            job('user:alice', 'register-unclaimed', [internal])
            nested = file('shared-directory/keep.mp4')
            a = job('user:alice', 'folder-owner', [nested.parent])
            job('user:bob', 'nested-owner', [nested])
            pipeline.remove_job(a)
            trash.purge(tid(a), 'user:alice')
            assert nested.exists()
            jid = job('user:alice', 'retry-failed-delete')
            pipeline.remove_job(jid)
            trash_id = tid(jid)
            with patch.object(pipeline, 'delete_job_files', side_effect=OSError('locked')):
                response = alice.post(f'/api/trash/{trash_id}/delete')
                assert response.status_code == 409
            assert core.q('SELECT 1 FROM job_trash WHERE id=?', (trash_id,), one=True)
            assert (media / 'Videos/Other/retry-failed-delete.mp4').exists()

        check('trash: shared directories, outside paths, symlinks and failed deletion preserve data', safe_paths_and_failure)

        def legacy_scan():
            legacy = file('old-entry-without-job.mp4')
            file('old-entry-without-job.zh.srt')
            private = media / '.notes/private.mp4'
            private.parent.mkdir()
            private.write_bytes(b'private')
            assert bob.post('/api/trash/scan').status_code == 403
            response = admin.post('/api/trash/scan')
            assert response.status_code == 200 and response.json['found'] == 1, response.json
            rows = admin.get('/api/trash').json
            assert rows['can_scan'] and len(rows['items']) == 1, rows
            entry = rows['items'][0]
            assert entry['orphan'] and entry['expires_at'] is None and entry['url'] == ''
            assert entry['title'] == 'old-entry-without-job'
            assert admin.post('/api/trash/scan').json['found'] == 0
            response = admin.post(f'/api/trash/{entry["id"]}/restore')
            assert response.status_code == 200, response.json
            row = core.q('SELECT * FROM jobs WHERE id=?', (response.json['job_id'],), one=True)
            assert row['owner'] == 'user:admin' and row['source'] == 'recovered' and row['status'] == 'done'
            assert str(legacy) in json.loads(row['files']) and legacy.exists() and private.exists()

        check('trash: admin-only orphan recovery excludes private, linked and already indexed files', legacy_scan)

        def migration_and_owner_transfer():
            jid = job('device-before-login', 'anonymous-restore')
            context = json.dumps({'media_urls': ['https://example.test/stream?id=9'], 'referer': 'https://example.test/lesson/9'})
            core.q('UPDATE jobs SET source=? WHERE id=?', ('crawl', jid))
            core.q('INSERT INTO crawl_downloads (job_id,context) VALUES (?,?)', (jid, context))
            pipeline.remove_job(jid)
            trash_id = tid(jid)
            trash.move_owner('device-before-login', 'user:alice')
            restored = trash.restore(trash_id, 'user:alice')
            assert core.q('SELECT owner FROM jobs WHERE id=?', (restored,), one=True)['owner'] == 'user:alice'
            assert core.q('SELECT source FROM jobs WHERE id=?', (restored,), one=True)['source'] == 'crawl'
            assert core.q('SELECT context FROM crawl_downloads WHERE job_id=?', (restored,), one=True)['context'] == context
            migrations.run(core.DB)
            assert core.DB.execute('PRAGMA user_version').fetchone()[0] >= 27
            assert core.q('SELECT 1 FROM jobs WHERE id=?', (restored,), one=True)

        check('trash: anonymous ownership migrates on login and schema upgrade is idempotent', migration_and_owner_transfer)

        def semantic_indexes():
            def index(jid, text):
                core.q("INSERT INTO seg (kind,ref,part,t,src,text) VALUES ('job',?,0,12,'subtitle',?)", (jid, text))
                core.q("INSERT INTO vec (kind,ref,part,t,src,base,v) VALUES ('job',?,0,12,?,0,?)",
                       (jid, text, b'\x00\x00\x80\x3f\x00\x00\x00\x00'))
                return (core.q('SELECT id FROM seg WHERE ref=? AND text=?', (jid, text), one=True)['id'],
                        core.q('SELECT id FROM vec WHERE ref=? AND src=?', (jid, text), one=True)['id'])

            jid = job('user:alice', 'semantic-restore')
            seg_id, vec_id = index(jid, 'A pendulum swings through its equilibrium position')
            core.q("INSERT INTO tasks (kind,target,state,payload) VALUES ('index',?,'queued','{}')", (f'job:{jid}:0',))
            pipeline.remove_job(jid)
            trash_id = tid(jid)
            assert core.q('SELECT ref FROM seg WHERE id=?', (seg_id,), one=True)['ref'] == jid
            assert bytes(core.q('SELECT v FROM vec WHERE id=?', (vec_id,), one=True)['v']) == b'\x00\x00\x80\x3f\x00\x00\x00\x00'
            assert not core.q('SELECT 1 FROM tasks WHERE target=?', (f'job:{jid}:0',), one=True)
            assert trash.restore(trash_id, 'user:alice') == jid
            assert core.q('SELECT text FROM seg WHERE id=?', (seg_id,), one=True)['text'].startswith('A pendulum')
            pipeline.remove_job(jid)
            trash_id = tid(jid)
            # Simulate an explicit legacy import reusing the original ID. Its
            # newly-created index must not be moved with the recycled entry.
            core.q("INSERT INTO jobs (id,url,key,owner,status,title,files) VALUES (?,?,?,'user:bob','done','replacement','[]')",
                   (jid, 'https://example.test/replacement', 'replacement'))
            replacement_seg, replacement_vec = index(jid, 'Unrelated replacement index')
            restored = trash.restore(trash_id, 'user:alice')
            assert restored != jid
            assert core.q('SELECT ref FROM seg WHERE id=?', (seg_id,), one=True)['ref'] == restored
            restored_vec = core.q('SELECT id,v FROM vec WHERE ref=?', (restored,), one=True)
            assert restored_vec and restored_vec['id'] > replacement_vec
            assert bytes(restored_vec['v']) == b'\x00\x00\x80\x3f\x00\x00\x00\x00'
            assert not core.q('SELECT 1 FROM vec WHERE id=?', (vec_id,), one=True)
            assert core.q('SELECT ref FROM seg WHERE id=?', (replacement_seg,), one=True)['ref'] == jid
            assert core.q('SELECT ref FROM vec WHERE id=?', (replacement_vec,), one=True)['ref'] == jid
            pipeline.remove_job(restored)
            trash.purge(tid(restored), 'user:alice')
            assert not core.q('SELECT 1 FROM seg WHERE id=?', (seg_id,), one=True)
            assert not core.q('SELECT 1 FROM vec WHERE id=?', (restored_vec['id'],), one=True)
            assert core.q('SELECT 1 FROM seg WHERE id=?', (replacement_seg,), one=True)
            assert core.q('SELECT 1 FROM vec WHERE id=?', (replacement_vec,), one=True)

        check('trash: semantic text/image indexes survive restore, move safely on ID collision and purge permanently', semantic_indexes)


if __name__ == '__main__':
    def check(name, fn):
        fn()
        print('ok   ', name)
    run(check)
