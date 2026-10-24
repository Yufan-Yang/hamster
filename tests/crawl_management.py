"""Task edits/deletion and concurrent collection against synthetic pages only."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('crawl_fixtures', Path(__file__).with_name('crawl.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, web


class CrawlManagementTests(unittest.TestCase):
    setUp = fixtures.CrawlTests.setUp
    start = fixtures.CrawlTests.start
    fake_get = fixtures.CrawlTests.fake_get
    run_crawl = fixtures.CrawlTests.run_crawl
    result = fixtures.CrawlTests.result

    def test_bulk_partial_errors_duplicates_and_queue_limit(self):
        urls = [f'https://example.test/list/{n}' for n in range(20)]
        response = self.client.post('/api/crawls', json={'urls': urls, 'max_pages': 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['crawls']), 20)
        self.assertEqual(response.json['errors'], [])
        partial = self.client.post('/api/crawls', json={'urls': [urls[0], urls[0], 'file:///bad', 'https://example.test/extra']}).json
        self.assertEqual(len(partial['crawls']), 2)
        self.assertEqual(len(partial['errors']), 1)
        self.assertEqual(partial['errors'][0]['url'], 'file:///bad')
        self.assertEqual(self.client.post('/api/crawls', json={'urls': urls + [urls[0]]}).status_code, 400)
        self.client.post('/api/crawls', json={'urls': [f'https://example.test/more/{n}' for n in range(20)]})
        capped = self.client.post('/api/crawls', json={'urls': [f'https://example.test/last/{n}' for n in range(20)]}).json
        self.assertEqual((len(capped['crawls']), len(capped['errors'])), (9, 11))
        self.assertEqual(core.q('SELECT COUNT(*) n FROM crawls', one=True)['n'], 50)

    def test_edit_validation_running_conflict_and_refresh_compatibility(self):
        cid = self.start()
        path = f'/api/crawls/{cid}/settings'
        for status in ('queued', 'running'):
            core.q('UPDATE crawls SET status=? WHERE id=?', (status, cid))
            for fields in ({'url': 'https://example.test/new'}, {'max_pages': 90}):
                self.assertEqual(self.client.post(path, json=fields).status_code, 409)
            self.assertEqual(self.client.post(path, json={'refresh_hours': 4}).status_code, 200)
        self.client.post(f'/api/crawls/{cid}/stop', json={})
        for fields in ({'url': 'file:///private'}, {'max_pages': 0}, {'max_pages': 501}, {'refresh_hours': .5}):
            self.assertEqual(self.client.post(path, json=fields).status_code, 400)
        with patch.object(web, 'internal_link', return_value='private address'):
            self.assertEqual(self.client.post(path, json={'url': 'http://127.0.0.1'}).status_code, 400)

    def test_url_edit_resets_only_own_scan_and_delete_preserves_downloads_and_shared_results(self):
        first = self.start()
        self.run_crawl(first)
        ids = [i['id'] for i in self.result(first)['items']]
        second = self.start()
        self.run_crawl(second)
        self.client.post(f'/api/crawls/{first}/import', json={'all': True})
        jobs = [dict(r) for r in core.q('SELECT * FROM jobs')]
        self.assertTrue(jobs)
        changed = self.client.post(f'/api/crawls/{first}/settings', json={
            'url': 'https://example.test/new/', 'max_pages': 99, 'refresh_hours': 3})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json['crawl']['status'], 'queued')
        self.assertEqual((changed.json['crawl']['found'], changed.json['crawl']['pages_done']), (0, 0))
        self.assertEqual([i['id'] for i in self.result(second)['items']], ids)
        response = self.client.post(f'/api/crawls/{first}/delete', json={})
        self.assertEqual((response.json['ok'], response.json['id']), (True, first))
        self.assertEqual(self.client.get(f'/api/crawls/{first}').status_code, 404)
        self.assertEqual(core.q('SELECT * FROM crawl_pages WHERE crawl_id=?', (first,)), [])
        self.assertEqual(core.q('SELECT * FROM crawl_results WHERE crawl_id=?', (first,)), [])
        self.assertEqual([dict(r) for r in core.q('SELECT * FROM jobs')], jobs)
        self.assertEqual([i['id'] for i in self.result(second)['items']], ids)

    def test_other_owner_cannot_edit_or_delete(self):
        cid = self.start()
        self.client.set_cookie(web.DEVICE_COOKIE, 'b' * 32)
        self.assertEqual(self.client.post(f'/api/crawls/{cid}/delete', json={}).status_code, 403)
        self.client.environ_base.pop('HTTP_X_PRIVACY_TOKEN', None)
        for action in ('settings', 'delete', 'stop', 'resume'):
            self.assertEqual(self.client.post(f'/api/crawls/{cid}/{action}', json={'max_pages': 99}).status_code, 404)
        self.assertEqual(core.q('SELECT owner FROM crawls WHERE id=?', (cid,), one=True)['owner'], self.owner)

    def test_late_request_cannot_write_after_stop_url_change_or_delete(self):
        for action in ('stop', 'replace', 'delete'):
            with self.subTest(action=action):
                cid = self.start(url=f'https://example.test/old/{action}')
                entered, release = threading.Event(), threading.Event()
                def get(fetcher, url, referer=''):
                    if '/old/' in url:
                        entered.set()
                        release.wait(3)
                        return '<title>Stale secret</title><video src="https://cdn.test/stale.mp4"></video>', url
                    return '<title>New scan</title><video src="https://cdn.test/fresh.mp4"></video>', url
                with patch.object(crawl.Fetcher, 'get', get):
                    old = threading.Thread(target=crawl.run_crawl, args=(cid,))
                    old.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        self.client.post(f'/api/crawls/{cid}/' + ('delete' if action == 'delete' else 'stop'), json={})
                        if action == 'replace':
                            self.client.post(f'/api/crawls/{cid}/settings', json={'url': 'https://example.test/new/'})
                            crawl.run_crawl(cid)
                    finally:
                        release.set()
                        old.join(3)
                    self.assertFalse(old.is_alive())
                if action == 'delete':
                    self.assertEqual(self.client.get(f'/api/crawls/{cid}').status_code, 404)
                else:
                    result = self.result(cid)
                    self.assertEqual(result['crawl']['status'], 'done' if action == 'replace' else 'stopped')
                    self.assertEqual(len(result['items']), 1 if action == 'replace' else 0)
                    self.assertNotIn('Stale secret', str(result))

    def test_duplicate_claim_is_rejected(self):
        cid = self.start()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def get(fetcher, url, referer=''):
            calls.append(url)
            entered.set()
            release.wait(3)
            return '<video src="https://cdn.test/one.mp4"></video>', url
        with patch.object(crawl.Fetcher, 'get', get):
            first = threading.Thread(target=crawl.run_crawl, args=(cid,))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                others = [threading.Thread(target=crawl.run_crawl, args=(cid,)) for _ in range(5)]
                for thread in others:
                    thread.start()
                for thread in others:
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                first.join(3)
        self.assertEqual(self.result(cid)['total'], 1)

    def test_worker_runs_two_and_keeps_process_lock_until_shutdown_requests_finish(self):
        ids = [self.start(url=f'https://example.test/concurrent/{n}') for n in range(3)]
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        lock, calls, running = threading.Lock(), [], [0, 0]
        original = crawl.run_crawl
        def run(cid):
            with lock:
                calls.append(cid)
                running[0] += 1
                running[1] = max(running)
                if running[0] == 2:
                    entered.set()
            try:
                release.wait(3)
                original(cid)
            finally:
                with lock:
                    running[0] -= 1
        with patch.object(crawl, 'run_crawl', side_effect=run), patch.object(crawl.Fetcher, 'get',
                side_effect=lambda url, referer='': ('<title>Empty page</title>', url)):
            worker = threading.Thread(target=crawl.worker_loop, args=(stop,))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(len(calls), 2)
                stop.set()
                core.ring('crawls')
                time.sleep(.15)
                competitor = threading.Thread(target=crawl.worker_loop, args=(threading.Event(),))
                competitor.start()
                competitor.join(1)
                self.assertFalse(competitor.is_alive(), 'process lock released before in-flight requests ended')
                self.assertTrue(worker.is_alive())
            finally:
                stop.set()
                release.set()
                core.ring('crawls')
                worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(running[1], 2)
        self.assertEqual(len(set(calls)), 2)
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (ids[2],), one=True)['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
