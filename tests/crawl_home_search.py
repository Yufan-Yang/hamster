"""Homepage collection/library search against isolated databases and synthetic metadata."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import core, crawl, crawl_search, web  # noqa: E402


class HomeSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crawl-home-search-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve()
        for target, name, value in ((core, 'STATE', self.path), (core, 'DB_PATH', self.path / 'db'),
                                    (core, 'DB', None), (web, 'MEDIA', self.path),
                                    (web, '_seen_cache', {}), (web, 'privacy_tokens', {})):
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.dict(core.app.config, TESTING=True, SECRET_KEY='home-search-test', DIRECTORY_COLLECTION_ENABLED=True)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.dict(crawl_search._cache, clear=True)
        mock.start()
        self.addCleanup(mock.stop)
        core.init_db()
        self.addCleanup(core.DB.close)
        self.alice = self.client('alice', 'a')
        self.bob = self.client('bob', 'b')
        self.admin = self.client('admin', 'c', admin=True)
        self.a = self.collection('user:alice', 'a')
        self.a2 = self.collection('user:alice', 'a2')
        self.b = self.collection('user:bob', 'b')
        self.c = self.collection('user:admin', 'admin')

    def client(self, name, char, admin=False):
        core.q('INSERT INTO users(name,pw,admin,created) VALUES(?,?,?,?)', (name, '', int(admin), time.time()))
        device = char * 32
        core.q('INSERT INTO devices(id,user,ip,seen) VALUES(?,?,?,?)', (device, name, '127.0.0.1', time.time()))
        client = core.app.test_client()
        client.set_cookie(web.DEVICE_COOKIE, device)
        with client.session_transaction() as session:
            session['user'] = name
        return client

    def collection(self, owner, slug, classification='general'):
        url = f'https://example.test/catalog/{slug}'
        core.q("INSERT INTO crawls(owner,url,url_key,status,source_classification,created,updated) VALUES(?,?,?,'done',?,?,?)",
               (owner, url, url, classification, time.time(), time.time()))
        return core.q('SELECT id FROM crawls WHERE url=?', (url,), one=True)['id']

    def item(self, owner, title, collections, updated=1):
        marker = core.q('SELECT COALESCE(MAX(id),0)+1 n FROM crawl_items', one=True)['n']
        url, page = f'https://cdn.test/clip-{marker}.mp4', f'https://example.test/lesson/{marker}'
        core.q('INSERT INTO crawl_items(owner,url,page_url,title,description,media_urls,sources,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
               (owner, url, page, title, 'Public educational metadata', json.dumps([url]), json.dumps([page]), 1, updated))
        ident = core.q('SELECT id FROM crawl_items WHERE url=?', (url,), one=True)['id']
        for cid in collections:
            core.q('INSERT INTO crawl_results(crawl_id,item_id) VALUES(?,?)', (cid, ident))
        core.q('INSERT INTO crawl_aliases(owner,url_key,item_id) VALUES(?,?,?)', (owner, url, ident))
        return ident

    def job(self, owner, title, summary='', tags=(), transcript=''):
        marker = core.q('SELECT COALESCE(MAX(id),0)+1 n FROM jobs', one=True)['n']
        url = f'https://example.test/watch/{marker}'
        analysis = json.dumps({'title': title, 'summary': summary, 'tags': list(tags), 'library': 'Videos', 'folder': 'Talks'})
        core.q("INSERT INTO jobs(owner,url,key,status,title,analysis,transcript,files,created,updated) VALUES(?,?,?,'done',?,?,?,'[]',?,?)",
               (owner, url, core.link_key(url), title, analysis, transcript, marker, marker))
        return core.q('SELECT id FROM jobs WHERE url=?', (url,), one=True)['id']

    def response(self, client, path, **query):
        response = client.get(path, query_string=query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_collection_search_deduplicates_only_live_owned_references(self):
        shared = self.item('user:alice', 'Shared lesson', [self.a, self.a2], 4)
        only_second = self.item('user:alice', 'Ocean lesson', [self.a2], 3)
        self.item('user:alice', 'Orphan lesson', [], 8)
        self.item('user:alice', 'Foreign reference lesson', [self.b], 9)
        self.item('user:bob', 'Foreign lesson', [self.b, self.a], 10)
        self.item('user:alice', 'Missing collection lesson', [999999], 11)
        data = self.response(self.alice, '/api/crawls/search', q='lesson')
        self.assertEqual([item['id'] for item in data['items']], [shared, only_second])
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['storage_owner'], 'user:alice')
        self.assertEqual(data['items'][0]['crawl_id'], self.a2)
        self.assertTrue(all(isinstance(item['sources'], list) and isinstance(item['media_urls'], list) and 'owner' not in item for item in data['items']))
        core.q('DELETE FROM crawl_results WHERE crawl_id=?', (self.a2,))
        data = self.response(self.alice, '/api/crawls/search', q='lesson')
        self.assertEqual([(item['id'], item['crawl_id']) for item in data['items']], [(shared, self.a)])

    def test_collection_pagination_modes_and_import_target(self):
        identifiers = [self.item('user:alice', f'Lesson {index:02}', [self.a], index) for index in range(24)]
        default = self.response(self.alice, '/api/crawls/search')
        self.assertEqual((default['total'], len(default['items']), default['limit']), (24, 20, 20))
        self.assertEqual(default['items'][0]['id'], identifiers[-1])
        page = self.response(self.alice, '/api/crawls/search', q='Lesson *2?', mode='wildcard', offset=1, limit=2)
        self.assertEqual(page['total'], 4)
        self.assertEqual([item['title'] for item in page['items']], ['Lesson 22', 'Lesson 21'])
        regex = self.response(self.alice, '/api/crawls/search', q=r'Lesson 0[12]$', mode='regex')
        self.assertEqual({item['title'] for item in regex['items']}, {'Lesson 01', 'Lesson 02'})
        exact = self.response(self.alice, '/api/crawls/search', q='Lesson*2?', mode='exact')
        self.assertEqual(exact['total'], 0)
        target = default['items'][0]
        imported = self.alice.post(f'/api/crawls/{target["crawl_id"]}/import', json={'ids': [target['id']]})
        self.assertEqual(imported.status_code, 200, imported.get_json())
        self.assertEqual(imported.get_json()['added'], 1)
        self.assertEqual(self.bob.post(f'/api/crawls/{target["crawl_id"]}/import', json={'ids': [target['id']]}).status_code, 404)
        self.assertEqual(self.alice.get('/api/crawls/search?mode=regex&q=[').status_code, 400)
        for params in ({'mode': 'unknown'}, {'offset': -1}, {'limit': 501}, {'offset': 0.5}):
            self.assertEqual(self.alice.get('/api/crawls/search', query_string=params).status_code, 400)

    def test_admin_home_search_stays_within_current_account(self):
        self.item('user:alice', 'Alice lesson', [self.a])
        self.item('user:bob', 'Bob lesson', [self.b])
        own_item = self.item('user:admin', 'Admin lesson', [self.c])
        self.job('user:alice', 'Alice lesson')
        self.job('user:bob', 'Bob lesson')
        own_job = self.job('user:admin', 'Admin lesson')
        data = self.response(self.admin, '/api/crawls/search', q='lesson')
        self.assertEqual([item['id'] for item in data['items']], [own_item])
        data = self.response(self.admin, '/api/jobs', q='lesson', mode='exact', compact=1)
        self.assertEqual([job['id'] for job in data['jobs']], [own_job])
        self.assertEqual(data['storage_owner'], 'user:admin')
        self.assertEqual(data['cats'], {'Talks': 1})
        # The old admin library route is intentionally still compatible.
        legacy = self.response(self.admin, '/api/jobs', q='lesson', compact=1)
        self.assertEqual(len(legacy['jobs']), 3)

    def test_library_modes_search_metadata_and_preserve_legacy_transcripts(self):
        first = self.job('user:alice', 'Ocean lesson', summary='waves and tides', tags=['physics'])
        second = self.job('user:alice', 'Mountain lesson', summary='rock formations', tags=['geology'])
        transcript = self.job('user:alice', 'Transcript example', transcript='Only the spoken transcript says pendulum-only here.')
        self.assertEqual([row['id'] for row in self.response(self.alice, '/api/jobs', q='Ocean*lesson', mode='wildcard')['jobs']], [first])
        self.assertEqual([row['id'] for row in self.response(self.alice, '/api/jobs', q=r'rock\s+formations', mode='regex')['jobs']], [second])
        self.assertEqual([row['id'] for row in self.response(self.alice, '/api/jobs', q='physics', mode='exact')['jobs']], [first])
        self.assertEqual(self.response(self.alice, '/api/jobs', q='Ocean*lesson', mode='exact')['jobs'], [])
        with patch.object(web.search, 'subtitle_hits', return_value=[]):
            exact = self.response(self.alice, '/api/jobs', q='pendulum-only', mode='exact')
            legacy = self.response(self.alice, '/api/jobs', q='pendulum-only')
        self.assertEqual([row['id'] for row in exact['jobs']], [transcript])
        self.assertEqual([row['id'] for row in legacy['jobs']], [transcript])
        self.assertEqual(self.alice.get('/api/jobs?mode=regex&q=[').status_code, 400)
        self.assertEqual(self.alice.get('/api/jobs?mode=unknown').status_code, 400)

    def test_exact_tag_chips_keep_owner_and_privacy_scope_even_for_admin(self):
        own = self.job('user:admin', 'Owned tagged lesson', tags=['Programming'])
        hidden = self.job('user:admin', 'Hidden tagged lesson', tags=['Programming'])
        source = self.job('user:admin', 'Unknown source tagged lesson', tags=['Programming'])
        self.job('user:alice', 'Foreign tagged lesson', tags=['Programming'])
        self.job('user:admin', 'Programming title is not a tag')
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:admin', '[]', json.dumps([hidden])))
        core.q("UPDATE jobs SET source='crawl:999999' WHERE id=?", (source,))
        found = self.response(self.admin, '/api/jobs', q='#Programming', mode='exact')
        self.assertEqual([job['id'] for job in found['jobs']], [own])
        self.assertEqual(found['note_hits'], [])

    def test_exact_literals_segments_and_optional_visual_keep_existing_library_search(self):
        literal = self.job('user:alice', r'100% complete_under_score C:\path')
        self.job('user:alice', '100 percent completeXunderXscore Cpath')
        for query in ('100%', 'complete_under_score', r'C:\path'):
            found = self.response(self.alice, '/api/jobs', q=query, mode='exact')
            self.assertEqual([row['id'] for row in found['jobs']], [literal])
        self.assertEqual(self.alice.get('/api/jobs', query_string={'q': 'x' * 257, 'mode': 'exact'}).status_code, 400)
        segment = self.job('user:alice', 'Segment lesson')
        foreign = self.job('user:bob', 'Foreign segment')
        hidden = self.job('user:alice', 'Private segment')
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([hidden])))
        for jid in (segment, foreign, hidden):
            core.q("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,0,12,'字幕','segment-only-marker')", (jid,))
        found = self.response(self.alice, '/api/jobs', q='segment-only-marker', mode='exact')
        self.assertEqual([row['id'] for row in found['jobs']], [segment])
        self.assertEqual(found['jobs'][0]['hits'][0]['t'], 12)
        with patch.object(web.search, 'visual_hits', return_value={segment: [(0.95, 0, 12, '')]}) as visual:
            found = self.response(self.alice, '/api/jobs', q='only in a video frame', mode='exact', visual=1)
        self.assertEqual([row['id'] for row in found['jobs']], [segment])
        self.assertNotIn(hidden, visual.call_args.args[2])
        self.assertNotIn(foreign, visual.call_args.args[2])

    def test_library_privacy_filters_before_ai_and_cache_survives_polling(self):
        visible = self.job('user:alice', 'Visible lesson', summary='A public lesson')
        hidden = self.job('user:alice', 'Hidden lesson', tags=['private'])
        manual = self.job('user:alice', 'Manual secret lesson')
        self.job('user:bob', 'Other account lesson')
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '["private"]', json.dumps([manual])))
        offered = []

        def model(system, payload, fields, max_tokens):
            if 'candidates' not in payload:
                return {'expansions': [{'anchor': group['anchor'], 'terms': [group['anchor']]}
                                       for group in payload['required'] + payload['excluded']]}
            offered.extend(payload['candidates'])
            return {'matches': [{'id': row['id'], 'score': 90, 'reason': 'The title contains lesson'} for row in payload['candidates']]}

        with patch.object(crawl_search, '_llm_json', side_effect=model) as llm:
            first = self.response(self.alice, '/api/jobs', q='lesson', mode='ai')
            second = self.response(self.alice, '/api/jobs', q='lesson', mode='ai')
            self.assertEqual(llm.call_count, 2)
            self.assertEqual([row['id'] for row in first['jobs']], [visible])
            self.assertEqual([row['id'] for row in second['jobs']], [visible])
            self.assertTrue(all('Hidden' not in row['title'] and 'Manual' not in row['title'] and 'Other account' not in row['title'] for row in offered))
            self.assertIn('AI', first['search_interpretation'])
            self.assertIsNone(first['parsed'])
            self.assertEqual(first['note_hits'], [])
            # UI can validate the current snapshot without another model call.
            core.q('UPDATE privacy SET ids=? WHERE owner=?', (json.dumps([manual, visible]), 'user:alice'))
            checked = self.response(self.alice, '/api/jobs', mode='exact', only=1, ids=str(visible))
            self.assertEqual(checked['jobs'], [])
            self.assertEqual(llm.call_count, 2)
        self.assertTrue(core.q('SELECT 1 FROM jobs WHERE id IN (?,?)', (hidden, manual), one=True))

    def test_collection_ai_receives_current_owner_and_only_its_items(self):
        alice_item = self.item('user:alice', 'Alice lesson', [self.a])
        bob_item = self.item('user:bob', 'Bob lesson', [self.b])
        calls = []

        def search(items, query, mode, owner):
            calls.append((owner, mode, [item['id'] for item in items]))
            return {'items': items, 'interpretation': 'Synthetic scoped AI result'}

        with patch.object(crawl, 'search_items', side_effect=search):
            self.response(self.alice, '/api/crawls/search', q='lesson', mode='ai')
            self.response(self.bob, '/api/crawls/search', q='lesson', mode='ai')
        self.assertEqual(calls, [('user:alice', 'ai', [alice_item]), ('user:bob', 'ai', [bob_item])])
        response = self.alice.get('/api/crawls/search?q=lesson', headers={'X-Storage-Owner': 'user:bob'})
        self.assertEqual(response.get_json()['storage_owner'], 'user:alice')
        self.assertEqual([item['id'] for item in response.get_json()['items']], [alice_item])

    def test_collected_imported_items_obey_library_privacy_before_search(self):
        visible = self.item('user:alice', 'Visible lesson', [self.a])
        private = self.item('user:alice', 'Private lesson', [self.a])
        hidden_job = self.job('user:alice', 'Private lesson', tags=['private'])
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (hidden_job, private))
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '["private"]', '[]'))
        self.assertEqual([item['id'] for item in self.response(self.alice, '/api/crawls/search', q='lesson')['items']], [visible])
        self.assertEqual(self.alice.post(f'/api/jobs/{hidden_job}/delete').status_code, 200)
        self.assertEqual([item['id'] for item in self.response(self.alice, '/api/crawls/search', q='lesson')['items']], [visible])
        web.privacy_tokens['synthetic-reveal'] = {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True}
        response = self.alice.get('/api/crawls/search?q=lesson', headers={'X-Privacy-Token': 'synthetic-reveal'})
        self.assertEqual({item['id'] for item in response.get_json()['items']}, {visible, private})

    def test_root_classification_hides_300_results_before_search_and_total(self):
        adult = self.collection('user:alice', 'restricted', 'adult')
        pending = self.collection('user:alice', 'pending', 'unknown')
        shared = self.item('user:alice', 'General shared lesson', [self.a, adult], 500)
        restricted = {self.item('user:alice', f'Restricted lesson {index}', [adult], index)
                      for index in range(300)}
        pending_item = self.item('user:alice', 'Pending lesson', [pending], 400)
        foreign = self.item('user:bob', 'Bob lesson', [self.b], 600)
        calls = []

        def search(items, query, mode, owner):
            calls.append((owner, {item['id'] for item in items}))
            return {'items': items, 'interpretation': 'Scoped fixture'}

        # Classification belongs to the root lifecycle, never to a result read.
        with patch.object(crawl, 'classify_source', side_effect=AssertionError('Per-result classification')):
            for mode in ('exact', 'wildcard', 'regex'):
                data = self.response(self.alice, '/api/crawls/search', q='lesson', mode=mode)
                self.assertEqual((data['total'], [item['id'] for item in data['items']]), (1, [shared]))
                self.assertEqual(data['items'][0]['crawl_id'], self.a)
            with patch.object(crawl, 'search_items', side_effect=search):
                self.response(self.alice, '/api/crawls/search', q='lesson', mode='ai')
                self.response(self.bob, '/api/crawls/search', q='lesson', mode='ai')
        self.assertEqual(calls, [('user:alice', {shared}), ('user:bob', {foreign})])
        web.privacy_tokens['alice-root-reveal'] = {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True}
        unlocked = self.alice.get('/api/crawls/search?q=lesson&limit=500', headers={'X-Privacy-Token': 'alice-root-reveal'})
        self.assertEqual(unlocked.status_code, 200)
        self.assertEqual(unlocked.get_json()['total'], 302)
        self.assertEqual({item['id'] for item in unlocked.get_json()['items']}, restricted | {shared, pending_item})
        wrong_owner = self.bob.get('/api/crawls/search?q=lesson', headers={'X-Privacy-Token': 'alice-root-reveal'})
        self.assertEqual(wrong_owner.status_code, 403)
        self.assertNotIn('items', wrong_owner.get_json())

    def test_imported_source_classification_hides_library_counts_and_ai_candidates(self):
        adult = self.collection('user:alice', 'restricted', 'adult')
        pending = self.collection('user:alice', 'pending', 'unknown')
        plain = self.job('user:alice', 'Plain lesson')
        visible = self.job('user:alice', 'General lesson')
        restricted = self.job('user:alice', 'Restricted lesson')
        unknown = self.job('user:alice', 'Pending lesson')
        for jid, cid in ((visible, self.a), (restricted, adult), (unknown, pending)):
            core.q('UPDATE jobs SET source=? WHERE id=?', (f'crawl:{cid}', jid))
        for query in ({}, {'q': 'lesson'}, {'q': 'lesson', 'mode': 'exact'}, {'q': '*lesson', 'mode': 'wildcard'}):
            data = self.response(self.alice, '/api/jobs', compact=1, **query)
            self.assertEqual({row['id'] for row in data['jobs']}, {plain, visible})
            self.assertEqual(data['cats'], {'Talks': 2})
        check = self.response(self.alice, '/api/jobs', mode='exact', only=1, ids=f'{restricted},{unknown}')
        self.assertEqual(check['jobs'], [])
        seen = []

        def search(items, query, mode, owner):
            seen.append((owner, {item['id'] for item in items}))
            return {'items': items, 'interpretation': 'Scoped AI fixture'}

        with patch.object(crawl_search, 'search_items', side_effect=search):
            self.response(self.alice, '/api/jobs', q='lesson', mode='ai')
            self.response(self.bob, '/api/jobs', q='lesson', mode='ai')
        self.assertEqual(seen, [('user:alice', {plain, visible}), ('user:bob', set())])
        web.privacy_tokens['library-root-reveal'] = {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True}
        unlocked = self.alice.get('/api/jobs?mode=exact&q=lesson', headers={'X-Privacy-Token': 'library-root-reveal'})
        self.assertEqual({row['id'] for row in unlocked.get_json()['jobs']}, {restricted, unknown})
        self.assertEqual(unlocked.get_json()['cats'], {'Talks': 2})


if __name__ == '__main__':
    unittest.main()
