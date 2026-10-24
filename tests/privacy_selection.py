"""Manual video privacy, using a fresh database and real Flask routes; no network."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend
from shiguang import core, intelligence, web


class PrivacySelectionTests(unittest.TestCase):
    job = backend.BackendReliability.job

    def setUp(self):
        backend.BackendReliability.setUp(self)
        self.token = 'privacy-selection-test'
        self.headers = {'X-Privacy-Token': self.token}
        self.tokens = {self.token: {'owner': self.owner, 'expires': time.time() + 300, 'reveal': False}}
        patcher = patch.object(web, 'privacy_tokens', self.tokens)
        patcher.start()
        self.addCleanup(patcher.stop)

    def select(self, ids, hidden=True, **kwargs):
        return self.client.post('/api/privacy/ids', json={'ids': ids, 'hidden': hidden},
                                headers=kwargs.pop('headers', self.headers), **kwargs)

    def prefs(self):
        return web.privacy_get(self.owner)

    def jobs(self, query='', headers=None):
        result = self.client.get('/api/jobs' + query, headers=headers or {})
        self.assertEqual(result.status_code, 200)
        return result.get_json()

    def test_manual_hide_without_tags_affects_list_search_detail_and_counts(self):
        hidden = self.job(title='Secret tutorial', analysis='{"library":"Videos","folder":"Secret"}')
        visible = self.job(title='Visible lesson', analysis='{"library":"Videos","folder":"Learning"}')
        self.assertEqual(self.select([hidden]).status_code, 200)
        self.assertEqual(self.prefs()['tags'], [])
        for query in ('', '?q=Secret', f'?ids={hidden}&only=1', f'?id={hidden}', '?grouped=1'):
            data = self.jobs(query)
            self.assertNotIn(hidden, [job['id'] for job in data['jobs']])
            self.assertNotIn('Secret', data['cats'])
        data = self.jobs()
        self.assertEqual([job['id'] for job in data['jobs']], [visible])
        self.assertFalse(data['jobs'][0]['manual_hidden'])
        self.assertTrue(data['jobs'][0]['privacy_editable'])

    def test_reveal_is_temporary_and_expiring_token_relocks(self):
        hidden = self.job(title='Private lesson')
        self.select([hidden])
        response = self.client.post('/api/privacy', json={'reveal': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        shown = self.jobs(headers=self.headers)['jobs']
        self.assertEqual([job['id'] for job in shown], [hidden])
        self.assertTrue(shown[0]['manual_hidden'])
        self.assertTrue(shown[0]['hidden'])
        self.assertEqual(self.jobs()['jobs'], [])
        self.client.post('/api/privacy', json={'reveal': False}, headers=self.headers)
        self.assertEqual(self.jobs(headers=self.headers)['jobs'], [])
        self.tokens[self.token]['reveal'] = True
        self.tokens[self.token]['expires'] = 0
        self.assertEqual(self.client.get('/api/jobs', headers=self.headers).status_code, 403)
        self.assertEqual(self.jobs()['jobs'], [])
        self.assertEqual(self.select([hidden], False).status_code, 403)
        self.assertEqual(self.prefs()['ids'], [hidden])

    def test_missing_invalid_and_other_owner_token_are_rejected(self):
        own = self.job()
        for headers in ({}, {'X-Privacy-Token': 'unknown'}):
            response = self.select([own], headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()['error'], 'locked')
        self.tokens[self.token]['owner'] = 'another-owner'
        self.assertEqual(self.select([own]).status_code, 403)
        self.assertEqual(self.prefs()['ids'], [])
        self.assertEqual(self.client.get('/api/privacy', headers=self.headers).status_code, 403)

    def test_batch_with_foreign_or_missing_ids_is_atomic(self):
        first, second = self.job(), self.job()
        foreign = self.job(owner='another-owner')
        self.select([first])
        for ids in ([second, foreign], [second, 999999], [first, foreign]):
            response = self.select(ids)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(self.prefs()['ids'], [first])
        self.assertEqual(self.select([first, foreign], False).status_code, 404)
        self.assertEqual(self.prefs()['ids'], [first])

    def test_tags_and_pin_survive_select_and_tag_settings_preserve_ids(self):
        first, second = self.job(), self.job()
        core.q('INSERT INTO privacy(owner,tags,ids,pin) VALUES (?,?,?,?)',
               (self.owner, '["hidden"]', json.dumps([first]), 'existing-pin'))
        self.assertEqual(self.select([second]).status_code, 200)
        self.assertEqual(self.prefs()['tags'], ['hidden'])
        self.client.post('/api/privacy', json={'tags': ['other'], 'reveal': True}, headers=self.headers)
        row = core.q('SELECT * FROM privacy WHERE owner=?', (self.owner,), one=True)
        self.assertEqual(json.loads(row['ids']), [first, second])
        self.assertEqual(json.loads(row['tags']), ['other'])
        self.assertEqual(row['pin'], 'existing-pin')
        self.client.post('/api/privacy', json={'reveal': False}, headers=self.headers)
        self.assertEqual(self.prefs()['ids'], [first, second])

    def test_unselecting_manual_item_still_obeys_hidden_tags(self):
        own = self.job(analysis='{"tags":["hidden"]}')
        web.privacy_set(self.owner, ['hidden'], [])
        self.select([own])
        self.assertEqual(self.select([own], False).get_json()['ids'], [])
        self.assertEqual(self.jobs()['jobs'], [])
        self.tokens[self.token]['reveal'] = True
        shown = self.jobs(headers=self.headers)['jobs'][0]
        self.assertTrue(shown['hidden'])
        self.assertFalse(shown['manual_hidden'])

    def test_manual_info_contains_only_current_owner_items_and_no_cache(self):
        own = self.job(title='Fallback title', analysis='{"title":"Preferred title"}')
        foreign = self.job(owner='another-owner', title='Foreign secret')
        web.privacy_set(self.owner, [], [own, foreign, 999999])
        response = self.client.get('/api/privacy', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['manual_items'], [{'id': own, 'title': 'Preferred title'}])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/api/privacy').status_code, 403)

    def test_admin_can_only_edit_own_jobs(self):
        foreign = self.job()
        core.q("INSERT INTO users(name,pw,admin,created) VALUES ('privacy-admin','test',1,?)", (time.time(),))
        core.q('INSERT OR REPLACE INTO devices(id,user) VALUES (?,?)', (self.owner, 'privacy-admin'))
        with self.client.session_transaction() as session:
            session['user'] = 'privacy-admin'
        self.owner = 'user:privacy-admin'
        self.tokens[self.token]['owner'] = self.owner
        own = self.job()
        data = {job['id']: job for job in self.jobs()['jobs']}
        self.assertTrue(data[own]['privacy_editable'])
        self.assertFalse(data[foreign]['privacy_editable'])
        self.assertEqual(self.select([own, foreign]).status_code, 404)
        self.assertEqual(self.prefs()['ids'], [])
        self.assertEqual(self.select([own]).status_code, 200)
        self.assertEqual([job['id'] for job in self.jobs()['jobs']], [foreign])

    def test_hidden_ids_chain_excludes_manual_items_from_ai_candidates_and_subs(self):
        core.q("INSERT INTO subs(id,owner,platform,name) VALUES (1,?,'youtube','Secret uploader')", (self.owner,))
        hidden = self.job(title='Python private', source='sub:1')
        visible = self.job(title='Python public')
        self.select([hidden])
        with core.app.test_request_context('/api/jobs', headers={'Cookie': f'{web.DEVICE_COOKIE}={self.owner}'}):
            web.identify()
            self.assertEqual(web.hidden_ids(), {hidden})
            self.assertEqual(web.hidden_subs(), {1})
            candidates = intelligence.candidates('Python')
            self.assertEqual([record['job_id'] for record in candidates], [visible])
        self.assertEqual(self.jobs()['subs'], [])

    def test_concurrent_additions_do_not_lose_a_selection(self):
        first, second = self.job(), self.job()

        def add(jid):
            client = core.app.test_client()
            client.set_cookie(web.DEVICE_COOKIE, self.owner)
            return client.post('/api/privacy/ids', json={'ids': [jid], 'hidden': True}, headers=self.headers).status_code

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(list(workers.map(add, (first, second))), [200, 200])
        self.assertEqual(self.prefs()['ids'], [first, second])

    def test_invalid_inputs_and_duplicate_ids(self):
        own = self.job()
        invalid = [None, [], {}, {'ids': [own], 'hidden': 'false'}, {'ids': own, 'hidden': True},
                   {'ids': [], 'hidden': True}, {'ids': [True], 'hidden': True},
                   {'ids': [str(own)], 'hidden': True}, {'ids': [-1], 'hidden': True},
                   {'ids': [2 ** 100], 'hidden': True}, {'ids': [own] * 1001, 'hidden': True}]
        for body in invalid:
            with self.subTest(body=str(body)[:80]):
                response = self.client.post('/api/privacy/ids', json=body, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.prefs()['ids'], [])
        self.assertEqual(self.select([own, own]).get_json()['ids'], [own])
        self.assertEqual(self.select([own], False).get_json()['ids'], [])
        for body in ([], {'reveal': 'false'}, {'tags': 'wrong'}):
            self.assertEqual(self.client.post('/api/privacy', json=body, headers=self.headers).status_code, 400)


if __name__ == '__main__':
    unittest.main()
