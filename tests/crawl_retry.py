"""Bounded directory retry/backoff and incident handoff; isolated DB, no network."""
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location('crawl_retry_fixtures', Path(__file__).with_name('crawl.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, heal, migrations


class CrawlRetryTests(unittest.TestCase):
    setUp = fixtures.CrawlTests.setUp
    start = fixtures.CrawlTests.start

    def result(self, url, *, links=None, video=False):
        return {'title': 'Synthetic lesson', 'description': '', 'videos': [
            {'url': 'https://cdn.test/lesson.mp4', 'media_urls': ['https://cdn.test/lesson.mp4'],
             'page_url': url, 'title': 'Lesson'}] if video else [],
            'links': links or [], 'pages': [], 'lazy_players': []}, url

    def run_fake(self, cid, extract, wait=None):
        self.clock = time.time()
        self.waits = []

        def advance(seconds):
            self.clock += seconds

        def waiting(topic, mark, delay):
            self.waits.append(delay)
            if wait:
                wait()
            advance(delay)

        with patch.object(crawl, '_extract', side_effect=extract), \
                patch.object(crawl.time, 'time', side_effect=lambda: self.clock), \
                patch.object(crawl.time, 'sleep', side_effect=advance), \
                patch.object(core, 'bell_wait', side_effect=waiting), patch.object(heal, 'send'):
            crawl.run_crawl(cid)

    def page(self, cid, kind='list'):
        return core.q('SELECT * FROM crawl_pages WHERE crawl_id=? AND kind=? ORDER BY id LIMIT 1', (cid, kind), one=True)

    def test_two_transient_failures_then_success_waits_and_resets(self):
        cid = self.start()
        attempted = []

        def extract(fetcher, url, listing, source=''):
            attempted.append(self.clock)
            if len(attempted) <= 2:
                raise requests.ReadTimeout('https://private.test/root?secret=1')
            return self.result(url, video=True)

        with patch.object(heal, 'report') as report:
            self.run_fake(cid, extract)
            report.assert_not_called()
        self.assertEqual(len(attempted), 3)
        self.assertGreaterEqual(attempted[1] - attempted[0], 2)
        self.assertGreaterEqual(attempted[2] - attempted[1], 10)
        row = self.page(cid)
        self.assertEqual((row['state'], row['failures'], row['retry_at'], row['heal_reported']), ('done', 0, None, 0))
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (cid,), one=True)['status'], 'done')
        self.assertTrue(self.waits)
        self.assertLessEqual(max(self.waits), .5)

    def test_terminal_failure_has_one_private_diagnostic_incident_and_no_automatic_fourth_attempt(self):
        cid = self.start()
        attempts = []

        def extract(fetcher, url, listing, source=''):
            attempts.append(url)
            raise requests.ConnectionError('private connection URL https://private.test/token')

        with patch.object(heal, 'report', wraps=heal.report) as report:
            self.run_fake(cid, extract)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(report.call_count, 1)
            args, kwargs = report.call_args
            self.assertEqual(args[:2], ('crawl', '目录采集'))
            self.assertNotIn('http', args[2])
            self.assertEqual(kwargs['context']['owner'], self.owner)
            self.assertEqual(kwargs['context']['attempts'], 3)
            row = self.page(cid)
            self.assertEqual((row['state'], row['failures'], row['retry_at']), ('failed', 3, None))
            self.assertGreater(row['heal_reported'], 0)
            incident = core.q('SELECT * FROM incidents WHERE id=?', (row['heal_reported'],), one=True)
            self.assertEqual(json.loads(incident['context'])['page_id'], row['id'])
            # A user explicitly retries the page, but a known persistent failure
            # must not spam the healer with the same page until it succeeds.
            self.client.post(f'/api/crawls/{cid}/resume', json={})
            self.run_fake(cid, extract)
            self.assertEqual(len(attempts), 6)
            self.assertEqual(report.call_count, 1)

    def test_other_ready_details_run_before_a_delayed_retry(self):
        cid = self.start()
        order, failed = [], [False]

        def extract(fetcher, url, listing, source=''):
            order.append(url)
            if listing:
                return self.result(url, links=[{'url': 'https://example.test/retry'}, {'url': 'https://example.test/ready'}])
            if url.endswith('/retry') and not failed[0]:
                failed[0] = True
                raise requests.ReadTimeout('temporary')
            return self.result(url, video=True)

        self.run_fake(cid, extract)
        self.assertEqual([url.rsplit('/', 1)[-1] for url in order[1:]], ['retry', 'ready', 'retry'])
        self.assertEqual(core.q("SELECT COUNT(*) n FROM crawl_pages WHERE crawl_id=? AND state='done'", (cid,), one=True)['n'], 3)

    def test_stop_or_delete_while_waiting_cancels_before_another_request(self):
        for action in ('stop', 'delete'):
            with self.subTest(action=action):
                cid = self.start(url=f'https://example.test/{action}')
                calls = []

                def extract(*args):
                    calls.append(1)
                    raise requests.ReadTimeout('temporary')

                def stop():
                    self.client.post(f'/api/crawls/{cid}/{action}', json={})

                with patch.object(heal, 'report') as report:
                    self.run_fake(cid, extract, stop)
                    report.assert_not_called()
                self.assertEqual(len(calls), 1)
                row = core.q('SELECT status FROM crawls WHERE id=?', (cid,), one=True)
                self.assertEqual(row['status'] if row else None, 'stopped' if action == 'stop' else None)

    def test_failure_after_generation_change_is_discarded(self):
        cid = self.start()

        def extract(*args):
            self.client.post(f'/api/crawls/{cid}/stop', json={})
            raise requests.ReadTimeout('late timeout')

        with patch.object(heal, 'report') as report:
            self.run_fake(cid, extract)
            report.assert_not_called()
        row = self.page(cid)
        self.assertEqual((row['state'], row['failures'], row['retry_at']), ('queued', 0, None))

    def test_only_transient_failures_are_eligible(self):
        for status in (403, 404, 429, 500, 502, 503, 599):
            response = requests.Response()
            response.status_code = status
            self.assertEqual(bool(crawl._transient_failure(requests.HTTPError('response', response=response))), status == 429 or status >= 500)
        self.assertTrue(crawl._transient_failure(requests.ReadTimeout()))
        self.assertTrue(crawl._transient_failure(ValueError('公开播放器配置暂时不可用，可以继续重试此页面')))
        self.assertFalse(crawl._transient_failure(ValueError('拒绝访问内网地址')))
        cid = self.start()
        with patch.object(heal, 'report') as report:
            self.run_fake(cid, lambda *args: (_ for _ in ()).throw(ValueError('Synthetic permanent error')))
            report.assert_not_called()
        self.assertEqual((self.page(cid)['state'], self.page(cid)['failures']), ('failed', 1))
        self.assertEqual(self.waits, [])

    def test_502_is_bounded_and_exhaustion_enters_existing_heal_path(self):
        cid = self.start()
        calls = []

        def extract(*args):
            calls.append(self.clock)
            response = requests.Response()
            response.status_code = 502
            raise requests.HTTPError('502 Bad Gateway', response=response)

        with patch.object(heal, 'report') as report:
            self.run_fake(cid, extract)
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(calls[1] - calls[0], 2)
        self.assertGreaterEqual(calls[2] - calls[1], 10)
        self.assertEqual((self.page(cid)['state'], self.page(cid)['failures']), ('failed', 3))
        report.assert_called_once()
        self.assertIn('HTTP 502', report.call_args.args[2])

    def test_refresh_keeps_failed_directory_when_root_no_longer_links_it(self):
        for status in ('done', 'limited'):
            with self.subTest(status=status):
                cid = self.start(url=f'https://example.test/{status}', max_pages=2)
                root = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
                missing = f'https://example.test/{status}/previous-page'
                with crawl.transaction() as db:
                    crawl._queue(db, root, missing, 'list')
                    db.execute("UPDATE crawl_pages SET state='failed',failures=3,error='502 Bad Gateway' WHERE url=?", (missing,))
                    db.execute("UPDATE crawl_pages SET state='done' WHERE crawl_id=? AND url!=?", (cid, missing))
                    db.execute('UPDATE crawls SET status=? WHERE id=?', (status, cid))
                crawl.refresh_crawl(cid)
                seen = []

                def extract(fetcher, url, listing, source=''):
                    seen.append(url)
                    return self.result(url, video=True)

                self.run_fake(cid, extract)
                self.assertIn(missing, seen)
                page = core.q('SELECT * FROM crawl_pages WHERE url=?', (missing,), one=True)
                current = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
                self.assertEqual((page['state'], page['generation']), ('done', current['generation']))
                self.assertEqual(current['status'], 'done')

    def test_refresh_retains_failed_directory_pending_at_existing_page_budget(self):
        cid = self.start(max_pages=1)
        root = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
        missing = 'https://example.test/older-failed-page'
        with crawl.transaction() as db:
            crawl._queue(db, root, missing, 'list')
            db.execute("UPDATE crawl_pages SET state='failed',failures=3 WHERE url=?", (missing,))
            db.execute("UPDATE crawls SET status='limited' WHERE id=?", (cid,))
        crawl.refresh_crawl(cid)
        self.run_fake(cid, lambda fetcher, url, listing, source='': self.result(url, video=True))
        current = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
        page = core.q('SELECT * FROM crawl_pages WHERE url=?', (missing,), one=True)
        self.assertEqual(current['status'], 'limited')
        self.assertEqual((page['state'], page['generation']), ('queued', current['generation']))

    def test_migration_is_idempotent_and_pending_backoff_survives_restart(self):
        cid = self.start()
        core.q('UPDATE crawl_pages SET failures=1,retry_at=? WHERE crawl_id=?', (time.time() + 2, cid))
        with core.db_lock:
            migrations._crawl_page_retries(core.DB)
            core.DB.commit()
        self.run_fake(cid, lambda fetcher, url, listing, source='': self.result(url, video=True))
        self.assertTrue(self.waits)
        self.assertEqual(self.page(cid)['state'], 'done')


if __name__ == '__main__':
    unittest.main()
