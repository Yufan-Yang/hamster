"""Paged library and timed subtitle recall, isolated data and no model calls."""
import json
import sqlite3
import threading
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crawl_home_search as fixtures  # noqa: E402
from shiguang import core, crawl, library_search, search_index, web  # noqa: E402


class LibrarySearchTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job
    item = fixtures.HomeSearchTests.item

    def get(self, query, mode='exact', **values):
        response = self.alice.get('/api/jobs', query_string={'q': query, 'mode': mode, 'compact': 1, **values})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.json

    def segment(self, jid, text, at=12, part=0):
        core.q("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,?,?,'字幕',?)", (jid, part, at, text))

    def ready(self):
        for name in search_index.TABLES:
            while not search_index.status()[name]['ready']:
                search_index.build_batch(core.DB, name, 1000)

    def test_exact_page_counts_order_and_literal_punctuation(self):
        ids = [self.job('user:alice', f'Python lesson {i}') for i in range(7)]
        self.job('user:bob', 'Python foreign')
        first = self.get('Python', limit=3)
        second = self.get('Python', limit=3, offset=3)
        self.assertEqual((first['search_total'], first['search_limit'], first['search_offset']), (7, 3, 0))
        self.assertEqual([row['id'] for row in first['jobs']], list(reversed(ids))[:3])
        self.assertEqual([row['id'] for row in second['jobs']], list(reversed(ids))[3:6])
        self.assertTrue(second['more'])
        self.assertFalse(self.get('Python', limit=3, offset=6)['more'])
        literal = self.job('user:alice', r'100% literal_under_score C:\path')
        self.ready()
        for query in ('100%', 'literal_under', r'C:\path'):
            self.assertEqual([row['id'] for row in self.get(query)['jobs']], [literal])

    def test_wildcard_caption_only_and_real_time_before_and_after_index(self):
        jid = self.job('user:alice', 'A lecture')
        self.segment(jid, 'Python beginner lesson in the transcript', 27, 1)
        for ready in (False, True):
            if ready:
                self.ready()
            data = self.get('Python*lesson', 'wildcard')
            self.assertEqual(data['search_total'], 1)
            self.assertEqual(data['jobs'][0]['id'], jid)
            self.assertEqual(data['jobs'][0]['hits'][0],
                             {'part': 1, 't': 27.0, 'src': '字幕', 'text': 'Python beginner lesson in the transcript'})

    def test_wildcard_unicode_case_equivalence_and_field_boundaries(self):
        variants = [('mining', 'mınıng'), ('asking', 'aſKİng'), ('Σίσυφος', 'ΣΊΣΥΦΟΣ')]
        for pattern, text in variants:
            jid = self.job('user:alice', 'No title match ' + str(len(text)))
            self.segment(jid, text)
            self.ready()
            self.assertIn(jid, [row['id'] for row in self.get('*' + pattern + '*', 'wildcard')['jobs']])
        split = self.job('user:alice', 'uniqueabc', summary='uniquexyz')
        self.assertNotIn(split, [row['id'] for row in self.get('uniqueabc*uniquexyz', 'wildcard')['jobs']])

    def test_privacy_and_owner_filter_before_caption_counts(self):
        visible = self.job('user:alice', 'Public')
        private = self.job('user:alice', 'Hidden')
        foreign = self.job('user:bob', 'Foreign')
        adult = self.job('user:alice', 'Adult root')
        core.q("UPDATE jobs SET source='crawl:99999' WHERE id=?", (adult,))
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([private])))
        for jid in (visible, private, foreign, adult):
            self.segment(jid, 'subtitle-owned-marker')
        self.ready()
        for mode, query in [('exact', 'subtitle-owned-marker'), ('wildcard', 'subtitle*marker')]:
            data = self.get(query, mode, limit=1)
            self.assertEqual(data['search_total'], 1)
            self.assertEqual([row['id'] for row in data['jobs']], [visible])

    def test_token_expiry_after_search_drops_rows_and_counts(self):
        jid = self.job('user:alice', 'Public')
        self.segment(jid, 'marker')
        web.privacy_tokens['valid'] = {'owner': 'user:alice', 'expires': time.time() + 30, 'reveal': True}
        original = library_search.find
        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            web.privacy_tokens['valid']['expires'] = 0
            return result
        with patch.object(library_search, 'find', side_effect=revoke):
            response = self.alice.get('/api/jobs?q=marker&mode=exact', headers={'X-Privacy-Token': 'valid'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('jobs', response.json)
        self.assertNotIn('search_total', response.json)

    def test_changed_hidden_preferences_abort_stale_result(self):
        jid = self.job('user:alice', 'marker')
        original = library_search.find
        def hide(*args, **kwargs):
            result = original(*args, **kwargs)
            core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([jid])))
            return result
        with patch.object(library_search, 'find', side_effect=hide):
            response = self.alice.get('/api/jobs?q=marker&mode=exact')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('search_total', response.json)

    def test_linked_source_evidence_is_mapped_to_owned_job(self):
        source = self.job('user:bob', 'Shared source')
        linked = self.job('user:alice', 'Linked lecture')
        core.q("UPDATE jobs SET ref=?,status='linked' WHERE id=?", (source, linked))
        self.segment(source, 'shared subtitle marker', 44)
        data = self.get('shared*marker', 'wildcard')
        self.assertEqual([row['id'] for row in data['jobs']], [linked])
        self.assertEqual(data['jobs'][0]['hits'][0]['t'], 44)

    def test_page_evidence_is_bounded_and_original_transcripts_are_not_returned(self):
        ids = [self.job('user:alice', f'lecture {i}', transcript='spoken ' * 1000) for i in range(4)]
        for jid in ids:
            core.DB.executemany("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,0,?,'字幕','spoken marker')",
                                [(jid, i) for i in range(150)])
        core.DB.commit()
        self.ready()
        data = self.get('spoken', limit=1)
        self.assertEqual(data['search_total'], 4)
        self.assertEqual(len(data['jobs']), 1)
        self.assertEqual(len(data['jobs'][0]['hits']), 30)
        self.assertNotIn('transcript', data['jobs'][0])
        self.assertNotIn('segments', data['jobs'][0])
        self.assertLess(len(json.dumps(data)), 30000)

    def test_transcript_without_timeline_is_text_evidence_without_invented_time(self):
        jid = self.job('user:alice', 'untimed', transcript='only transcript matches this marker')
        with patch.object(web.search, 'subtitle_hits', return_value=[]):
            data = self.get('transcript matches')
        self.assertEqual(data['jobs'][0]['id'], jid)
        self.assertIsNone(data['jobs'][0]['hits'][0]['t'])
        self.assertIn('transcript matches', data['jobs'][0]['hits'][0]['text'])

    def test_state_and_device_filters_precede_pagination(self):
        a = self.job('user:alice', 'marker waiting')
        b = self.job('user:alice', 'marker final failure')
        c = self.job('user:alice', 'marker queued follow')
        core.q("UPDATE jobs SET status='failed',retry_at=9999999999,device='📱 Phone' WHERE id=?", (a,))
        core.q("UPDATE jobs SET status='failed',retry_at=NULL,device='Other' WHERE id=?", (b,))
        core.q("UPDATE jobs SET status='queued',source='sub:1' WHERE id=?", (c,))
        self.assertEqual(self.get('marker', state='all')['search_total'], 2)
        self.assertEqual(self.get('marker', state='active')['search_total'], 2)
        self.assertEqual([r['id'] for r in self.get('marker', state='failed')['jobs']], [b])
        self.assertEqual([r['id'] for r in self.get('marker', state='device', device='Phone')['jobs']], [a])

    def test_short_chinese_wildcard_does_not_run_python_on_unrelated_captions(self):
        jid = self.job('user:alice', 'Lecture')
        core.DB.executemany("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,0,?,'字幕','unrelated-caption')",
                            [(jid, i) for i in range(5000)])
        core.DB.commit()
        self.segment(jid, '这是教程原文', 5010)
        original = library_search._wild
        unrelated = []
        def counted(query, text):
            if text == 'unrelated-caption':
                unrelated.append(text)
            return original(query, text)
        with patch.object(library_search, '_wild', side_effect=counted):
            data = self.get('*教程*', 'wildcard')
        self.assertEqual(data['search_total'], 1)
        self.assertEqual(data['jobs'][0]['hits'][0]['t'], 5010)
        self.assertEqual(unrelated, [])

    def test_collected_duration_reuses_only_owned_single_video_probe(self):
        own = self.job('user:alice', 'Own measured')
        foreign = self.job('user:bob', 'Foreign measured')
        a = self.item('user:alice', 'Duration lesson', [self.a])
        b = self.item('user:alice', 'Duration foreign link', [self.a])
        for jid, path in ((own, '/synthetic/own.mp4'), (foreign, '/synthetic/foreign.mp4')):
            core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([path]), jid))
            core.q("INSERT INTO probes(path,duration,vcodec) VALUES(?,93.5,'h264')", (path,))
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (own, a))
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (foreign, b))
        data = self.alice.get('/api/crawls/search?q=Duration').json
        durations = {row['id']: row['duration'] for row in data['items']}
        self.assertEqual(durations[a], 93.5)
        self.assertIsNone(durations[b])
        core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps(['/synthetic/own.mp4', '/synthetic/unmeasured.mp4']), own))
        mixed = self.alice.get('/api/crawls/search?q=Duration').json
        self.assertIsNone({r['id']: r['duration'] for r in mixed['items']}[a])
        core.q('UPDATE crawl_items SET duration=120 WHERE id=?', (a,))
        self.assertEqual({r['id']: r['duration'] for r in self.alice.get('/api/crawls/search?q=Duration').json['items']}[a], 120)

    def test_duration_survives_merged_aliases(self):
        core.q("UPDATE crawls SET status='running' WHERE id=?", (self.a,))
        base = 'https://duration.test/'
        a = crawl.record_video(self.a, {'url': base + 'unknown'}, base)
        b = crawl.record_video(self.a, {'url': base + 'known', 'duration': 123}, base)
        merged = crawl.record_video(self.a, {'url': base + 'unknown', 'aliases': [base + 'known']}, base)
        self.assertEqual(merged, a)
        self.assertIsNone(core.q('SELECT 1 FROM crawl_items WHERE id=?', (b,), one=True))
        self.assertEqual(core.q('SELECT duration FROM crawl_items WHERE id=?', (a,), one=True)['duration'], 123)

    def test_search_connection_is_read_only(self):
        before = core.q('SELECT COUNT(*) n FROM jobs', one=True)['n']
        with self.assertRaises(sqlite3.OperationalError):
            with library_search._reader() as db:
                db.execute("INSERT INTO jobs(owner,title) VALUES('user:alice','should not write')")
        self.assertEqual(core.q('SELECT COUNT(*) n FROM jobs', one=True)['n'], before)

    def test_busy_search_does_not_hold_shared_connection_lock(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def search_reader():
            try:
                with library_search._reader() as db:
                    def pause():
                        entered.set()
                        release.wait(3)
                        return 1
                    db.create_function('test_pause', 0, pause)
                    db.execute('SELECT test_pause()').fetchall()
            except Exception as exc:
                errors.append(exc)
        def small_query():
            try:
                core.q('SELECT COUNT(*) FROM jobs')
                completed.set()
            except Exception as exc:
                errors.append(exc)
        busy = threading.Thread(target=search_reader)
        busy.start()
        small = None
        try:
            self.assertTrue(entered.wait(2))
            small = threading.Thread(target=small_query)
            small.start()
            self.assertTrue(completed.wait(1), 'an active search blocked the shared application connection')
        finally:
            release.set()
            busy.join(4)
            if small:
                small.join(4)
        self.assertFalse(busy.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
