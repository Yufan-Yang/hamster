"""Substring index equivalence, incremental maintenance and interrupted backfill."""
import fcntl
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('index_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, search_index as index  # noqa: E402


class SearchIndexTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def build(self, name=None):
        for table in [name] if name else index.TABLES:
            while not index.status()[table]['ready']:
                index.build_batch(core.DB, table, 7)

    def segment(self, text, ref=1, kind='job'):
        core.q('INSERT INTO seg(kind,ref,text,part,t,src) VALUES(?,?,?,0,12,?)', (kind, ref, text, '字幕'))
        return core.q('SELECT MAX(id) id FROM seg', one=True)['id']

    def hits(self, query, table='jobs', accelerated=True):
        cols = index.TABLES[table][1]
        pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        condition = (index.job_condition if table == 'jobs' else index.segment_condition)(query) if accelerated else None
        where = '(' + ' OR '.join(f"{column} LIKE ? ESCAPE '\\'" for column in cols) + ')'
        args = [pattern] * len(cols)
        if condition:
            where += ' AND ' + condition[0]
            args.extend(condition[1])
        return [row['id'] for row in core.q(f'SELECT id FROM {table} WHERE {where} ORDER BY id', args)]

    def test_migration_is_empty_and_helpers_fallback_until_each_table_is_ready(self):
        self.job('user:alice', 'initial lesson')
        self.segment('initial subtitle')
        self.assertTrue(all(not state['ready'] and state['cursor'] is None for state in index.status().values()))
        self.assertIsNone(index.job_condition('lesson'))
        self.assertIsNone(index.segment_condition('subtitle'))
        self.assertEqual(core.q("SELECT rowid FROM jobs_search_fts WHERE jobs_search_fts MATCH 'les'"), [])
        self.build('jobs')
        self.assertIsNotNone(index.job_condition('lesson'))
        self.assertIsNone(index.segment_condition('subtitle'))
        self.build('seg')
        for query in ('', 'a', '中文', 'ab\x00c'):
            self.assertIsNone(index.job_condition(query))
            self.assertIsNone(index.segment_condition(query))
        for mode in ('wildcard', 'regex', 'ai'):
            self.assertIsNone(index.job_condition('lesson', mode=mode))
        with self.assertRaises(ValueError):
            index.job_condition('lesson', alias='jobs; DROP TABLE jobs')

    def test_literal_like_equivalence_for_chinese_ascii_unicode_and_metacharacters(self):
        values = ['中文搜索教程', 'hello_HELLO_% back\\slash', '100%_\\ value', 'élève ÉLÈVE',
                  'Straße STRASSE', 'İstanbul istanbul', 'Σίσυφος', 'foo"bar', 'a\nb', 'abc\x00xyz',
                  'æÆéÉİıßſK', 'hello-first-field']
        for value in values:
            self.job('user:alice', value)
            self.segment(value)
        extra = self.job('user:alice', 'metadata-only')
        core.q('UPDATE jobs SET analysis=?,files=?,transcript=?,url=? WHERE id=?',
               (json.dumps({'summary': '原始摘要字段', 'tags': ['标签文本']}, ensure_ascii=False),
                '["literal_file.mp4"]', 'transcript-only lesson', 'https://lesson.test/100%_raw', extra))
        self.build()
        queries = ['中文搜', '搜索', 'HEL', '%_\\', '100%', 'lo_', '\\sl', 'élè', 'ÉLÈ', 'Stra', 'STRA',
                   'İst', 'ist', 'Σίσ', 'foo"', 'a\nb', '\x00xy', 'summary', '原始摘要', 'literal_file',
                   'transcript-only', 'https://lesson', 'æÆé', 'éÉİ']
        for table in index.TABLES:
            for query in queries:
                with self.subTest(table=table, query=query):
                    self.assertEqual(self.hits(query, table), self.hits(query, table, False))
        # FTS may fold more Unicode than SQLite LIKE; only the final LIKE decides.
        core.q('PRAGMA case_sensitive_like=ON')
        self.assertEqual(self.hits('HEL'), self.hits('HEL', accelerated=False))

    def test_random_substring_candidates_never_lose_like_hits(self):
        rng = random.Random(319)
        alphabet = 'aAbBcC _%\\"中文搜索éÉæÆİıßſKΣσς\n'
        values = [''.join(rng.choice(alphabet) for _ in range(35)) for _ in range(36)]
        for value in values:
            self.job('user:alice', value)
        self.build('jobs')
        for value in values:
            for start in (0, 7, 19):
                term = value[start:start + 7]
                for query in (term, term.swapcase()):
                    self.assertEqual(self.hits(query), self.hits(query, accelerated=False), repr(query))

    def test_incremental_insert_update_delete_and_unbuilt_rows_remain_consistent(self):
        first = self.job('user:alice', 'old indexed title')
        pending = self.job('user:alice', 'old pending title')
        deleted = self.job('user:alice', 'removed pending title')
        self.assertEqual(index.build_batch(core.DB, 'jobs', 1), 1)
        core.q('UPDATE jobs SET title=? WHERE id=?', ('changed indexed title', first))
        core.q('UPDATE jobs SET title=? WHERE id=?', ('changed pending title', pending))
        core.q('DELETE FROM jobs WHERE id=?', (deleted,))
        core.q('INSERT INTO jobs(id,url,title) VALUES(0,?,?)', ('https://example.test/low', 'inserted behind cursor'))
        self.build('jobs')
        for query in ('old indexed', 'changed indexed', 'changed pending', 'removed pending', 'behind cursor'):
            self.assertEqual(self.hits(query), self.hits(query, accelerated=False))
        added = self.job('user:alice', 'new after ready')
        core.q('UPDATE jobs SET title=? WHERE id=?', ('updated after ready', added))
        core.q('DELETE FROM jobs WHERE id=?', (first,))
        self.assertEqual(self.hits('updated after'), [added])
        self.assertEqual(self.hits('changed indexed'), [])
        core.q("INSERT INTO jobs_search_fts(jobs_search_fts,rank) VALUES('integrity-check',1)")

    def test_primary_key_moves_across_build_cursor_do_not_duplicate_or_lose_rows(self):
        first = self.job('user:alice', 'moving indexed record')
        self.job('user:alice', 'pending record')
        index.build_batch(core.DB, 'jobs', 1)
        core.q('UPDATE jobs SET id=100 WHERE id=?', (first,))
        self.build('jobs')
        self.assertEqual(self.hits('moving indexed'), [100])
        core.q('UPDATE jobs SET id=0 WHERE id=100')
        self.assertEqual(self.hits('moving indexed'), [0])
        core.q("INSERT INTO jobs_search_fts(jobs_search_fts,rank) VALUES('integrity-check',1)")

    def test_progress_and_identical_text_updates_do_not_rebuild_index(self):
        jid = self.job('user:alice', 'stable lesson')
        sid = self.segment('stable subtitle', jid)
        self.build()
        before = core.DB.total_changes
        core.q('UPDATE jobs SET progress=30,stage=?,updated=123 WHERE id=?', ('downloading', jid))
        self.assertEqual(core.DB.total_changes - before, 1)
        before = core.DB.total_changes
        core.q('UPDATE jobs SET title=title,analysis=analysis WHERE id=?', (jid,))
        self.assertEqual(core.DB.total_changes - before, 1)
        before = core.DB.total_changes
        core.q('UPDATE seg SET t=45,part=2,src=? WHERE id=?', ('字幕', sid))
        self.assertEqual(core.DB.total_changes - before, 1)
        core.q('UPDATE seg SET text=? WHERE id=?', ('updated subtitle', sid))
        self.assertEqual(self.hits('stable subtitle', 'seg'), [])
        self.assertEqual(self.hits('updated subtitle', 'seg'), [sid])
        core.q('DELETE FROM seg WHERE id=?', (sid,))
        self.assertEqual(self.hits('updated subtitle', 'seg'), [])
        core.q("INSERT INTO seg_search_fts(seg_search_fts,rank) VALUES('integrity-check',1)")

    def test_external_content_keeps_no_copy_and_uses_match_virtual_index(self):
        self.job('user:alice', 'candidate lesson')
        self.segment('candidate subtitle')
        self.build()
        tables = {row['name'] for row in core.q("SELECT name FROM sqlite_master WHERE type='table'")}
        for fts, _columns, _batch in index.TABLES.values():
            self.assertNotIn(fts + '_content', tables)
            self.assertNotIn(fts + '_docsize', tables)
        condition, args = index.segment_condition('candidate')
        plan = core.q('EXPLAIN QUERY PLAN SELECT id FROM seg WHERE ' + condition, args)
        self.assertTrue(any('VIRTUAL TABLE INDEX' in row['detail'] and 'M' in row['detail'] for row in plan), plan)

    def test_owner_and_hidden_scope_precede_limit_and_linked_segments_use_source_ref(self):
        foreign = self.job('user:bob', 'common private lesson')
        hidden = self.job('user:alice', 'common hidden lesson')
        visible = self.job('user:alice', 'common visible lesson')
        linked = self.job('user:alice', 'linked without title match')
        core.q('UPDATE jobs SET ref=? WHERE id=?', (foreign, linked))
        self.segment('common subtitle from shared source', foreign)
        self.build()
        where, args = index.job_condition('common', alias='j')
        rows = core.q('SELECT j.id FROM jobs j WHERE j.owner=? AND j.id<>? AND ' + where + ' ORDER BY j.id LIMIT 1',
                      ('user:alice', hidden, *args))
        self.assertEqual([row['id'] for row in rows], [visible])
        where, args = index.segment_condition('subtitle', alias='s')
        rows = core.q('SELECT j.id FROM jobs j JOIN seg s ON s.kind=? AND s.ref=COALESCE(j.ref,j.id) '
                      'WHERE j.owner=? AND j.id<>? AND ' + where, ('job', 'user:alice', hidden, *args))
        self.assertEqual([row['id'] for row in rows], [linked])

    def test_batch_never_commits_or_rolls_back_a_callers_transaction(self):
        core.DB.execute('BEGIN IMMEDIATE')
        core.DB.execute("INSERT INTO jobs(url,title) VALUES('https://example.test/uncommitted','uncommitted lesson')")
        with self.assertRaises(RuntimeError):
            index.build_batch(core.DB, 'jobs')
        self.assertTrue(core.DB.in_transaction)
        core.DB.rollback()
        self.assertEqual(core.q('SELECT id FROM jobs'), [])

    def test_background_lease_and_resume_do_not_block_startup_or_rebuild_finished_tables(self):
        for number in range(12):
            self.job('user:alice', f'background lesson {number}')
        index.build_batch(core.DB, 'jobs', 2)
        stop = threading.Event()
        with open(str(core.DB_PATH) + '.search-index.lock', 'a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            thread = index.start_background(stop)
            self.assertIs(index.start_background(stop), thread)
            time.sleep(.04)
            self.assertEqual(index.status()['jobs']['cursor'], 2)
            stop.set(); thread.join(1)
            self.assertFalse(thread.is_alive())
        thread = index.start_background()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(state['ready'] for state in index.status().values()))
        core.q("INSERT INTO jobs_search_fts(jobs_search_fts,rank) VALUES('integrity-check',1)")
        before = index.status()
        thread = index.start_background(); thread.join(1)
        self.assertEqual(index.status(), before)

    def test_missing_trigram_support_gracefully_keeps_correct_fallback(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        class Unavailable:
            def execute(self, statement, args=()):
                if statement.startswith('CREATE VIRTUAL TABLE'):
                    raise sqlite3.OperationalError('no such tokenizer: trigram')
                return db.execute(statement, args)
        index.install(Unavailable())
        db.commit()
        with patch.object(core, 'DB', db):
            self.assertTrue(all(not state['supported'] for state in index.status().values()))
            self.assertIsNone(index.job_condition('lesson'))
            self.assertEqual(index.build_batch(db, 'jobs'), 0)


if __name__ == '__main__':
    unittest.main()
