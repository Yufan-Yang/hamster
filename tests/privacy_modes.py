"""Public and unlocked libraries are disjoint owner-scoped views."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('privacy_mode_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl_search, web  # noqa: E402


class PrivacyModeTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def unlock(self, owner='user:alice', token='mode-test'):
        web.privacy_tokens[token] = {'owner': owner, 'reveal': True, 'expires': time.time() + 60}
        return {'X-Privacy-Token': token}

    def listing(self, client=None, headers=None, **params):
        response = (client or self.alice).get('/api/jobs', headers=headers or {}, query_string={'compact': 1, **params})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.json

    def test_manual_tags_aliases_and_inherited_sources_form_disjoint_views(self):
        public = self.job('user:alice', 'Public lesson')
        manual = self.job('user:alice', 'Manual lesson')
        tagged = self.job('user:alice', 'Tagged lesson', tags=['private-alias'])
        inherited = self.job('user:alice', 'Inherited lesson')
        adult = self.collection('user:alice', 'adult-root', 'adult')
        core.q('UPDATE jobs SET source=? WHERE id=?', (f'crawl:{adult}', inherited))
        core.kv_set('tag_aliases', {'private-alias': 'private'})
        web.privacy_set('user:alice', ['private'], [manual])
        plain = self.listing(grouped=1)
        private = self.listing(headers=self.unlock(), grouped=1)
        self.assertEqual({row['id'] for row in plain['jobs']}, {public})
        self.assertEqual({row['id'] for row in private['jobs']}, {manual, tagged, inherited})
        self.assertEqual(plain['cats'], {'Talks': 1})
        self.assertEqual(private['cats'], {'Talks': 3})
        self.assertTrue(all(row['hidden'] for row in private['jobs']))
        self.assertTrue(private['privacy']['revealed'])
        self.assertFalse(plain['privacy']['revealed'])

    def test_empty_private_view_does_not_fall_back_to_public_or_resume_cards(self):
        for index in range(12):
            jid = self.job('user:alice', f'Public lesson {index}')
            core.q('INSERT INTO watch(owner,job_id,pos,dur,done,updated) VALUES(?,?,?,?,?,?)',
                   ('user:alice', jid, 30, 600, 0, time.time()))
        private = self.listing(headers=self.unlock(), grouped=1, limit=1)
        self.assertEqual(private['jobs'], [])
        self.assertEqual(private['cats'], {})
        self.assertFalse(private['more'])
        self.assertEqual(private['reading'], [])
        self.assertEqual(private['notes'], 0)
        self.assertEqual(private['books'], 0)
        self.assertEqual(self.listing(headers=self.unlock(), q='lesson', mode='exact')['search_total'], 0)

    def test_all_search_modes_and_counts_use_only_current_mode(self):
        public = self.job('user:alice', 'Shared public lesson')
        private = self.job('user:alice', 'Shared private lesson')
        foreign = self.job('user:bob', 'Shared foreign lesson')
        web.privacy_set('user:alice', [], [private])
        headers = self.unlock()
        for mode, query in (('exact', 'Shared'), ('wildcard', 'Shared*'), ('regex', r'Shared\s')):
            result = self.listing(headers=headers, mode=mode, q=query, limit=1)
            self.assertEqual([row['id'] for row in result['jobs']], [private])
            self.assertEqual(result['search_total'], 1)
            self.assertFalse(result['more'])
            self.assertEqual([row['id'] for row in self.listing(mode=mode, q=query)['jobs']], [public])
        received = []
        def search(items, query, mode, owner):
            received.append((owner, {row['id'] for row in items}))
            return {'items': items, 'interpretation': 'Privacy fixture'}
        with patch.object(crawl_search, 'search_items', side_effect=search):
            self.listing(headers=headers, mode='ai', q='Shared')
        self.assertEqual(received, [('user:alice', {private})])
        self.assertNotIn(foreign, received[0][1])

    def test_admin_privacy_view_only_contains_own_private_jobs(self):
        own = self.job('user:admin', 'Admin private lesson')
        self.job('user:admin', 'Admin public lesson')
        foreign = self.job('user:alice', 'Foreign private lesson')
        web.privacy_set('user:admin', [], [own])
        web.privacy_set('user:alice', [], [foreign])
        result = self.listing(client=self.admin, headers=self.unlock('user:admin'))
        self.assertEqual([row['id'] for row in result['jobs']], [own])
        self.assertEqual(result['cats'], {'Talks': 1})

    def test_subscription_progress_counts_only_visible_mode_members(self):
        core.q("INSERT INTO subs(id,owner,platform,key,name,url) VALUES(1,'user:alice','youtube','youtube:@mixed','Mixed source','https://youtube.com/@mixed')")
        private = self.job('user:alice', 'Private followed lesson')
        public = self.job('user:alice', 'Public followed lesson')
        for jid in (private, public):
            core.q("UPDATE jobs SET source='sub:1' WHERE id=?", (jid,))
        web.privacy_set('user:alice', [], [private])
        result = self.listing(headers=self.unlock(), sub=1)
        self.assertEqual([row['id'] for row in result['jobs']], [private])
        self.assertEqual(len(result['subs']), 1)
        self.assertEqual(result['subs'][0]['done'], 1)
        self.assertEqual(result['subs'][0]['active'], 0)
        self.assertEqual(self.listing()['subs'], [])

    def test_foreign_expired_and_mid_request_revoked_privacy_are_rejected(self):
        private = self.job('user:alice', 'Private lesson')
        web.privacy_set('user:alice', [], [private])
        headers = self.unlock()
        self.assertEqual(self.bob.get('/api/jobs', headers=headers).status_code, 403)
        web.privacy_tokens['mode-test']['expires'] = 0
        self.assertEqual(self.alice.get('/api/jobs', headers=headers).status_code, 403)
        self.unlock()
        original = web.library.card_media
        def revoke(jobs):
            result = original(jobs)
            web.privacy_set('user:alice', [], [])
            return result
        with patch.object(web.library, 'card_media', side_effect=revoke):
            result = self.alice.get('/api/jobs?compact=1', headers=headers)
        self.assertEqual(result.status_code, 403)
        self.assertNotIn('jobs', result.json)
        self.assertNotIn('cats', result.json)


if __name__ == '__main__':
    unittest.main()
