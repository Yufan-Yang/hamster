"""Collector retirement must erase web indexes without deleting the user's media."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('retirement_fixtures', Path(__file__).with_name('sources.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, directory_retired as retired, sources  # noqa: E402


class DirectoryRetiredTests(unittest.TestCase):
    setUp = fixtures.SourceTests.setUp
    client = fixtures.SourceTests.client
    collection = fixtures.SourceTests.collection
    item = fixtures.SourceTests.item
    job = fixtures.SourceTests.job

    def test_default_gate_rejects_all_old_endpoints_and_worker_never_fetches(self):
        with patch.dict(core.app.config, DIRECTORY_COLLECTION_ENABLED=False):
            for method, url in [('get', '/api/crawls'), ('post', '/api/crawls'), ('get', '/api/crawls/search'),
                                ('post', f'/api/crawls/{self.a}/refresh'), ('get', f'/api/crawls/{self.a}/export')]:
                response = getattr(self.alice, method)(url)
                self.assertEqual(response.status_code, 410)
                self.assertTrue(response.json['retired'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            with patch.object(crawl, 'Fetcher', side_effect=AssertionError('retired worker fetched a page')):
                crawl.run_crawl(self.a)
                crawl.worker_loop()

    def test_cleanup_keeps_video_watch_and_private_status_and_removes_all_web_rows(self):
        adult = self.collection('user:alice', 'private-root', 'adult')
        private = self.job('user:alice', 'Private saved video')
        public = self.job('user:alice', 'General saved video')
        for jid, cid, classification in [(private, adult, 'adult'), (public, self.a, 'general')]:
            row = core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)
            core.q('UPDATE jobs SET source=?,files=? WHERE id=?', (f'crawl:{cid}', json.dumps(['/do-not-delete/video.mp4']), jid))
            core.q('INSERT INTO crawl_sources(owner,url_key,classification) VALUES(?,?,?)', ('user:alice', row['url_key'], classification))
            core.q('INSERT INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)', ('user:alice', jid, row['url_key']))
        core.q('INSERT INTO watch(owner,job_id,done,updated) VALUES(?,?,1,1)', ('user:alice', public))
        before = core.q('SELECT id,url,title,files,analysis FROM jobs ORDER BY id')
        with sources.transaction() as db:
            retired.clear_cache(db)
            retired.clear_cache(db)
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in core.q('SELECT id,url,title,files,analysis FROM jobs ORDER BY id')])
        self.assertEqual(core.q('SELECT done FROM watch WHERE job_id=?', (public,), one=True)['done'], 1)
        ids = json.loads(core.q('SELECT ids FROM privacy WHERE owner=?', ('user:alice',), one=True)['ids'])
        self.assertIn(private, ids)
        self.assertNotIn(public, ids)
        self.assertEqual(core.q('SELECT source FROM jobs WHERE id=?', (public,), one=True)['source'], 'import')
        for table in ('crawls', 'crawl_pages', 'crawl_items', 'crawl_aliases', 'crawl_results', 'crawl_downloads', 'crawl_sources', 'crawl_source_jobs'):
            self.assertEqual(core.q('SELECT count(*) n FROM ' + table, one=True)['n'], 0, table)


if __name__ == '__main__':
    unittest.main()
