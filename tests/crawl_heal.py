"""Verified crawler repair retries and owner/source-scoped incident visibility."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend
from shiguang import core, crawl, heal, web


class CrawlHealTests(unittest.TestCase):
    def setUp(self):
        backend.BackendReliability.setUp(self)
        enabled = patch.dict(core.app.config, DIRECTORY_COLLECTION_ENABLED=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def collection(self, status='failed', owner=None, classification='general'):
        owner = owner or self.owner
        url = f'https://example.test/root/{time.time_ns()}'
        core.q('INSERT INTO crawls(owner,url,url_key,status,generation,error,source_classification) VALUES(?,?,?,?,7,?,?)',
               (owner, url, url, status, 'old error', classification))
        cid = core.q('SELECT MAX(id) id FROM crawls', one=True)['id']
        core.q('INSERT INTO crawl_sources(owner,url_key,classification,checked) VALUES(?,?,?,?)',
               (owner, url, classification, time.time()))
        return {'owner': owner, 'url_key': url, 'root_url': url, 'crawl_id': cid, 'generation': 7}

    def incident(self, context, kind='crawl'):
        core.q('INSERT INTO incidents(sig,kind,label,title,error,context,tasks,state,first,last) VALUES(?,?,?,?,?,?,?, ?,?,?)',
               (str(time.time_ns()), kind, '目录采集', 'Private root detail', 'ReadTimeout', json.dumps(context), '[]',
                'healing', time.time(), time.time()))
        return core.q('SELECT MAX(id) id FROM incidents', one=True)['id']

    def page(self, context, iid, state='failed', generation=7):
        url = f'https://example.test/page/{time.time_ns()}'
        core.q('INSERT INTO crawl_pages(crawl_id,url,url_key,kind,state,generation,error,failures,retry_at,heal_reported) '
               'VALUES(?,?,?,\'detail\',?,?,?,?,?,?)',
               (context['crawl_id'], url, url, state, generation, 'original error', 3, time.time() + 300, iid))
        return core.q('SELECT MAX(id) id FROM crawl_pages', one=True)['id']

    def done(self, iid, **changes):
        result = {'state': 'fixed', 'retry': True, 'commit': 'a' * 10,
                  'verification': {'status': 'passed', 'revision': 'a' * 40,
                                   'checks': ['source_hashes', 'page', 'task_api', 'fresh_pi_cpu']}}
        result.update(changes)
        heal.heal_done({'payload': json.dumps({'incident': iid})}, result)

    def row(self, pid):
        return dict(core.q('SELECT * FROM crawl_pages WHERE id=?', (pid,), one=True))

    def test_verified_fix_retries_only_reported_current_failed_pages(self):
        for status in ('done', 'failed', 'limited'):
            with self.subTest(status=status):
                context = self.collection(status)
                iid = self.incident(context)
                retry = self.page(context, iid)
                keep = [self.page(context, iid, state='done'), self.page(context, iid, generation=6),
                        self.page(context, iid + 100), self.page(self.collection(), iid)]
                before = {pid: self.row(pid) for pid in keep}
                self.done(iid)
                row = self.row(retry)
                self.assertEqual((row['state'], row['failures'], row['retry_at'], row['heal_reported'], row['error']),
                                 ('queued', 0, None, 0, ''))
                self.assertEqual(core.q('SELECT status,error FROM crawls WHERE id=?', (context['crawl_id'],), one=True)['status'], 'queued')
                self.assertEqual({pid: self.row(pid) for pid in keep}, before)

    def test_stopped_deleted_and_replaced_roots_are_not_revived(self):
        for status in ('stopped', 'deleted', 'replaced', 'other-owner'):
            with self.subTest(status=status):
                context = self.collection(status if status in ('stopped', 'running', 'queued') else 'failed')
                iid = self.incident(context)
                pid = self.page(context, iid)
                before = self.row(pid)
                if status == 'deleted':
                    core.q('DELETE FROM crawls WHERE id=?', (context['crawl_id'],))
                elif status == 'replaced':
                    core.q("UPDATE crawls SET url_key='new root' WHERE id=?", (context['crawl_id'],))
                elif status == 'other-owner':
                    core.q("UPDATE crawls SET owner='another-owner' WHERE id=?", (context['crawl_id'],))
                self.done(iid)
                self.assertEqual(self.row(pid), before)

    def test_active_tasks_queue_repaired_pages_without_replacing_parent_state(self):
        for status in ('running', 'queued'):
            with self.subTest(status=status):
                context = self.collection(status)
                iid = self.incident(context)
                pid = self.page(context, iid)
                core.q("UPDATE crawls SET current_url='another page in flight' WHERE id=?", (context['crawl_id'],))
                before = dict(core.q('SELECT * FROM crawls WHERE id=?', (context['crawl_id'],), one=True))
                self.done(iid)
                self.assertEqual((self.row(pid)['state'], self.row(pid)['heal_reported']), ('queued', 0))
                self.assertEqual(dict(core.q('SELECT * FROM crawls WHERE id=?', (context['crawl_id'],), one=True)), before)

    def test_fix_after_worker_reads_empty_queue_is_not_lost_at_completion(self):
        context = self.collection('queued')
        iid = self.incident(context)
        pid = self.page(context, iid)
        real_query, callbacks = crawl.q, []

        def query(sql, args=(), one=False):
            result = real_query(sql, args, one)
            if 'SELECT MIN(retry_at)' in sql and not callbacks:
                callbacks.append(iid)
                # The worker has already observed no ready or delayed pages;
                # the callback inserts retry work before its completion write.
                self.done(iid)
            return result

        with patch.object(crawl, 'q', side_effect=query), patch.object(crawl, '_extract') as fetch:
            crawl.run_crawl(context['crawl_id'])
            fetch.assert_not_called()
        self.assertEqual(callbacks, [iid])
        self.assertEqual((self.row(pid)['state'], self.row(pid)['heal_reported']), ('queued', 0))
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (context['crawl_id'],), one=True)['status'], 'queued')

    def test_unverified_or_unrequested_retry_does_not_change_pages(self):
        for changes in ({'verification': {}}, {'retry': False}, {'state': 'diagnosed'}):
            context = self.collection()
            iid = self.incident(context)
            pid = self.page(context, iid)
            before = self.row(pid)
            self.done(iid, **changes)
            self.assertEqual(self.row(pid), before)

    def test_completed_page_is_not_requeued_by_duplicate_heal_callback(self):
        context = self.collection()
        iid = self.incident(context)
        pid = self.page(context, iid)
        self.done(iid)
        core.q("UPDATE crawl_pages SET state='done' WHERE id=?", (pid,))
        core.q("UPDATE crawls SET status='done' WHERE id=?", (context['crawl_id'],))
        self.done(iid)
        self.assertEqual(self.row(pid)['state'], 'done')
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (context['crawl_id'],), one=True)['status'], 'done')

    def test_stop_committed_while_callback_waits_cannot_be_overwritten(self):
        context = self.collection()
        iid = self.incident(context)
        pid = self.page(context, iid)
        with core.db_lock:
            thread = threading.Thread(target=self.done, args=(iid,))
            thread.start()
            core.DB.execute("UPDATE crawls SET status='stopped' WHERE id=?", (context['crawl_id'],))
            core.DB.commit()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.row(pid)['state'], 'failed')
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (context['crawl_id'],), one=True)['status'], 'stopped')

    def test_crawl_incident_signature_isolates_owners_roots_and_tasks(self):
        first = self.collection()
        contexts = [first, {**first, 'owner': 'another-owner'}, {**first, 'url_key': 'another-root'},
                    {**first, 'crawl_id': first['crawl_id'] + 100}]
        with patch.object(heal, 'HEAL', True), patch.object(heal, 'send'):
            for context in contexts:
                heal.report('crawl', '目录采集', 'ReadTimeout: failed after 3 attempts', context=context)
            heal.report('crawl', '目录采集', 'ReadTimeout: failed after 3 attempts', context=first)
        incidents = core.q('SELECT sig,count,context FROM incidents')
        self.assertEqual(len(incidents), 4)
        self.assertEqual(sorted(row['count'] for row in incidents), [1, 1, 1, 2])
        self.assertEqual(heal.signature('web', 'same error', '', first), heal.signature('web', 'same error', '', contexts[1]))

    def test_immediate_post_fix_failure_starts_second_heal_and_stops_at_try_limit(self):
        context = self.collection('running')
        pid = self.page(context, 0, state='running')

        def exhausted():
            core.q("UPDATE crawls SET status='running' WHERE id=?", (context['crawl_id'],))
            core.q("UPDATE crawl_pages SET state='running',failures=2 WHERE id=?", (pid,))
            crawl._page_failed(context['crawl_id'], 7, self.row(pid), requests.ReadTimeout('synthetic persistent failure'))

        with patch.object(heal, 'HEAL', True), patch.object(heal, 'HEAL_TRIES', 2), patch.object(heal, 'HEAL_PER_DAY', 6):
            exhausted()
            iid = self.row(pid)['heal_reported']
            self.assertGreater(iid, 0)
            for expected_tries, expected_state in ((2, 'healing'), (2, 'back')):
                core.q("UPDATE tasks SET state='done' WHERE kind='heal' AND target=?", (f'incident:{iid}',))
                core.q("UPDATE crawls SET status='failed' WHERE id=?", (context['crawl_id'],))
                self.done(iid)
                self.assertEqual(self.row(pid)['heal_reported'], 0)
                exhausted()
                incident = core.q('SELECT state,tries,healed FROM incidents WHERE id=?', (iid,), one=True)
                self.assertLess(time.time() - incident['healed'], 60)
                self.assertEqual((incident['tries'], incident['state']), (expected_tries, expected_state))
                task = core.q("SELECT state FROM tasks WHERE kind='heal' AND target=?", (f'incident:{iid}',), one=True)
                self.assertEqual(task['state'], 'queued' if expected_state == 'healing' else 'done')
            self.assertEqual(self.row(pid)['state'], 'failed')

    def test_post_fix_failure_stays_unresolved_when_daily_heal_allowance_is_full(self):
        context = self.collection('running')
        pid = self.page(context, 0, state='running')
        with patch.object(heal, 'HEAL', True), patch.object(heal, 'HEAL_PER_DAY', 6):
            core.q('UPDATE crawl_pages SET failures=2 WHERE id=?', (pid,))
            crawl._page_failed(context['crawl_id'], 7, self.row(pid), requests.ReadTimeout('persistent failure'))
            iid = self.row(pid)['heal_reported']
            for index in range(5):
                core.q("INSERT INTO tasks(kind,target,state,payload,created) VALUES('heal',?,'done','{}',?)",
                       (f'quota:{index}', time.time()))
            core.q("UPDATE tasks SET state='done' WHERE target=?", (f'incident:{iid}',))
            core.q("UPDATE crawls SET status='failed' WHERE id=?", (context['crawl_id'],))
            self.done(iid)
            core.q("UPDATE crawls SET status='running' WHERE id=?", (context['crawl_id'],))
            core.q('UPDATE crawl_pages SET failures=2 WHERE id=?', (pid,))
            crawl._page_failed(context['crawl_id'], 7, self.row(pid), requests.ReadTimeout('persistent failure'))
        incident = core.q('SELECT state,tries FROM incidents WHERE id=?', (iid,), one=True)
        self.assertEqual((incident['state'], incident['tries']), ('back', 1))
        self.assertEqual(core.q("SELECT COUNT(*) n FROM tasks WHERE kind='heal' AND state='queued'", one=True)['n'], 0)

    def test_request_incidents_filter_owner_and_source_before_limit_and_honor_reveal(self):
        own = self.incident(self.collection())
        generic = self.incident({}, kind='web')
        adult = self.incident(self.collection(classification='adult'))
        unknown = self.incident(self.collection(classification='unknown'))
        foreign = self.incident(self.collection(owner='another-owner'))
        self.assertEqual({row['id'] for row in heal.recent(20)}, {own, generic, adult, unknown, foreign})
        headers = {'Cookie': f'{web.DEVICE_COOKIE}={self.owner}'}
        with core.app.test_request_context('/api/board', headers=headers):
            web.identify()
            self.assertEqual({row['id'] for row in heal.recent(2)}, {own, generic})
        tokens = {'test-reveal': {'owner': self.owner, 'expires': time.time() + 300, 'reveal': True}}
        headers['X-Privacy-Token'] = 'test-reveal'
        with patch.object(web, 'privacy_tokens', tokens):
            with core.app.test_request_context('/api/board', headers=headers):
                web.identify()
                self.assertEqual({row['id'] for row in heal.recent(20)}, {own, generic, adult, unknown})
            tokens['test-reveal']['expires'] = 0
            with core.app.test_request_context('/api/board', headers=headers):
                web.identify()
                self.assertEqual({row['id'] for row in heal.recent(20)}, {own, generic})


if __name__ == '__main__':
    unittest.main()
