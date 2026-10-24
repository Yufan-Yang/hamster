"""Unified sources and durable discovery inbox with isolated synthetic content."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import channels, core, crawl, migrations, sources, web  # noqa: E402


class SourceTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    item = fixtures.HomeSearchTests.item
    job = fixtures.HomeSearchTests.job

    def test_already_watched_discoveries_do_not_remind_and_new_completion_marks_read(self):
        sid = self.subscription()
        old = self.job('user:alice', 'Already watched')
        fresh = self.job('user:alice', 'New episode')
        for jid in (old, fresh):
            core.q('UPDATE jobs SET created=?,source=? WHERE id=?', (time.time() + 1, f'sub:{sid}', jid))
            sources.record_sub(sid, jid, {'url': core.q('SELECT url FROM jobs WHERE id=?', (jid,), one=True)['url']})
        core.q('INSERT INTO watch(owner,job_id,part,pos,dur,done,updated) VALUES(?,?,0,100,100,1,?)', ('user:alice', old, time.time()))
        self.assertEqual([item['job_id'] for item in self.inbox()['items']], [fresh])
        response = self.alice.post(f'/api/watch/{fresh}', json={'pos':100, 'dur':100, 'part':0, 'ended':True, 'secs':0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.inbox()['total'], 0)
        self.assertEqual({item['state'] for item in self.inbox(state='all')['items']}, {'read'})

    def discover(self, cid=None, title='New lesson', old=False, url=None):
        cid = self.a if cid is None else cid
        source = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
        iid = self.item(source['owner'], title, [cid])
        core.q('UPDATE crawl_items SET created=? WHERE id=?', (1 if old else time.time() + 1, iid))
        if url:
            core.q('UPDATE crawl_items SET url=? WHERE id=?', (url, iid))
        with crawl.transaction() as db:
            sources.record_crawl(db, cid, iid)
        return iid

    def incremental(self, cid=None):
        cid = cid or self.a
        row = core.q('SELECT url_key,generation FROM crawls WHERE id=?', (cid,), one=True)
        core.q('INSERT OR IGNORE INTO source_inbox_baselines(source_id,source_key,generation) VALUES(?,?,?)',
               (cid, row['url_key'], row['generation']))
        core.q('UPDATE crawls SET generation=generation+1 WHERE id=?', (cid,))

    def inbox(self, client=None, **query):
        response = (client or self.alice).get('/api/inbox', query_string=query)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def subscription(self, owner='user:alice', key='youtube:lessons', platform='youtube'):
        core.q('INSERT INTO subs(owner,platform,key,url,name,created,checked,seen) VALUES(?,?,?,?,?,?,?,?)',
               (owner, platform, key, 'https://example.test/channel/' + key, 'Channel lessons', time.time(), time.time(), '[]'))
        return core.q('SELECT MAX(id) id FROM subs', one=True)['id']

    def test_initial_scan_and_preupgrade_history_are_read_then_incremental_is_unread(self):
        self.discover(title='Initial scan')
        self.assertEqual(self.inbox()['total'], 0)
        self.incremental()
        self.discover(title='Existing before upgrade', old=True)
        iid = self.discover(title='Incremental discovery')
        data = self.inbox()
        self.assertEqual((data['total'], data['unread']), (1, 1))
        self.assertEqual(data['items'][0]['title'], 'Incremental discovery')
        self.assertTrue(data['items'][0]['can_download'])
        with crawl.transaction() as db:
            sources.record_crawl(db, self.a, iid)
        self.assertEqual(self.inbox()['total'], 1)
        self.assertEqual(self.inbox(state='read')['total'], 2)

    def test_cross_crawl_and_channel_dedup_preserves_all_sources(self):
        self.incremental(); self.incremental(self.a2)
        url = 'https://www.youtube.com/watch?v=abcdefghijk'
        iid = self.discover(url=url)
        core.q('INSERT INTO crawl_results(crawl_id,item_id) VALUES(?,?)', (self.a2, iid))
        with crawl.transaction() as db:
            sources.record_crawl(db, self.a2, iid)
        sid = self.subscription()
        jid = self.job('user:alice', 'Shared channel lesson')
        core.q('UPDATE jobs SET url=?,key=?,created=?,source=? WHERE id=?', (url, core.link_key(url), time.time() + 1, f'sub:{sid}', jid))
        sources.record_sub(sid, jid, {'url': url}, baseline=False)
        data = self.inbox()
        self.assertEqual(data['total'], 1)
        self.assertEqual({s['key'] for s in data['items'][0]['sources']}, {f'crawl:{self.a}', f'crawl:{self.a2}', f'sub:{sid}'})
        self.assertEqual(data['items'][0]['job_id'], jid)
        center = self.alice.get('/api/sources').json
        self.assertEqual(center['unread'], 1)
        self.assertEqual({s['type'] for s in center['items']}, {'crawl', 'sub'})

    def test_generic_query_ids_and_http_https_remain_distinct(self):
        self.incremental()
        urls = ['https://example.test/watch?ref=one', 'https://example.test/watch?ref=two',
                'https://example.test/watch?mid=one', 'https://example.test/watch?mid=two',
                'http://example.test/scheme', 'https://example.test/scheme']
        for url in urls:
            self.discover(url=url)
        self.assertEqual(self.inbox()['total'], len(urls))
        self.assertEqual({item['url'] for item in self.inbox()['items']}, set(urls))

    def test_independent_general_source_is_visible_without_exposing_adult_provenance(self):
        secret = self.collection('user:alice', 'private-root', 'adult')
        self.incremental(secret); self.incremental()
        iid = self.discover(secret, title='Common content')
        core.q('INSERT INTO crawl_results(crawl_id,item_id) VALUES(?,?)', (self.a, iid))
        with crawl.transaction() as db:
            sources.record_crawl(db, self.a, iid)
        data = self.inbox()
        self.assertEqual(data['total'], 1)
        self.assertEqual([s['key'] for s in data['items'][0]['sources']], [f'crawl:{self.a}'])
        self.assertNotIn('private-root', str(data))

    def test_read_ignore_and_unread_do_not_change_watch_or_start_downloads(self):
        self.incremental(); self.discover()
        ident = self.inbox()['items'][0]['id']
        with patch.object(crawl.pipeline, 'add_job_ex') as download:
            for action, state in (('read', 'read'), ('ignore', 'ignored'), ('unread', 'unread')):
                result = self.alice.post('/api/inbox/action', json={'action': action, 'ids': [ident]})
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(self.inbox(state=state)['items'][0]['id'], ident)
            download.assert_not_called()
        self.assertEqual(core.q('SELECT COUNT(*) n FROM watch', one=True)['n'], 0)

    def test_hidden_sources_and_manual_jobs_are_filtered_before_counts_and_actions(self):
        self.incremental(); self.discover(title='Visible lesson')
        secret = self.collection('user:alice', 'adult', 'adult')
        self.incremental(secret); self.discover(secret, title='Secret title')
        manual = self.discover(title='Manual hidden title')
        jid = self.job('user:alice', 'Hidden imported job')
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (jid, manual))
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([jid])))
        locked = self.inbox()
        self.assertEqual((locked['total'], locked['unread']), (1, 1))
        self.assertNotIn('Secret title', str(locked)); self.assertNotIn('Manual hidden title', str(locked))
        ids = [r['id'] for r in core.q('SELECT id FROM source_inbox WHERE owner=?', ('user:alice',))]
        for endpoint, body in (('/api/inbox/action', {'action': 'read', 'ids': ids}), ('/api/inbox/import', {'ids': ids})):
            self.assertEqual(self.alice.post(endpoint, json=body).status_code, 400)
        self.assertEqual(self.inbox()['total'], 1)
        token = {'owner': 'user:alice', 'reveal': True, 'expires': time.time() + 60}
        with patch.object(web, 'privacy_tokens', {'show': token}):
            shown = self.alice.get('/api/inbox', headers={'X-Privacy-Token': 'show'}).json
            self.assertEqual(shown['total'], 3)
        center = self.alice.get('/api/sources').json
        self.assertNotIn(f'crawl:{secret}', {s['key'] for s in center['items']})

    def test_owner_admin_and_invalid_ids_cannot_mutate_others(self):
        self.incremental(); self.discover()
        ident = self.inbox()['items'][0]['id']
        for client in (self.bob, self.admin):
            self.assertEqual(self.inbox(client)['total'], 0)
            self.assertEqual(client.post('/api/inbox/action', json={'action': 'read', 'ids': [ident]}).status_code, 400)
            self.assertEqual(client.post('/api/inbox/import', json={'ids': [ident]}).status_code, 400)
        self.assertEqual(self.inbox()['total'], 1)
        for ids in ([], [True], [0], [ident, 999999], list(range(1, 102))):
            self.assertEqual(self.alice.post('/api/inbox/action', json={'action': 'read', 'ids': ids}).status_code, 400)

    def test_deleted_or_replaced_source_disappears_and_one_surviving_source_remains(self):
        self.incremental(); self.incremental(self.a2)
        iid = self.discover()
        core.q('INSERT INTO crawl_results(crawl_id,item_id) VALUES(?,?)', (self.a2, iid))
        with crawl.transaction() as db:
            sources.record_crawl(db, self.a2, iid)
        self.alice.post(f'/api/crawls/{self.a}/delete', json={})
        self.assertEqual(self.inbox()['items'][0]['sources'][0]['key'], f'crawl:{self.a2}')
        core.q("UPDATE crawls SET url_key='different root' WHERE id=?", (self.a2,))
        self.assertEqual(self.inbox()['total'], 0)

    def test_merge_keeps_provenance_and_does_not_resurrect_ignored_content(self):
        self.incremental(); self.incremental(self.a2)
        first, other = self.discover(title='First'), self.discover(self.a2, title='Duplicate')
        entries = self.inbox()['items']
        self.alice.post('/api/inbox/action', json={'action': 'ignore', 'ids': [entries[0]['id']]})
        with crawl.transaction() as db:
            crawl._merge(db, first, other)
        self.assertEqual(self.inbox()['total'], 0)
        ignored = self.inbox(state='ignored')
        self.assertEqual(ignored['total'], 1)
        self.assertEqual(len(ignored['items'][0]['sources']), 2)

    def test_channel_backfill_is_read_and_later_incremental_is_unread(self):
        sid = self.subscription()
        for baseline, title in ((True, 'Historical'), (False, 'New channel entry')):
            jid = self.job('user:alice', title)
            job = core.q('SELECT url FROM jobs WHERE id=?', (jid,), one=True)
            core.q('UPDATE jobs SET created=?,source=? WHERE id=?', (time.time() + 1, f'sub:{sid}', jid))
            sources.record_sub(sid, jid, {'url': job['url']}, baseline=baseline)
        self.assertEqual(self.inbox()['total'], 1)
        self.assertEqual(self.inbox()['items'][0]['title'], 'New channel entry')
        self.assertEqual(self.inbox(state='read')['total'], 1)

    def test_existing_crawl_and_channel_executors_emit_discoveries(self):
        self.incremental()
        core.q("UPDATE crawls SET status='running' WHERE id=?", (self.a,))
        crawl.record_video(self.a, {'url': 'https://cdn.test/executor.mp4', 'title': 'Executor discovery',
                                    'media_urls': ['https://cdn.test/executor.mp4']}, 'https://example.test/detail')
        self.assertEqual(self.inbox()['items'][0]['title'], 'Executor discovery')
        sid = self.subscription()
        core.q('UPDATE subs SET seen=? WHERE id=?', ('["prior-video"]', sid))
        result = {'name': 'Source channel', 'avatar': '', 'total': 1,
                  'entries': [{'url': 'https://example.test/executor-channel', 'title': 'Channel discovery'}]}
        with patch.object(channels, 'list_channel', return_value=result):
            self.assertEqual(channels.check_sub(sid), 1)
            self.assertEqual(channels.check_sub(sid), 0)
        self.assertEqual(self.inbox()['total'], 2)
        self.assertEqual({item['title'] for item in self.inbox()['items']}, {'Executor discovery', 'Channel discovery'})

    def test_upgrade_running_scan_and_replaced_root_establish_new_baselines(self):
        core.q('UPDATE crawls SET generation=7 WHERE id=?', (self.a,))
        migrations._source_inbox(core.DB)
        core.DB.commit()
        self.discover(title='Remainder of pre-upgrade scan')
        self.assertEqual(self.inbox()['total'], 0)
        self.incremental()
        self.discover(title='Next incremental round')
        self.assertEqual(self.inbox()['total'], 1)
        core.q("UPDATE crawls SET url_key='https://example.test/replaced',generation=9 WHERE id=?", (self.a,))
        self.discover(title='Replacement initial inventory')
        self.assertEqual(self.inbox()['total'], 0)
        self.incremental()
        self.discover(title='Replacement incremental round')
        self.assertEqual(self.inbox()['total'], 1)

    def test_empty_first_scan_still_has_incremental_discoveries(self):
        self.assertIsNone(core.q('SELECT * FROM source_inbox_baselines WHERE source_id=?', (self.a,), one=True))
        crawl.refresh_crawl(self.a, 'user:alice')
        self.discover(title='First arrival after empty initial scan')
        self.assertEqual(self.inbox()['total'], 1)

    def test_stopping_initial_scan_does_not_make_resumed_history_unread(self):
        core.q("UPDATE crawls SET status='running' WHERE id=?", (self.a,))
        self.discover(title='Initial inventory before stopping')
        for _ in range(2):
            self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/stop', json={}).status_code, 200)
        self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/resume', json={}).status_code, 200)
        self.discover(title='Initial inventory after resuming')
        self.assertEqual(self.inbox()['total'], 0)
        self.assertEqual(self.inbox(state='read')['total'], 2)
        core.q("UPDATE crawls SET status='done' WHERE id=?", (self.a,))
        crawl.refresh_crawl(self.a, 'user:alice')
        self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/stop', json={}).status_code, 200)
        self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/resume', json={}).status_code, 200)
        self.discover(title='New arrival in resumed incremental scan')
        self.assertEqual(self.inbox()['total'], 1)

    def test_download_reuses_collection_import_and_existing_channel_jobs(self):
        row = core.q('SELECT owner,url_key FROM crawls WHERE id=?', (self.a,), one=True)
        core.q("INSERT INTO crawl_sources(owner,url_key,classification) VALUES(?,?,'general')", (row['owner'], row['url_key']))
        self.incremental(); self.discover()
        ident = self.inbox()['items'][0]['id']
        first = self.alice.post('/api/inbox/import', json={'ids': [ident]})
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json['added'], 1)
        second = self.alice.post('/api/inbox/import', json={'ids': [ident]})
        self.assertEqual(second.json['duplicates'], 1)
        self.assertEqual(second.json['ids'], first.json['ids'])
        self.assertEqual(self.inbox()['total'], 1)

    def test_permission_revocation_during_snapshot_blocks_read_and_mutation(self):
        self.incremental(); self.discover()
        ident = self.inbox()['items'][0]['id']
        original = sources._snapshot
        def revoked(owner, reveal, **kwargs):
            result = original(owner, reveal, **kwargs)
            web.privacy_tokens.pop('show', None)
            return result
        for endpoint in ('/api/sources', '/api/inbox', '/api/inbox/action', '/api/inbox/import'):
            token = {'owner': 'user:alice', 'reveal': True, 'expires': time.time() + 60}
            with patch.object(web, 'privacy_tokens', {'show': token}), patch.object(sources, '_snapshot', side_effect=revoked):
                if endpoint.endswith('/action'):
                    response = self.alice.post(endpoint, json={'action': 'read', 'ids': [ident]}, headers={'X-Privacy-Token': 'show'})
                elif endpoint.endswith('/import'):
                    response = self.alice.post(endpoint, json={'ids': [ident]}, headers={'X-Privacy-Token': 'show'})
                else:
                    response = self.alice.get(endpoint, headers={'X-Privacy-Token': 'show'})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.inbox()['total'], 1)

    def test_manual_hidden_changes_before_mutation_prevent_read_and_download(self):
        self.incremental(); iid = self.discover()
        jid = self.job('user:alice', 'Linked job')
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (jid, iid))
        ident = self.inbox()['items'][0]['id']
        original = sources._selected
        def hide(body, owner, reveal):
            result = original(body, owner, reveal)
            core.q('INSERT OR REPLACE INTO privacy(owner,tags,ids) VALUES(?,?,?)', (owner, '[]', json.dumps([jid])))
            return result
        for endpoint, body in (('/api/inbox/action', {'action': 'read', 'ids': [ident]}), ('/api/inbox/import', {'ids': [ident]})):
            core.q('DELETE FROM privacy WHERE owner=?', ('user:alice',))
            with patch.object(sources, '_selected', side_effect=hide):
                self.assertEqual(self.alice.post(endpoint, json=body).status_code, 403)
        self.assertEqual(core.q('SELECT state FROM source_inbox WHERE id=?', (ident,), one=True)['state'], 'unread')

    def test_import_revocation_after_collection_read_cannot_start_download(self):
        secret = self.collection('user:alice', 'adult-import', 'adult')
        self.incremental(secret)
        iid = self.discover(secret)
        token = {'owner': 'user:alice', 'reveal': True, 'expires': time.time() + 60}
        headers = {'X-Privacy-Token': 'show'}
        original = crawl._items
        def revoke_after_read(*args, **kwargs):
            items = original(*args, **kwargs)
            web.privacy_tokens.pop('show', None)
            return items
        with patch.object(web, 'privacy_tokens', {'show': token}):
            ident = self.alice.get('/api/inbox', headers=headers).json['items'][0]['id']
            with patch.object(crawl, '_items', side_effect=revoke_after_read):
                response = self.alice.post('/api/inbox/import', json={'ids': [ident]}, headers=headers)
        self.assertEqual(response.status_code, 403, response.json)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM jobs', one=True)['n'], 0)
        self.assertIsNone(core.q('SELECT job_id FROM crawl_items WHERE id=?', (iid,), one=True)['job_id'])

    def test_import_source_change_after_collection_read_cannot_start_download(self):
        self.incremental(); iid = self.discover()
        ident = self.inbox()['items'][0]['id']
        original = crawl._items
        def replace_after_read(*args, **kwargs):
            items = original(*args, **kwargs)
            core.q("UPDATE crawls SET url_key='replaced-before-download' WHERE id=?", (self.a,))
            return items
        with patch.object(crawl, '_items', side_effect=replace_after_read):
            response = self.alice.post('/api/inbox/import', json={'ids': [ident]})
        self.assertEqual(response.status_code, 403, response.json)
        self.assertNotIn('added', response.json)
        self.assertNotIn('errors', response.json)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM jobs', one=True)['n'], 0)
        self.assertIsNone(core.q('SELECT job_id FROM crawl_items WHERE id=?', (iid,), one=True)['job_id'])

    def test_login_owner_merge_keeps_deduplicated_sources_and_state(self):
        self.incremental(); own = self.discover(url='https://www.youtube.com/watch?v=abcdefghijk')
        old = 'd' * 32
        cid = self.collection(old, 'anonymous')
        self.incremental(cid)
        self.discover(cid, url='https://www.youtube.com/watch?v=abcdefghijk')
        sources.move_owner(old, 'user:alice')
        crawl.move_owner(old, 'user:alice')
        self.assertEqual(self.inbox()['total'], 1)
        self.assertEqual(len(self.inbox()['items'][0]['sources']), 2)
        self.assertIsNotNone(core.q('SELECT id FROM crawl_items WHERE id=?', (own,), one=True))
        self.assertEqual(core.q('SELECT COUNT(*) n FROM source_inbox WHERE owner=?', (old,), one=True)['n'], 0)


if __name__ == '__main__':
    unittest.main()
