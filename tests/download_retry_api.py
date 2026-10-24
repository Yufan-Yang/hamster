"""Mobile compact download retry state and explicit cancellation, isolated database."""
import importlib.util
from pathlib import Path
import time
import unittest

spec = importlib.util.spec_from_file_location('retry_api_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core  # noqa: E402


class DownloadRetryAPITests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def waiting(self):
        jid = self.job('user:alice', 'Synthetic retry')
        due = time.time() + 300
        core.q("UPDATE jobs SET status='failed',attempts=2,retry_at=?,error=? WHERE id=?",
               (due, 'Synthetic timeout（第 2/3 次）', jid))
        return jid, due

    def test_compact_grouped_homepage_retains_retry_fields_and_owner_scope(self):
        jid, due = self.waiting()
        response = self.alice.get('/api/jobs?compact=1&grouped=1')
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json['jobs'] if item['id'] == jid)
        self.assertEqual((row['status'], row['attempts'], row['retry_at']), ('failed', 2, due))
        self.assertIn('2/3', row['error'])
        self.assertTrue(row['compact'])
        self.assertNotIn(jid, [item['id'] for item in self.bob.get('/api/jobs?compact=1&grouped=1').json['jobs']])

    def test_cancel_waiting_retry_cannot_be_claimed_again_and_foreign_owner_cannot_cancel(self):
        jid, due = self.waiting()
        self.assertEqual(self.bob.post(f'/api/jobs/{jid}/cancel').status_code, 404)
        self.assertEqual(self.alice.post(f'/api/jobs/{jid}/cancel').status_code, 200)
        row = core.q('SELECT status,retry_at,attempts FROM jobs WHERE id=?', (jid,), one=True)
        self.assertEqual(tuple(row), ('cancelled', None, 2))
        self.assertFalse(core.q("SELECT id FROM jobs WHERE id=? AND status='failed' AND retry_at<=?", (jid, due+1)))
        self.assertEqual(self.alice.post(f'/api/jobs/{jid}/retry').status_code, 200)
        row = core.q('SELECT status,retry_at,attempts,cancel FROM jobs WHERE id=?', (jid,), one=True)
        self.assertEqual(tuple(row), ('queued', None, 0, 0))


if __name__ == '__main__':
    unittest.main()
