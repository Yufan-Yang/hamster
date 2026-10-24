"""Owner/private-safe collection diagnostics and targeted retry, without network."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('health_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, web  # noqa: E402


class CrawlHealthTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    item = fixtures.HomeSearchTests.item
    job = fixtures.HomeSearchTests.job

    def page(self, cid=None, state='failed', failures=3, generation=None, url=None, heal=0):
        cid = self.a if cid is None else cid
        if generation is None:
            generation = core.q('SELECT generation FROM crawls WHERE id=?', (cid,), one=True)['generation']
        url = url or f'https://example.test/problem/{time.time_ns()}'
        core.q('INSERT INTO crawl_pages(crawl_id,url,url_key,kind,state,generation,failures,error,retry_at,heal_reported) '
               "VALUES(?,?,?,'detail',?,?,?,?,?,?)", (cid, url, crawl.identity(url), state, generation, failures,
                                                      'Synthetic timeout at ' + url, 1234 if failures else None, heal))
        return core.q('SELECT MAX(id) id FROM crawl_pages', one=True)['id']

    def row(self, pid):
        return dict(core.q('SELECT * FROM crawl_pages WHERE id=?', (pid,), one=True))

    def issues(self, **params):
        response = self.alice.get(f'/api/crawls/{self.a}/issues', query_string=params)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def retry(self, **body):
        generation = core.q('SELECT generation FROM crawls WHERE id=?', (self.a,), one=True)['generation']
        return self.alice.post(f'/api/crawls/{self.a}/retry-failed', json={'generation': generation, **body})

    def test_counts_use_ended_pages_not_transient_failures_and_paginate(self):
        self.assertIsNone(self.issues()['health']['failure_rate'])
        for _ in range(3):
            self.page(state='done', failures=0)
        failures = [self.page() for _ in range(2)]
        retry = self.page(state='queued', failures=1)
        active_retry = self.page(state='running', failures=2)
        for _ in range(2):
            self.page(state='queued', failures=0)
        self.page(state='running', failures=0)
        self.page(generation=999)
        data = self.issues(limit=2)
        self.assertEqual(data['health'], {'completed': 3, 'failed': 2, 'retrying': 2, 'pending': 2,
                                         'running': 2, 'failure_rate': .4})
        self.assertEqual((data['total'], data['failed'], data['retrying']), (4, 2, 2))
        self.assertEqual([item['id'] for item in data['items']], failures)
        self.assertEqual([item['id'] for item in self.issues(limit=2, offset=2)['items']], [retry, active_retry])
        self.assertTrue(data['can_retry'])
        self.assertFalse(data['privacy_locked'])
        self.assertNotIn('owner', data)

    def test_foreign_owner_admin_and_deleted_task_are_not_visible(self):
        self.page()
        for client in (self.bob, self.admin):
            self.assertEqual(client.get(f'/api/crawls/{self.a}/issues').status_code, 404)
            self.assertEqual(client.post(f'/api/crawls/{self.a}/retry-failed', json={'generation': 1}).status_code, 404)
        self.alice.post(f'/api/crawls/{self.a}/delete', json={})
        self.assertEqual(self.alice.get(f'/api/crawls/{self.a}/issues').status_code, 404)
        self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/retry-failed', json={'generation': 1}).status_code, 404)

    def test_hidden_source_requires_current_owner_reveal_for_diagnostics_and_retry(self):
        pid = self.page()
        tokens = {'valid': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True},
                  'menu': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': False},
                  'foreign': {'owner': 'user:bob', 'expires': time.time() + 60, 'reveal': True},
                  'expired': {'owner': 'user:alice', 'expires': time.time() - 1, 'reveal': True}}
        with patch.object(web, 'privacy_tokens', tokens):
            for classification in ('adult', 'unknown'):
                core.q('UPDATE crawls SET source_classification=? WHERE id=?', (classification, self.a))
                for token in ('', 'menu', 'foreign', 'expired'):
                    headers = {'X-Privacy-Token': token}
                    response = self.alice.get(f'/api/crawls/{self.a}/issues', headers=headers)
                    data = response.json
                    if token in ('foreign', 'expired'):
                        self.assertEqual(response.status_code, 403)
                        self.assertNotIn('items', data)
                        continue
                    self.assertEqual((data['items'], data['total']), ([], 0))
                    self.assertIsNone(data['health'])
                    self.assertTrue(data['privacy_locked'])
                    self.assertNotIn('problem/', json.dumps(data))
                    self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/retry-failed', json={'generation': 0}, headers=headers).status_code, 403)
            data = self.alice.get(f'/api/crawls/{self.a}/issues', headers={'X-Privacy-Token': 'valid'}).json
            self.assertEqual(data['items'][0]['id'], pid)
            response = self.alice.post(f'/api/crawls/{self.a}/retry-failed', json={'generation': data['generation']},
                                       headers={'X-Privacy-Token': 'valid'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['requeued'], 1)

    def test_manual_tag_and_alias_hidden_pages_are_filtered_before_counts_or_retry(self):
        jobs = [self.job('user:alice', 'Hidden manually'), self.job('user:alice', 'Hidden tag', tags=['private'])]
        hidden = []
        for job in jobs:
            iid = self.item('user:alice', 'Hidden lesson', [self.a])
            core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (job, iid))
            item = core.q('SELECT page_url FROM crawl_items WHERE id=?', (iid,), one=True)
            hidden.append(self.page(url=item['page_url']))
            alias = f'https://example.test/mirror/{iid}'
            core.q('INSERT INTO crawl_aliases(owner,url_key,item_id) VALUES(?,?,?)', ('user:alice', alias, iid))
            hidden.append(self.page(url=alias))
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '["private"]', json.dumps([jobs[0]])))
        visible = self.page()
        before = {pid: self.row(pid) for pid in hidden}
        data = self.issues()
        self.assertEqual((data['total'], data['health']['failed']), (1, 1))
        self.assertEqual([item['id'] for item in data['items']], [visible])
        self.assertEqual(self.retry(ids=[visible, hidden[0]]).status_code, 400)
        self.assertEqual(self.row(visible)['state'], 'failed')
        self.assertEqual(self.retry().json['requeued'], 1)
        self.assertEqual({pid: self.row(pid) for pid in hidden}, before)

    def test_reveal_revocation_after_scope_never_exposes_or_retries_hidden_page(self):
        jid = self.job('user:alice', 'Private imported job')
        iid = self.item('user:alice', 'Private item', [self.a])
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (jid, iid))
        url = core.q('SELECT page_url FROM crawl_items WHERE id=?', (iid,), one=True)['page_url']
        pid = self.page(url=url)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '[]', json.dumps([jid])))
        generation = core.q('SELECT generation FROM crawls WHERE id=?', (self.a,), one=True)['generation']
        original = crawl._issue_scope
        def revoked(row):
            scope = original(row)
            web.privacy_tokens.pop('revealed', None)
            return scope
        for method in ('get', 'post'):
            tokens = {'revealed': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True}}
            with patch.object(web, 'privacy_tokens', tokens), patch.object(crawl, '_issue_scope', side_effect=revoked):
                headers = {'X-Privacy-Token': 'revealed'}
                response = self.alice.get(f'/api/crawls/{self.a}/issues', headers=headers) if method == 'get' else (
                    self.alice.post(f'/api/crawls/{self.a}/retry-failed', headers=headers, json={'generation': generation}))
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(url, response.get_data(as_text=True))
            self.assertEqual(self.row(pid)['state'], 'failed')

    def test_only_current_failed_pages_reset_and_active_task_settings_stay_intact(self):
        for status in ('queued', 'running'):
            core.q('UPDATE crawls SET status=?,max_pages=351,refresh_hours=6,current_url=? WHERE id=?',
                   (status, 'https://example.test/inflight', self.a))
            retry = self.page(heal=42)
            keep = [self.page(state='done', failures=0), self.page(state='queued', failures=1),
                    self.page(state='running', failures=1), self.page(generation=999), self.page(cid=self.b)]
            before = {pid: self.row(pid) for pid in keep}
            parent = dict(core.q('SELECT * FROM crawls WHERE id=?', (self.a,), one=True))
            response = self.retry(ids=[retry])
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['requeued'], 1)
            row = self.row(retry)
            self.assertEqual((row['state'], row['failures'], row['error'], row['retry_at'], row['heal_reported']),
                             ('queued', 0, '', None, 0))
            self.assertEqual({pid: self.row(pid) for pid in keep}, before)
            self.assertEqual(dict(core.q('SELECT * FROM crawls WHERE id=?', (self.a,), one=True)), parent)

    def test_finished_task_can_retry_without_raising_page_limit_but_stopped_stays_stopped(self):
        for status in ('done', 'failed', 'limited'):
            core.q('UPDATE crawls SET status=?,max_pages=351,refresh_hours=6 WHERE id=?', (status, self.a))
            pid = self.page()
            response = self.retry(ids=[pid])
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json['crawl']['status'], response.json['crawl']['max_pages'],
                              response.json['crawl']['refresh_hours']), ('queued', 351, 6))
        self.alice.post(f'/api/crawls/{self.a}/stop', json={})
        pid = self.page()
        self.assertFalse(self.issues()['can_retry'])
        self.assertEqual(self.retry(ids=[pid]).status_code, 409)
        self.assertEqual(self.row(pid)['state'], 'failed')
        self.assertEqual(core.q('SELECT status FROM crawls WHERE id=?', (self.a,), one=True)['status'], 'stopped')

    def test_stale_generation_and_stop_delete_races_do_not_requeue(self):
        pid = self.page()
        self.assertEqual(self.retry(generation=999).status_code, 409)
        for action in ('stop', 'delete'):
            original = crawl._issue_scope
            def changed(row):
                scope = original(row)
                if action == 'stop':
                    core.q("UPDATE crawls SET status='stopped',generation=generation+1 WHERE id=?", (self.a,))
                else:
                    core.q('DELETE FROM crawls WHERE id=?', (self.a,))
                return scope
            with patch.object(crawl, '_issue_scope', side_effect=changed):
                response = self.retry()
                self.assertEqual(response.status_code, 409 if action == 'stop' else 404)
            self.assertEqual(self.row(pid)['state'], 'failed')

    def test_heal_state_is_scoped_and_invalid_requests_do_not_mutate(self):
        row = core.q('SELECT * FROM crawls WHERE id=?', (self.a,), one=True)
        context = {'owner': 'user:alice', 'crawl_id': self.a, 'url_key': row['url_key']}
        core.q("INSERT INTO incidents(sig,kind,label,title,error,context,tasks,state,first,last) "
               "VALUES('health-test','crawl','目录采集','private title','private error',?,'[]','healing',?,?)",
               (json.dumps(context), time.time(), time.time()))
        iid = core.q('SELECT MAX(id) id FROM incidents', one=True)['id']
        pid = self.page(heal=iid)
        data = self.issues()
        self.assertEqual(data['items'][0]['heal'], {'state': 'healing'})
        self.assertNotIn('private title', json.dumps(data))
        core.q("INSERT INTO tasks(kind,target,state,payload,created,updated) VALUES('heal',?,'failed','{}',?,?)",
               (f'incident:{iid}', time.time(), time.time()))
        self.assertEqual(self.issues()['items'][0]['heal'], {'state': 'error'})
        context['owner'] = 'user:bob'
        core.q('UPDATE incidents SET context=? WHERE id=?', (json.dumps(context), iid))
        self.assertIsNone(self.issues()['items'][0]['heal'])
        gen = row['generation']
        for body in ({}, {'generation': True}, {'generation': gen, 'ids': []}, {'generation': gen, 'ids': [True]},
                     {'generation': gen, 'ids': [pid, 999999]}):
            response = self.alice.post(f'/api/crawls/{self.a}/retry-failed', json=body)
            self.assertEqual(response.status_code, 400, response.json)
        for query in ('?limit=0', '?limit=101', '?offset=-1', '?offset=1.5'):
            self.assertEqual(self.alice.get(f'/api/crawls/{self.a}/issues' + query).status_code, 400)
        self.assertEqual(self.row(pid)['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
