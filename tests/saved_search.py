"""Saved-query ownership, privacy, validation and login merge regressions."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

spec = importlib.util.spec_from_file_location('saved_search_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, migrations, saved_search, web  # noqa: E402


class SavedSearchTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection

    def create(self, client=None, **body):
        return (client or self.alice).post('/api/searches', json={'name': 'Python tutorials', 'q': 'Python*入门',
                                                                 'mode': 'wildcard', **body})

    def seed(self, owner, query, private=False, name=None):
        core.q('INSERT INTO saved_searches(owner,name,query,mode,private,created,updated) VALUES(?,?,?,?,?,?,?)',
               (owner, name or query, query, 'exact', private, time.time(), time.time()))
        return core.q('SELECT MAX(id) id FROM saved_searches', one=True)['id']

    def tokens(self):
        return {'valid': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True},
                'menu': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': False},
                'expired': {'owner': 'user:alice', 'expires': time.time() - 1, 'reveal': True},
                'foreign': {'owner': 'user:bob', 'expires': time.time() + 60, 'reveal': True}}

    def test_create_duplicate_rename_get_and_delete_without_storing_results(self):
        first = self.create()
        self.assertEqual(first.status_code, 200, first.json)
        self.assertTrue(first.json['created'])
        item = first.json['item']
        self.assertEqual(set(item), {'id', 'name', 'q', 'mode', 'private', 'created', 'updated'})
        self.assertFalse(item['private'])
        again = self.create(name='New duplicate name')
        self.assertEqual(again.json['created'], False)
        self.assertEqual(again.json['item'], item)
        changed = self.alice.post(f"/api/searches/{item['id']}/rename", json={'name': 'Updated name'})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json['item']['name'], 'Updated name')
        fetched = self.alice.get(f"/api/searches/{item['id']}")
        self.assertEqual(fetched.json['item'], changed.json['item'])
        self.assertEqual(fetched.headers['Cache-Control'], 'no-store')
        listed = self.alice.get('/api/searches')
        self.assertEqual((listed.json['total'], listed.json['storage_owner']), (1, 'user:alice'))
        self.assertEqual(listed.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.alice.post(f"/api/searches/{item['id']}/delete", json={}).status_code, 200)
        self.assertEqual(self.alice.get(f"/api/searches/{item['id']}").status_code, 404)

    def test_owner_isolation_includes_admin_anonymous_and_stale_owner_header(self):
        ident = self.create().json['item']['id']
        anonymous = core.app.test_client()
        anonymous.set_cookie(web.DEVICE_COOKIE, 'd' * 32)
        for client in (self.bob, self.admin, anonymous):
            self.assertEqual(client.get('/api/searches').json['total'], 0)
            self.assertEqual(client.get(f'/api/searches/{ident}').status_code, 404)
            self.assertEqual(client.post(f'/api/searches/{ident}/rename', json={'name': 'other'}).status_code, 404)
            self.assertEqual(client.post(f'/api/searches/{ident}/delete', json={}).status_code, 404)
        self.assertEqual(self.create(anonymous).status_code, 200)
        self.assertEqual(anonymous.get('/api/searches').json['storage_owner'], 'd' * 32)
        self.assertEqual(self.alice.post('/api/searches', json={'name': 'x', 'q': 'q', 'mode': 'exact'},
                                         headers={'X-Storage-Owner': 'user:bob'}).status_code, 409)
        self.assertEqual(self.alice.get('/api/searches').json['total'], 1)

    def test_private_flag_is_server_derived_and_all_operations_require_reveal(self):
        public = self.create().json['item']['id']
        with patch.object(web, 'privacy_tokens', self.tokens()):
            response = self.alice.post('/api/searches', json={'name': 'Private topic', 'q': 'Python*入门', 'mode': 'wildcard'},
                                       headers={'X-Privacy-Token': 'valid'})
            self.assertEqual(response.status_code, 200)
            secret = response.json['item']['id']
            self.assertTrue(response.json['item']['private'])
            self.assertNotEqual(secret, public)
            for token in ('', 'menu', 'expired', 'foreign'):
                headers = {'X-Privacy-Token': token}
                response = self.alice.get('/api/searches', headers=headers)
                data = response.json
                if token in ('expired', 'foreign'):
                    self.assertEqual(response.status_code, 403)
                else:
                    self.assertEqual((data['total'], [item['id'] for item in data['items']]), (1, [public]))
                self.assertNotIn('Private topic', str(data))
                expected = 403 if token in ('expired', 'foreign') else 404
                self.assertEqual(self.alice.get(f'/api/searches/{secret}', headers=headers).status_code, expected)
                self.assertEqual(self.alice.post(f'/api/searches/{secret}/rename', json={'name': 'x'}, headers=headers).status_code, expected)
                self.assertEqual(self.alice.post(f'/api/searches/{secret}/delete', json={}, headers=headers).status_code, expected)
            self.assertEqual(self.alice.get('/api/searches', headers={'X-Privacy-Token': 'valid'}).json['total'], 2)
            bad = self.alice.post('/api/searches', json={'name': 'x', 'q': 'secret', 'mode': 'exact', 'private': False},
                                  headers={'X-Privacy-Token': 'valid'})
            self.assertEqual(bad.status_code, 400)

    def test_expired_or_revoked_token_at_entry_never_creates_public_record(self):
        with patch.object(web, 'privacy_tokens', self.tokens()):
            for token in ('expired', 'revoked', 'foreign'):
                response = self.alice.post('/api/searches', json={'name': 'Private name', 'q': 'Private query', 'mode': 'exact'},
                                           headers={'X-Privacy-Token': token})
                self.assertEqual(response.status_code, 403)
            self.assertEqual(core.q('SELECT COUNT(*) n FROM saved_searches', one=True)['n'], 0)
            ordinary = self.alice.post('/api/searches', json={'name': 'Public name', 'q': 'Public query', 'mode': 'exact'},
                                       headers={'X-Privacy-Token': 'menu'})
            self.assertEqual(ordinary.status_code, 200)
            self.assertFalse(ordinary.json['item']['private'])

    def test_private_intent_survives_reveal_toggle_without_overriding_server_flag(self):
        body = {'name': 'Private name', 'q': 'Private query', 'mode': 'exact', 'expected_private': True}
        with patch.object(web, 'privacy_tokens', self.tokens()):
            # The token remains valid, but another request closed hidden content
            # after the save form was opened. Never silently downgrade the save.
            closed = self.alice.post('/api/searches', json=body, headers={'X-Privacy-Token': 'menu'})
            self.assertEqual(closed.status_code, 403)
            self.assertEqual(core.q('SELECT COUNT(*) n FROM saved_searches', one=True)['n'], 0)
            body['expected_private'] = False
            revealed = self.alice.post('/api/searches', json=body, headers={'X-Privacy-Token': 'valid'})
            self.assertEqual(revealed.status_code, 200)
            self.assertTrue(revealed.json['item']['private'])
            for value in (0, 1, 'true', None):
                body['expected_private'] = value
                self.assertEqual(self.alice.post('/api/searches', json=body).status_code, 400)

    def test_validation_supports_each_mode_and_only_compiles_regex(self):
        valid = [('exact', '猫' * 256), ('wildcard', '猫*?'), ('regex', '(a+)+$'), ('ai', '适合零基础的编程课，排除广告')]
        for mode, query in valid:
            self.assertEqual(self.create(mode=mode, q=query).status_code, 200)
        for body in ({'q': ''}, {'q': '   '}, {'q': 'a' * 257}, {'mode': 'other'}, {'mode': []},
                     {'mode': 'regex', 'q': '['}, {'mode': 'regex', 'q': 'a' * 129}, {'name': ' '},
                     {'name': 'x' * 61}, {'name': 'line\nbreak'}, {'q': False}, {'q': '\x00'}):
            self.assertEqual(self.create(**body).status_code, 400, body)
        for body in (None, [], {}, {'name': 'a', 'q': 'x', 'mode': 'exact', 'results': []}):
            self.assertEqual(self.alice.post('/api/searches', json=body).status_code, 400)
        for query in ('?limit=0', '?limit=101', '?offset=-1', '?offset=1.5', '?limit=true'):
            self.assertEqual(self.alice.get('/api/searches' + query).status_code, 400)

    def test_capacity_is_per_owner_and_duplicates_still_work_at_capacity(self):
        first = self.create().json['item']
        for index in range(99):
            self.seed('user:alice', f'query-{index}')
        self.assertEqual(self.create(q='another').status_code, 409)
        repeated = self.create(name='overwrite forbidden')
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json['created'])
        self.assertEqual(repeated.json['item']['name'], first['name'])
        self.assertEqual(self.create(self.bob, q='another').status_code, 200)
        self.alice.post(f"/api/searches/{first['id']}/delete", json={})
        self.assertEqual(self.create(q='another').status_code, 200)

    def test_login_merges_queries_and_preserves_private_duplicates_and_large_lists(self):
        device = 'e' * 32
        anonymous = core.app.test_client()
        anonymous.set_cookie(web.DEVICE_COOKIE, device)
        public = self.seed('user:alice', 'same', name='Account name')
        self.seed(device, 'same', name='Anonymous name')
        private = self.seed(device, 'same', private=True, name='Private name')
        for index in range(104):
            self.seed(device, f'imported-{index}')
        core.q('UPDATE users SET pw=? WHERE name=?', (generate_password_hash('test-password'), 'alice'))
        response = anonymous.post('/api/login', json={'name': 'alice', 'password': 'test-password'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM saved_searches WHERE owner=?', (device,), one=True)['n'], 0)
        self.assertEqual(core.q('SELECT name FROM saved_searches WHERE id=?', (public,), one=True)['name'], 'Account name')
        self.assertEqual(core.q('SELECT private FROM saved_searches WHERE id=?', (private,), one=True)['private'], 1)
        pages = [anonymous.get('/api/searches', query_string={'offset': offset, 'limit': 50}).json for offset in (0, 50, 100)]
        self.assertEqual([page['total'] for page in pages], [105] * 3)
        self.assertEqual([page['more'] for page in pages], [True, True, False])
        self.assertEqual(len({item['id'] for page in pages for item in page['items']}), 105)
        self.assertEqual(self.create(anonymous, q='over-cap').status_code, 409)
        self.assertEqual(self.create(anonymous, q='same', mode='exact').status_code, 200)
        with patch.object(web, 'privacy_tokens', self.tokens()):
            self.assertEqual(anonymous.get('/api/searches', headers={'X-Privacy-Token': 'valid'}).json['total'], 106)

    def test_revoked_reveal_during_reads_never_returns_private_query_or_name(self):
        ident = self.seed('user:alice', 'private-query', private=True, name='private-name')
        original = saved_search.q
        def revoke(sql, args=(), one=False):
            value = original(sql, args, one)
            web.privacy_tokens.pop('valid', None)
            return value
        for url in ('/api/searches', f'/api/searches/{ident}'):
            with patch.object(web, 'privacy_tokens', self.tokens()), patch.object(saved_search, 'q', side_effect=revoke):
                response = self.alice.get(url, headers={'X-Privacy-Token': 'valid'})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn('private-query', response.get_data(as_text=True))
                self.assertNotIn('private-name', response.get_data(as_text=True))

    def test_revoked_reveal_during_create_rename_delete_rolls_back(self):
        ident = self.seed('user:alice', 'private-query', private=True, name='private-name')
        original = saved_search._guard
        for action, body in (('', {'name': 'new private', 'q': 'new-private-query', 'mode': 'ai'}),
                             (f'/{ident}/rename', {'name': 'changed'}), (f'/{ident}/delete', {})):
            calls = []
            def revoked(owner, revealed):
                calls.append(1)
                if len(calls) == 2:
                    web.privacy_tokens.pop('valid', None)
                return original(owner, revealed)
            with patch.object(web, 'privacy_tokens', self.tokens()), patch.object(saved_search, '_guard', side_effect=revoked):
                response = self.alice.post('/api/searches' + action, json=body, headers={'X-Privacy-Token': 'valid'})
                self.assertEqual(response.status_code, 403)
            rows = core.q('SELECT * FROM saved_searches WHERE owner=?', ('user:alice',))
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['name'], rows[0]['private']), ('private-name', 1))

    def test_migration_is_repeatable_without_losing_existing_shortcuts(self):
        ident = self.create().json['item']['id']
        migrations._saved_searches(core.DB)
        migrations._saved_searches(core.DB)
        core.DB.commit()
        self.assertEqual(self.alice.get(f'/api/searches/{ident}').status_code, 200)
        self.assertGreaterEqual(core.DB.execute('PRAGMA user_version').fetchone()[0], 30)


if __name__ == '__main__':
    unittest.main()
