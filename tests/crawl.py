"""Directory workflow regressions against an isolated DB and synthetic pages; no external network."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import core, crawl, download, pipeline, web  # noqa: E402


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crawl-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for target, name, value in ((core, 'STATE', self.path), (core, 'DB_PATH', self.path / 'db'),
                                     (core, 'DB', None), (web, '_seen_cache', {})):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(core.app.config, TESTING=True, SECRET_KEY='crawl-test')
        p.start()
        self.addCleanup(p.stop)
        core.init_db()
        self.addCleanup(core.DB.close)
        self.client = core.app.test_client()
        self.owner = 'a' * 32
        self.client.set_cookie(web.DEVICE_COOKIE, self.owner)
        # Operational workflow tests exercise authorized management of newly
        # queued (not yet classified) roots. Privacy suites use locked clients.
        if not getattr(self, 'locked_by_default', False):
            tokens = patch.dict(web.privacy_tokens, {'workflow': {'owner': self.owner, 'reveal': True,
                                                                 'expires': time.time() + 86400}})
            tokens.start()
            self.addCleanup(tokens.stop)
            self.client.environ_base['HTTP_X_PRIVACY_TOKEN'] = 'workflow'
        self.public = patch.object(web, 'internal_link', return_value=None)
        self.public.start()
        self.addCleanup(self.public.stop)
        self.source_classifier = patch.object(crawl, 'classify_source', return_value={'classification': 'general', 'reason': 'Synthetic public lessons'})
        self.source_classifier.start()
        self.addCleanup(self.source_classifier.stop)
        self.pages = {
            'https://example.test/catalog/': '<title>Public lessons</title><article><a href="/video/1">Python 入门</a></article>'
                '<article><a href="/video/2">Python 镜像</a></article><a rel="next" href="?page=2">下一页</a>',
            'https://example.test/catalog/?page=2': '<article><a href="/video/3">第二集</a></article>'
                '<article><a href="/video/1?utm_source=again">Python 入门</a></article>',
            'https://example.test/video/1': '<title>Python 入门第一集</title><meta name="description" content="基础教学">'
                '<video src="https://cdn.test/lesson.mp4?id=1&amp;token=old"></video>',
            'https://example.test/video/2': '<title>同一个视频的镜像页</title>'
                '<video src="https://cdn.test/lesson.mp4?id=1&amp;token=new"></video>',
            'https://example.test/video/3': '<title>Python 第二集</title><video src="https://cdn.test/lesson.mp4?id=2"></video>',
        }
        self.fetched = []

    def start(self, **fields):
        response = self.client.post('/api/crawls', json={'url': 'https://example.test/catalog/', **fields})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['crawl']['id']

    def fake_get(self, fetcher, url, referer=''):
        self.fetched.append(url)
        if url not in self.pages:
            raise ValueError('Synthetic missing page')
        return self.pages[url], url

    def run_crawl(self, cid):
        def get(fetcher, url, referer=''):
            return self.fake_get(fetcher, url, referer)
        with patch.object(crawl.Fetcher, 'get', get), patch.object(crawl.time, 'sleep'):
            crawl.run_crawl(cid)

    def result(self, cid, query=''):
        response = self.client.get(f'/api/crawls/{cid}' + query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_end_to_end_pagination_merge_query_ids_search_and_export(self):
        cid = self.start()
        self.run_crawl(cid)
        result = self.result(cid)
        self.assertEqual(result['crawl']['status'], 'done')
        self.assertEqual(result['crawl']['pages_done'], 2)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['crawl']['merged'], 1)
        first, second = result['items']
        self.assertEqual(len(first['sources']), 2)
        self.assertIn('token=new', first['media_urls'][0])
        self.assertIn('id=2', second['url'])
        self.assertNotIn('https://example.test/video/1?utm_source=again', self.fetched)
        self.assertEqual(self.result(cid, '?mode=wildcard&q=Python*集')['total'], 2)
        self.assertEqual(self.result(cid, '?mode=exact&q=Python*集')['total'], 0)
        self.assertEqual(self.result(cid, '?mode=regex&q=第二.Para')['total'], 0)
        exported = self.client.get(f'/api/crawls/{cid}/export').get_data(as_text=True)
        self.assertIn('https://cdn.test/lesson.mp4?id=2', exported)
        self.assertEqual(len(exported.splitlines()), 2)

    def test_incremental_refresh_reads_new_details_only_and_keeps_results(self):
        cid = self.start()
        self.run_crawl(cid)
        original_ids = [i['id'] for i in self.result(cid)['items']]
        self.pages['https://example.test/catalog/'] += '<article><a href="/video/4">New lesson</a></article>'
        self.pages['https://example.test/video/4'] = '<title>New lesson</title><video src="https://cdn.test/new.mp4"></video>'
        self.fetched.clear()
        response = self.client.post(f'/api/crawls/{cid}/refresh', json={})
        self.assertEqual(response.status_code, 200)
        self.run_crawl(cid)
        result = self.result(cid)
        self.assertEqual(result['total'], 3)
        self.assertEqual([i['id'] for i in result['items']][:2], original_ids)
        self.assertEqual(set(self.fetched), {'https://example.test/catalog/', 'https://example.test/catalog/?page=2', 'https://example.test/video/4'})
        self.assertGreater(result['crawl']['next_refresh'], result['crawl']['last_refresh'])

    def test_page_limit_can_resume_and_stop_disables_schedule(self):
        cid = self.start(max_pages=1)
        self.run_crawl(cid)
        self.assertEqual(self.result(cid)['crawl']['status'], 'limited')
        self.assertEqual(self.client.post(f'/api/crawls/{cid}/resume', json={}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/crawls/{cid}/resume', json={'max_pages': 2}).status_code, 200)
        self.run_crawl(cid)
        self.assertEqual(self.result(cid)['total'], 2)
        self.client.post(f'/api/crawls/{cid}/stop', json={})
        state = self.result(cid)['crawl']
        self.assertEqual((state['status'], state['refresh_hours'], state['next_refresh']), ('stopped', 0, None))

    def test_legacy_result_limit_resumes_original_pages_and_large_results_stay_pageable(self):
        cid = self.start(max_pages=351)
        with crawl.transaction() as db:
            db.executemany('INSERT INTO crawl_items(owner,url,page_url,title,created,updated) VALUES(?,?,?,?,0,0)',
                           [(self.owner, f'https://cdn.test/seed-{i}.mp4', 'https://example.test/catalog/', f'Seed {i}') for i in range(5000)])
            db.execute('INSERT INTO crawl_results(crawl_id,item_id) SELECT ?,id FROM crawl_items', (cid,))
            db.executemany("INSERT INTO crawl_pages(crawl_id,url_key,url,kind,state,generation) VALUES(?,?,?,'detail','done',0)",
                           [(cid, f'https://example.test/old/{i}', f'https://example.test/old/{i}') for i in range(10500)])
            db.execute("UPDATE crawls SET status='limited',error='达到本次页数或结果上限，可提高页数后继续' WHERE id=?", (cid,))
        resumed = self.client.post(f'/api/crawls/{cid}/resume', json={}).get_json()['crawl']
        self.assertEqual(resumed['max_pages'], 351)
        self.run_crawl(cid)
        data = self.result(cid, '?offset=5000&limit=1')
        self.assertEqual(data['crawl']['status'], 'done')
        self.assertEqual(data['crawl']['pages_done'], 2)
        self.assertEqual(data['total'], 5002)
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['items'][0]['title'], 'Python 入门第一集')
        # Browsing uses SQL pagination; exports still include every result.
        from shiguang import library_search
        with patch.object(library_search, 'collected_page', wraps=library_search.collected_page) as read:
            self.result(cid, '?offset=5001&limit=1')
            self.assertEqual(read.call_args.args[5:7], (1, 5001))
            self.assertTrue(read.call_args.kwargs['directory'])
        exported = self.client.get(f'/api/crawls/{cid}/export').get_data(as_text=True)
        self.assertEqual(len(exported.splitlines()), 5002)
        with patch.object(pipeline, 'add_job_ex') as add:
            reply = self.client.post(f'/api/crawls/{cid}/import', json={'all': True})
            self.assertEqual(reply.status_code, 400)
            self.assertIn('分批', reply.get_json()['error'])
            add.assert_not_called()

    def test_streamed_export_keeps_later_items_when_earlier_items_merge(self):
        cid = self.start()
        with crawl.transaction() as db:
            db.execute("UPDATE crawls SET source_classification='general' WHERE id=?", (cid,))
            db.executemany('INSERT INTO crawl_items(owner,url,page_url,title,created,updated) VALUES(?,?,?,?,0,0)',
                           [(self.owner, f'https://cdn.test/export-{i}.mp4', 'https://example.test/catalog/', f'Export {i}') for i in range(502)])
            db.execute('INSERT INTO crawl_results(crawl_id,item_id) SELECT ?,id FROM crawl_items', (cid,))
            ids = [r['id'] for r in db.execute('SELECT id FROM crawl_items ORDER BY id')]
        response = self.client.get(f'/api/crawls/{cid}/export', buffered=False)
        stream = iter(response.response)
        first = [next(stream).decode().strip() for _ in range(500)]
        # A collector merges already-exported entries while the stream pauses
        # between batches. OFFSET would now skip the first remaining entry.
        def merge():
            with crawl.transaction() as db:
                crawl._merge(db, ids[0], ids[1])
        worker = threading.Thread(target=merge)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        remaining = [chunk.decode().strip() for chunk in stream]
        response.close()
        self.assertEqual(remaining, ['https://cdn.test/export-500.mp4', 'https://cdn.test/export-501.mp4'])
        self.assertEqual(len(first + remaining), 502)

    def test_repeated_content_stops_unbounded_next_links(self):
        self.pages['https://example.test/catalog/?page=2'] = self.pages['https://example.test/catalog/'].replace('page=2', 'page=3')
        cid = self.start()
        self.run_crawl(cid)
        self.assertEqual(self.result(cid)['crawl']['pages_done'], 2)
        self.assertNotIn('https://example.test/catalog/?page=3', self.fetched)

    def test_failed_page_preserves_other_results_and_can_retry(self):
        del self.pages['https://example.test/video/3']
        cid = self.start()
        self.run_crawl(cid)
        result = self.result(cid)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['crawl']['errors'], 1)
        self.pages['https://example.test/video/3'] = '<video src="https://cdn.test/second.mp4"></video>'
        self.client.post(f'/api/crawls/{cid}/resume', json={})
        self.run_crawl(cid)
        self.assertEqual(self.result(cid)['total'], 2)

    def test_private_api_results_mutations_and_export_are_owner_scoped(self):
        cid = self.start()
        self.run_crawl(cid)
        self.client.set_cookie(web.DEVICE_COOKIE, 'b' * 32)
        self.assertEqual(self.client.get('/api/crawls').status_code, 403)
        self.client.environ_base.pop('HTTP_X_PRIVACY_TOKEN', None)
        self.assertEqual(self.client.get('/api/crawls').get_json()['crawls'], [])
        for path in (f'/api/crawls/{cid}', f'/api/crawls/{cid}/export'):
            self.assertEqual(self.client.get(path).status_code, 404)
        for action in ('stop', 'resume', 'refresh', 'settings', 'import'):
            self.assertEqual(self.client.post(f'/api/crawls/{cid}/{action}', json={'all': True}).status_code, 404)

    def test_import_is_idempotent_preserves_referer_and_old_library_alias(self):
        cid = self.start()
        self.run_crawl(cid)
        items = self.result(cid)['items']
        old, _ = pipeline.add_job_ex('https://example.test/video/1', owner=self.owner)
        response = self.client.post(f'/api/crawls/{cid}/import', json={'all': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        imported = response.get_json()
        self.assertEqual((imported['added'], imported['duplicates']), (1, 1))
        self.assertIn(old, imported['ids'])
        result = self.client.post(f'/api/crawls/{cid}/import', json={'ids': [x['id'] for x in items]}).get_json()
        self.assertEqual((result['added'], result['duplicates']), (0, 2))
        context = core.q('SELECT context FROM crawl_downloads', one=True)
        self.assertEqual(json.loads(context['context'])['referer'], 'https://example.test/video/3')
        self.assertEqual(self.client.post(f'/api/crawls/{cid}/import', json={'ids': [99999]}).status_code, 400)

    def test_same_media_across_crawls_reuses_item_and_download_after_token_rotation(self):
        cid = self.start()
        self.run_crawl(cid)
        first = self.client.post(f'/api/crawls/{cid}/import', json={'all': True}).get_json()
        another = self.start()
        self.pages['https://example.test/video/1'] = self.pages['https://example.test/video/1'].replace('token=old', 'token=rotated')
        self.run_crawl(another)
        result = self.client.post(f'/api/crawls/{another}/import', json={'all': True}).get_json()
        self.assertEqual(result['duplicates'], 2)
        self.assertEqual(first['ids'], result['ids'])
        self.assertEqual(core.q('SELECT COUNT(*) n FROM crawl_items', one=True)['n'], 2)

    def test_regular_add_after_crawl_resolves_media_and_detail_aliases(self):
        cid = self.start()
        self.run_crawl(cid)
        item = self.result(cid)['items'][0]
        imported = self.client.post(f'/api/crawls/{cid}/import', json={'ids': [item['id']]}).get_json()
        jid = imported['ids'][0]
        stored = core.q('SELECT key FROM jobs WHERE id=?', (jid,), one=True)['key']
        for address in (item['url'], item['url'].replace('token=new', 'token=rotated'),
                        'https://example.test/video/1', 'https://example.test/video/2'):
            self.assertEqual(pipeline.add_job_ex(address, owner=self.owner), (jid, 'duplicate'))
        self.assertEqual(core.q('SELECT COUNT(*) n FROM jobs', one=True)['n'], 1)
        self.assertEqual(core.q('SELECT key FROM jobs WHERE id=?', (jid,), one=True)['key'], stored)
        shared, how = pipeline.add_job_ex('https://example.test/video/2', owner='another-owner')
        self.assertEqual(how, 'linked')
        self.assertEqual(core.q('SELECT ref FROM jobs WHERE id=?', (shared,), one=True)['ref'], jid)

    def test_legacy_generic_key_collisions_do_not_merge_query_episodes(self):
        for parameter in ('id', 'mid', 'ref'):
            first = f'https://cdn.test/{parameter}.mp4?{parameter}=1'
            second = f'https://cdn.test/{parameter}.mp4?{parameter}=2'
            a, _ = pipeline.add_job_ex(first, owner=self.owner)
            b, how = pipeline.add_job_ex(second, owner=self.owner)
            self.assertEqual(how, 'new')
            self.assertNotEqual(a, b)
            self.assertEqual(pipeline.add_job_ex(first, owner=self.owner), (a, 'duplicate'))
            self.assertEqual(pipeline.add_job_ex(second, owner=self.owner), (b, 'duplicate'))
        a, _ = pipeline.add_job_ex('http://cdn.test/scheme.mp4', owner=self.owner)
        b, how = pipeline.add_job_ex('https://cdn.test/scheme.mp4', owner=self.owner)
        self.assertEqual(how, 'new')
        self.assertNotEqual(a, b)

    def test_resolved_platform_short_links_keep_duplicate_behavior(self):
        full = 'https://www.bilibili.com/video/BV1234567890?p=2'
        short = 'https://b23.tv/synthetic-part-two'
        original_key = pipeline.link_key
        with patch.object(pipeline, 'link_key', side_effect=lambda address:
                          'bilibili:BV1234567890:p2' if address == short else original_key(address)):
            jid, _ = pipeline.add_job_ex(full, owner=self.owner)
            self.assertEqual(pipeline.add_job_ex(short, owner=self.owner), (jid, 'duplicate'))
            other, how = pipeline.add_job_ex(full.replace('p=2', 'p=3'), owner=self.owner)
            self.assertEqual(how, 'new')
            self.assertNotEqual(jid, other)

    def test_identity_keeps_distinct_queries_and_bilibili_parts(self):
        self.assertEqual(crawl.identity('https://cdn.test/a.mp4?id=1&token=old', True), crawl.identity('https://cdn.test/a.mp4?token=new&id=1', True))
        self.assertNotEqual(crawl.identity('https://cdn.test/a.mp4?id=1', True), crawl.identity('https://cdn.test/a.mp4?id=2', True))
        self.assertNotEqual(crawl.identity('https://cdn.test/stream?token=1', True), crawl.identity('https://cdn.test/stream?token=2', True))
        self.assertNotEqual(crawl.identity('https://www.bilibili.com/video/BV1234567890?p=1'), crawl.identity('https://www.bilibili.com/video/BV1234567890?p=2'))

    def test_concurrent_adds_create_one_job(self):
        result = []
        barrier = threading.Barrier(8)
        def add():
            barrier.wait()
            result.append(pipeline.add_job_ex('https://cdn.test/a.mp4', owner=self.owner))
        threads = [threading.Thread(target=add) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(result), 8)
        self.assertEqual(len(set(i for i, _ in result)), 1)
        self.assertEqual([how for _, how in result].count('new'), 1)

    def test_validation_limits_and_private_redirects(self):
        for body in ({'url': 'file:///etc/passwd'}, {'url': 'http://user:pw@example.test'},
                     {'url': 'https://example.test', 'max_pages': 1.2}, {'url': 'https://example.test', 'max_pages': 501}):
            self.assertEqual(self.client.post('/api/crawls', json=body).status_code, 400)
        self.assertEqual(self.client.post('/api/crawls', json=['wrong']).status_code, 400)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.is_redirect = True
        response.headers = {'Location': 'http://127.0.0.1/admin'}
        fetcher = crawl.Fetcher()
        self.addCleanup(fetcher.close)
        with patch.object(fetcher.session, 'get', return_value=response) as get, \
             patch.object(web, 'internal_link', side_effect=lambda u: 'private' if '127.0.0.1' in u else None):
            with self.assertRaisesRegex(ValueError, 'private'):
                fetcher.get('https://example.test/')
        self.assertEqual(get.call_count, 1)

    def test_selected_media_download_uses_page_referer(self):
        work = self.path / 'download'
        work.mkdir()
        def stream(jid, url, dest, referer, cookies):
            self.assertEqual(referer, 'https://example.test/video/3')
            (dest / 'video.mp4').write_bytes(b'synthetic')
            return {'source': 'synthetic'}
        with patch.object(download, 'check_cancel'), patch.object(download, 'update'), \
             patch.object(download, 'fast_stream_download', side_effect=stream), patch.object(download, 'sniff_page') as sniff:
            meta, _ = download.page_video_download(1, 'https://cdn.test/clip.mp4', work,
                media_context={'media_urls': ['https://cdn.test/clip.mp4'], 'title': 'Selected clip', 'referer': 'https://example.test/video/3'})
        self.assertEqual(meta['webpage_url'], 'https://example.test/video/3')
        sniff.assert_not_called()


if __name__ == '__main__':
    unittest.main()
