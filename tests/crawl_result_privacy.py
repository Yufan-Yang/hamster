"""Single-task results obey the same privacy boundary as homepage/library search."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('result_privacy_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, web


class CrawlResultPrivacyTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    item = fixtures.HomeSearchTests.item
    job = fixtures.HomeSearchTests.job
    response = fixtures.HomeSearchTests.response

    def private_imports(self):
        items, jobs = [], []
        for title, tags in (('Public lesson', []), ('Manual lesson', []), ('Tagged lesson', ['private'])):
            jid = self.job('user:alice', title, tags=tags)
            iid = self.item('user:alice', title, [self.a, self.a2])
            core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (jid, iid))
            items.append(iid)
            jobs.append(jid)
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', ('user:alice', '["private"]', json.dumps([jobs[1]])))
        return items, jobs

    def test_hidden_imports_filtered_before_single_task_search_ai_and_export(self):
        items, _ = self.private_imports()
        for cid in (self.a, self.a2):
            for mode, query in (('exact', 'lesson'), ('wildcard', '*lesson'), ('regex', 'lesson$')):
                data = self.response(self.alice, f'/api/crawls/{cid}', q=query, mode=mode)
                self.assertEqual((data['total'], [r['id'] for r in data['items']]), (1, [items[0]]))
            with patch.object(crawl, 'search_items', side_effect=lambda rows, *args: {'items': rows, 'interpretation': 'test'}) as search:
                data = self.response(self.alice, f'/api/crawls/{cid}', q='lesson', mode='ai')
                self.assertEqual([r['id'] for r in search.call_args.args[0]], [items[0]])
                self.assertEqual(data['total'], 1)
            exported = self.alice.get(f'/api/crawls/{cid}/export').get_data(as_text=True)
            self.assertEqual(exported.splitlines(), [f'https://cdn.test/clip-{items[0]}.mp4'])

    def test_direct_hidden_import_ids_are_rejected_without_partial_import(self):
        items, _ = self.private_imports()
        with patch.object(crawl.pipeline, 'add_job_ex') as add:
            response = self.alice.post(f'/api/crawls/{self.a}/import', json={'ids': items})
            self.assertEqual(response.status_code, 400)
            add.assert_not_called()
            response = self.alice.post(f'/api/crawls/{self.a}/import', json={'all': True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json['added'], response.json['duplicates']), (0, 1))
            add.assert_not_called()

    def test_archived_manual_and_tagged_imports_remain_hidden(self):
        items, jobs = self.private_imports()
        for jid in jobs[1:]:
            self.assertEqual(self.alice.post(f'/api/jobs/{jid}/delete').status_code, 200)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM job_trash', one=True)['n'], 2)
        self.assertEqual([r['id'] for r in self.response(self.alice, f'/api/crawls/{self.a}')['items']], [items[0]])
        self.assertEqual([r['id'] for r in self.response(self.alice, '/api/crawls/search')['items']], [items[0]])
        self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/import', json={'ids': [items[2]]}).status_code, 400)

    def test_only_current_owner_valid_reveal_exposes_hidden_imports(self):
        items, _ = self.private_imports()
        tokens = {
            'menu-only': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': False},
            'expired': {'owner': 'user:alice', 'expires': time.time() - 1, 'reveal': True},
            'foreign': {'owner': 'user:bob', 'expires': time.time() + 60, 'reveal': True},
            'valid': {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True},
        }
        with patch.object(web, 'privacy_tokens', tokens):
            data = self.alice.get(f'/api/crawls/{self.a}', headers={'X-Privacy-Token': 'menu-only'}).json
            self.assertEqual([r['id'] for r in data['items']], [items[0]])
            for token in ('expired', 'foreign'):
                denied = self.alice.get(f'/api/crawls/{self.a}', headers={'X-Privacy-Token': token})
                self.assertEqual(denied.status_code, 403)
                self.assertNotIn('items', denied.json)
            headers = {'X-Privacy-Token': 'valid'}
            data = self.alice.get(f'/api/crawls/{self.a}', headers=headers).json
            self.assertEqual([r['id'] for r in data['items']], items)
            self.assertEqual(len(self.alice.get(f'/api/crawls/{self.a}/export', headers=headers).get_data(as_text=True).splitlines()), 3)
            self.assertEqual(self.alice.post(f'/api/crawls/{self.a}/import', json={'ids': [items[1]]}, headers=headers).status_code, 200)
            self.assertEqual(self.admin.get(f'/api/crawls/{self.a}', headers=headers).status_code, 403)
            self.assertEqual(self.bob.get(f'/api/crawls/{self.a}', headers=headers).status_code, 403)
            self.assertEqual(self.admin.get(f'/api/crawls/{self.a}').status_code, 404)
            self.assertEqual(self.bob.get(f'/api/crawls/{self.a}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
