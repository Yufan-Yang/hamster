"""One root decision inherited by collected/imported videos, with no external calls."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('crawl_source_fixtures', Path(__file__).with_name('crawl.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, library_search, migrations, web


class CrawlSourceTests(unittest.TestCase):
    locked_by_default = True
    setUp = fixtures.CrawlTests.setUp
    start = fixtures.CrawlTests.start
    result = fixtures.CrawlTests.result

    def page(self, count=1):
        return {'title': 'Public root directory', 'description': 'A directory of programming lessons',
                'videos': [{'url': f'https://cdn.test/lesson-{n}.mp4', 'title': f'Lesson {n}',
                            'media_urls': [f'https://cdn.test/lesson-{n}.mp4']} for n in range(count)],
                'pages': [], 'links': []}

    def scan(self, cid, count=1):
        with patch.object(crawl, '_extract', side_effect=lambda f, url, *args: (self.page(count), url)), \
                patch.object(crawl.time, 'sleep'):
            crawl.run_crawl(cid)

    def reveal(self):
        token = 'source-tests'
        tokens = {token: {'owner': self.owner, 'expires': time.time() + 300, 'reveal': True}}
        patcher = patch.object(web, 'privacy_tokens', tokens)
        patcher.start()
        self.addCleanup(patcher.stop)
        return {'X-Privacy-Token': token}

    def test_one_root_call_classifies_350_videos_and_refresh_reuses_it(self):
        cid = self.start()
        with patch.object(crawl, 'classify_source', return_value={'classification': 'general', 'reason': 'Root lessons'}) as classify:
            self.scan(cid, 350)
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(classify.call_args.args, ('https://example.test/catalog/', 'Public root directory',
                                                       'A directory of programming lessons'))
            self.assertEqual(self.result(cid)['total'], 350)
            self.assertEqual(self.result(cid)['crawl']['source_classification'], 'general')
            self.client.post(f'/api/crawls/{cid}/refresh', json={})
            self.scan(cid, 351)
            self.assertEqual(classify.call_count, 1)
            self.assertEqual(self.result(cid)['total'], 351)
            another = self.start()
            self.assertEqual(self.result(another)['crawl']['source_classification'], 'general')
            self.scan(another, 2)
            self.assertEqual(classify.call_count, 1)

    def test_known_adult_is_immediate_and_plain_apis_hide_all_descendants(self):
        with patch.object(crawl, 'classify_source') as classify:
            cid = self.start(url='https://www.pornhub.com/video')
            state = dict(core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True))
            self.assertEqual(state['source_classification'], 'adult')
            self.assertIsNotNone(state['source_checked'])
            self.scan(cid, 350)
            classify.assert_not_called()
        self.assertEqual(self.result(cid)['total'], 0)
        self.assertEqual(self.client.get(f'/api/crawls/{cid}/export').status_code, 403)
        self.assertEqual(self.client.post(f'/api/crawls/{cid}/import', json={'all': True}).status_code, 403)
        with patch.object(library_search, 'collected_page', wraps=library_search.collected_page) as search:
            response = self.client.get(f'/api/crawls/{cid}?mode=exact&q=Lesson')
            search.assert_not_called()
            self.assertEqual((response.json['items'], response.json['total']), ([], 0))
        headers = self.reveal()
        self.assertEqual(self.client.get(f'/api/crawls/{cid}', headers=headers).json['total'], 350)
        self.assertEqual(len(self.client.get(f'/api/crawls/{cid}/export', headers=headers).get_data(as_text=True).splitlines()), 350)
        self.assertEqual(self.result(cid)['total'], 0)

    def test_unavailable_classification_stays_unknown_without_repeated_model_calls(self):
        cid = self.start()
        with patch.object(crawl, 'classify_source', side_effect=RuntimeError('model unavailable')) as classify:
            self.scan(cid)
            self.client.post(f'/api/crawls/{cid}/refresh', json={})
            self.scan(cid)
            self.assertEqual(classify.call_count, 1)
        state = self.result(cid)
        self.assertTrue(state['crawl']['privacy_locked'])
        self.assertEqual(state['total'], 0)
        self.assertEqual(core.q('SELECT source_classification FROM crawls WHERE id=?', (cid,), one=True)[0], 'unknown')
        self.assertEqual(core.q('SELECT COUNT(*) n FROM crawl_results WHERE crawl_id=?', (cid,), one=True)['n'], 1)
        self.assertEqual(self.client.get(f'/api/crawls/{cid}', headers=self.reveal()).json['total'], 1)

    def test_url_change_gets_new_root_decision_and_returning_to_old_root_reuses_cache(self):
        cid = self.start()
        with patch.object(crawl, 'classify_source', return_value={'classification': 'general', 'reason': 'Public root'}) as classify:
            self.scan(cid)
            changed = self.client.post(f'/api/crawls/{cid}/settings', json={'url': 'https://example.test/another/'})
            self.assertTrue(changed.json['crawl']['privacy_locked'])
            self.assertIsNone(core.q('SELECT source_checked FROM crawls WHERE id=?', (cid,), one=True)[0])
            self.scan(cid)
            self.assertEqual(classify.call_count, 2)
            changed = self.client.post(f'/api/crawls/{cid}/settings', json={'url': 'https://example.test/catalog/'})
            self.assertEqual(changed.json['crawl']['source_classification'], 'general')
            self.scan(cid)
            self.assertEqual(classify.call_count, 2)

    def test_cache_is_owner_scoped(self):
        with patch.object(crawl, 'classify_source', return_value={'classification': 'general', 'reason': 'Owner root'}) as classify:
            first = self.start()
            self.scan(first)
            self.client.set_cookie(web.DEVICE_COOKIE, 'b' * 32)
            second = self.start()
            self.scan(second)
            self.assertEqual(classify.call_count, 2)
            self.assertEqual(len(core.q('SELECT * FROM crawl_sources')), 2)

    def test_parallel_same_root_claims_once_and_updates_both_tasks(self):
        first = self.start()
        self.client.post(f'/api/crawls/{first}/stop', json={})
        second = self.start()
        entered, release = threading.Event(), threading.Event()
        def classify(*args):
            entered.set()
            release.wait(3)
            return {'classification': 'general', 'reason': 'Shared root'}
        with patch.object(crawl, 'classify_source', side_effect=classify) as classify:
            thread = threading.Thread(target=crawl._classify_root, args=(first, self.page()))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                crawl._classify_root(second, self.page())
                self.assertEqual(classify.call_count, 1)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual({r['source_classification'] for r in core.q('SELECT source_classification FROM crawls')}, {'general'})

    def test_imported_source_survives_url_edit_task_delete_and_owner_move(self):
        cid = self.start(url='https://www.pornhub.com/video')
        self.scan(cid)
        headers = self.reveal()
        imported = self.client.post(f'/api/crawls/{cid}/import', json={'all': True}, headers=headers)
        self.assertEqual(imported.status_code, 200)
        jid = imported.json['ids'][0]
        self.assertIn(jid, crawl.hidden_source_job_ids(self.owner))
        self.client.post(f'/api/crawls/{cid}/settings', json={'url': 'https://example.test/general/'}, headers=headers)
        self.scan(cid)
        self.assertIn(jid, crawl.hidden_source_job_ids(self.owner), 'new URL cannot reclassify old imported videos')
        self.client.post(f'/api/crawls/{cid}/delete', json={})
        self.assertIn(jid, crawl.hidden_source_job_ids(self.owner))
        self.assertEqual(crawl.hidden_source_job_ids(self.owner, reveal=True), set())
        new_owner = 'user:source-owner'
        core.q('UPDATE jobs SET owner=? WHERE owner=?', (new_owner, self.owner))
        crawl.move_owner(self.owner, new_owner)
        self.assertIn(jid, crawl.hidden_source_job_ids(new_owner))
        self.assertEqual(crawl.hidden_source_job_ids(self.owner), set())

    def test_existing_imports_and_trash_snapshots_are_backfilled_without_model(self):
        cid = self.start(url='https://www.pornhub.com/video')
        core.q('INSERT INTO jobs(id,owner,source,status,url) VALUES(100,?,?,?,?)',
               (self.owner, f'crawl:{cid}', 'done', 'https://cdn.test/legacy.mp4'))
        core.q('INSERT INTO job_trash(owner,job_id,snapshot,deleted_at) VALUES(?,101,?,?)',
               (self.owner, '{"source":"crawl:' + str(cid) + '"}', time.time()))
        with patch.object(crawl, 'classify_source') as classify:
            migrations._directory_sources(core.DB)
            core.DB.commit()
            classify.assert_not_called()
        self.assertEqual(crawl.hidden_source_job_ids(self.owner), {100, 101})

    def test_worker_startup_syncs_legacy_known_sources_without_fetch_or_model(self):
        known = self.start(url='https://51cg1.com/catalog/')
        generic = self.start(url='https://example.test/legacy/')
        self.scan(known)
        core.q("UPDATE crawls SET status='running',source_classification='unknown',source_checked=NULL WHERE id=?", (known,))
        core.q("UPDATE crawl_sources SET classification='unknown',checked=NULL WHERE url_key=?", ('https://51cg1.com/catalog/',))
        before = [dict(row) for row in core.q('SELECT * FROM crawl_pages WHERE crawl_id=?', (known,))]
        stopped = threading.Event()
        stopped.set()
        with patch.object(crawl, '_extract') as extract, patch.object(crawl, 'classify_source') as classify:
            crawl.worker_loop(stopped)
            extract.assert_not_called()
            classify.assert_not_called()
        state = dict(core.q('SELECT * FROM crawls WHERE id=?', (known,), one=True))
        self.assertEqual((state['status'], state['source_classification']), ('queued', 'adult'))
        self.assertIsNotNone(state['source_checked'])
        self.assertEqual(before, [dict(row) for row in core.q('SELECT * FROM crawl_pages WHERE crawl_id=?', (known,))])
        self.assertIsNone(core.q('SELECT source_checked FROM crawls WHERE id=?', (generic,), one=True)[0])

    def test_resumed_legacy_known_source_does_not_refetch_finished_details(self):
        cid = self.start(url='https://51cg1.com/catalog/')
        self.scan(cid)
        with crawl.transaction() as db:
            row = db.execute('SELECT * FROM crawls WHERE id=?', (cid,)).fetchone()
            crawl._queue(db, row, 'https://51cg1.com/video/old', 'detail')
            db.execute("UPDATE crawl_pages SET state='done' WHERE crawl_id=?", (cid,))
            crawl._queue(db, row, 'https://51cg1.com/video/new', 'detail')
            db.execute("UPDATE crawls SET status='queued',source_classification='unknown',source_checked=NULL WHERE id=?", (cid,))
            db.execute("UPDATE crawl_sources SET classification='unknown',checked=NULL WHERE owner=?", (self.owner,))
        seen = []
        def extract(fetcher, url, *args):
            seen.append(url)
            return self.page(), url
        with patch.object(crawl, '_extract', side_effect=extract), patch.object(crawl.time, 'sleep'), \
                patch.object(crawl, 'classify_source') as classify:
            crawl.run_crawl(cid)
            classify.assert_not_called()
        state = dict(core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True))
        self.assertEqual((state['status'], state['source_classification']), ('done', 'adult'))
        self.assertEqual(seen, ['https://51cg1.com/video/new'])


if __name__ == '__main__':
    unittest.main()
