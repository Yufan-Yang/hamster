"""Offline regressions for resumed details and improved duplicate metadata."""
import ast
import builtins
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import unittest


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE crawls(id INTEGER PRIMARY KEY, owner TEXT, status TEXT, generation INT, merged INT DEFAULT 0, updated REAL);
            INSERT INTO crawls(id,owner,status,generation) VALUES(1,'alice','running',2);
            CREATE TABLE crawl_pages(id INTEGER PRIMARY KEY, crawl_id INT, url_key TEXT, url TEXT, kind TEXT,
                source TEXT, title TEXT, generation INT, state TEXT DEFAULT 'queued', error TEXT DEFAULT '',
                failures INT DEFAULT 0, retry_at REAL, heal_reported INT DEFAULT 0,duration REAL);
            CREATE TABLE crawl_items(id INTEGER PRIMARY KEY, owner TEXT, url TEXT, page_url TEXT, title TEXT,
                description TEXT, media_urls TEXT, sources TEXT, created REAL, updated REAL,duration REAL);
            CREATE TABLE crawl_aliases(owner TEXT, url_key TEXT, item_id INT, UNIQUE(owner,url_key));
            CREATE TABLE crawl_results(crawl_id INT, item_id INT, UNIQUE(crawl_id,item_id));
        ''')

        @contextmanager
        def transaction():
            with self.db:
                yield self.db

        source = Path(__file__).resolve().parents[1] / 'shiguang/crawl.py'
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in {'_queue', '_unique', '_media_unique', 'record_video'}]
        # This narrow metadata harness has no Flask package or inbox schema.
        # tests/sources.py exercises the real executor-to-inbox transaction.
        def import_dependency(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 1 and fromlist == ('sources',):
                return SimpleNamespace(sources=SimpleNamespace(record_crawl=lambda *args: None))
            return builtins.__import__(name, globals, locals, fromlist, level)
        self.scope = dict(json=json, time=time, MAX_ITEMS=5000, MEDIA_EXT=re.compile(r'\.mp4'),
                          identity=lambda url, *args, **kw: url, http_url=lambda url, base='': url,
                          transaction=transaction, Stopped=RuntimeError,
                          __builtins__={**vars(builtins), '__import__': import_dependency})
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.scope)
        self.crawl = self.db.execute('SELECT * FROM crawls').fetchone()

    def tearDown(self):
        self.db.close()

    def test_new_generation_requeues_unfinished_details_and_reuses_finished(self):
        queue = self.scope['_queue']
        for state in ('queued', 'running', 'failed', 'done'):
            url = 'https://example.test/' + state
            self.db.execute('INSERT INTO crawl_pages(crawl_id,url_key,url,kind,generation,state,error,failures,retry_at,heal_reported) VALUES(1,?,?,?,1,?,?,3,9999999999,8)',
                            (url, url, 'detail', state, 'old error'))
            self.assertFalse(queue(self.db, self.crawl, url, 'detail'))
            row = self.db.execute('SELECT * FROM crawl_pages WHERE url=?', (url,)).fetchone()
            if state == 'done':
                self.assertEqual((row['state'], row['generation']), ('done', 1))
            else:
                self.assertEqual((row['state'], row['generation'], row['error']), ('queued', 2, ''))
                self.assertEqual((row['failures'], row['retry_at'], row['heal_reported']), (0, None, 8))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM crawl_pages').fetchone()[0], 4)

    def test_same_generation_done_detail_is_not_requested_again(self):
        queue = self.scope['_queue']
        url = 'https://example.test/detail'
        self.assertTrue(queue(self.db, self.crawl, url, 'detail'))
        self.db.execute("UPDATE crawl_pages SET state='done'")
        self.assertFalse(queue(self.db, self.crawl, url, 'detail'))
        self.assertEqual(self.db.execute('SELECT state FROM crawl_pages').fetchone()[0], 'done')

    def test_listing_is_requested_again_in_new_generation(self):
        url = 'https://example.test/list'
        old = dict(self.crawl, generation=1)
        self.scope['_queue'](self.db, old, url, 'list')
        self.db.execute("UPDATE crawl_pages SET state='done'")
        self.scope['_queue'](self.db, self.crawl, url, 'list')
        row = self.db.execute('SELECT * FROM crawl_pages').fetchone()
        self.assertEqual((row['state'], row['generation']), ('queued', 2))

    def test_duplicate_can_fill_missing_title_without_overwriting_known_title(self):
        record = self.scope['record_video']
        video = {'url': 'https://example.test/video', 'title': '', 'description': 'original'}
        first = record(1, video, 'https://example.test/catalog')
        for title in ('Useful tutorial title', 'Different later title'):
            self.assertEqual(record(1, dict(video, title=title), 'https://example.test/catalog2'), first)
        rows = self.db.execute('SELECT * FROM crawl_items').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Useful tutorial title')
        self.assertIn('https://example.test/catalog2', json.loads(rows[0]['sources']))

    def test_duration_is_persisted_and_missing_or_invalid_values_do_not_erase_it(self):
        record = self.scope['record_video']
        video = {'url': 'https://example.test/video-duration', 'duration': 93.5}
        ident = record(1, video, 'https://example.test/catalog')
        for value in (None, 0, -1, True, float('nan'), float('inf'), '93'):
            self.assertEqual(record(1, dict(video, duration=value), 'https://example.test/catalog'), ident)
            self.assertEqual(self.db.execute('SELECT duration FROM crawl_items WHERE id=?', (ident,)).fetchone()[0], 93.5)
        record(1, dict(video, duration=120), 'https://example.test/catalog')
        self.assertEqual(self.db.execute('SELECT duration FROM crawl_items WHERE id=?', (ident,)).fetchone()[0], 120)

    def test_card_duration_survives_queue_and_enriches_completed_unique_video(self):
        queue, record = self.scope['_queue'], self.scope['record_video']
        url = 'https://example.test/watch-duration'
        queue(self.db, self.crawl, url, 'detail', duration=120)
        self.assertEqual(self.db.execute('SELECT duration FROM crawl_pages WHERE url=?', (url,)).fetchone()[0], 120)
        queue(self.db, self.crawl, url, 'detail')
        self.assertEqual(self.db.execute('SELECT duration FROM crawl_pages WHERE url=?', (url,)).fetchone()[0], 120)
        ident = record(1, {'url': url}, 'https://example.test/catalog')
        queue(self.db, self.crawl, url, 'detail', duration=135)
        self.assertEqual(self.db.execute('SELECT duration FROM crawl_items WHERE id=?', (ident,)).fetchone()[0], 135)

    def test_card_duration_does_not_guess_for_multiple_videos_on_one_page(self):
        record = self.scope['record_video']
        page = 'https://example.test/watch-multi'
        for n in (1, 2):
            record(1, {'url': f'https://media.test/video-{n}.mp4', 'page_url': page}, page)
        self.scope['_queue'](self.db, self.crawl, page, 'detail', duration=135)
        self.assertEqual([r[0] for r in self.db.execute('SELECT duration FROM crawl_items')], [None, None])


if __name__ == '__main__':
    unittest.main()
