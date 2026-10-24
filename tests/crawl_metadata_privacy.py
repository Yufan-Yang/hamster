"""Independent root/provenance privacy regression; synthetic data, no network or models."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('metadata_privacy_fixtures', Path(__file__).with_name('sources.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, sources, web  # noqa: E402


class CrawlMetadataPrivacyTests(unittest.TestCase):
    setUp = fixtures.SourceTests.setUp
    client = fixtures.SourceTests.client
    collection = fixtures.SourceTests.collection
    item = fixtures.SourceTests.item
    job = fixtures.SourceTests.job
    incremental = fixtures.SourceTests.incremental
    discover = fixtures.SourceTests.discover

    def private_root(self, classification='adult'):
        marker = 'SECRET_' + classification.upper()
        nonce = core.q('SELECT COALESCE(MAX(id),0)+1 n FROM crawls', one=True)['n']
        cid = self.collection('user:alice', marker + '_ROOT_' + str(nonce), classification)
        core.q('UPDATE crawls SET title=?,error=?,current_url=? WHERE id=?',
               (marker + '_TITLE', marker + '_ERROR', 'https://secret.test/' + marker + '_CURRENT', cid))
        self.incremental(cid)
        iid = self.discover(cid, title=marker + '_VIDEO')
        row = core.q('SELECT generation FROM crawls WHERE id=?', (cid,), one=True)
        core.q("INSERT INTO crawl_pages(crawl_id,url_key,url,kind,state,generation,error,failures) "
               "VALUES(?,?,?,'detail','failed',?,?,3)",
               (cid, marker + '_KEY', 'https://secret.test/' + marker + '_PAGE', row['generation'], marker + '_PAGE_ERROR'))
        return cid, iid

    def token(self, key='show', owner='user:alice', reveal=True, expired=False):
        web.privacy_tokens[key] = {'owner': owner, 'reveal': reveal, 'expires': time.time() + (-1 if expired else 60)}
        return {'X-Privacy-Token': key}

    def assert_opaque(self, response):
        self.assertNotIn('SECRET_', response.get_data(as_text=True))

    def test_locked_adult_and_unknown_roots_hide_metadata_and_counts_everywhere(self):
        private = [self.private_root(kind)[0] for kind in ('adult', 'unknown')]
        for headers in ({}, self.token('menu', reveal=False)):
            listing = self.alice.get('/api/crawls', headers=headers)
            self.assertEqual(listing.status_code, 200)
            self.assert_opaque(listing)
            self.assertTrue(set(private).isdisjoint(row['id'] for row in listing.json['crawls']))
            for cid in private:
                for suffix in ('', '/issues', '/export'):
                    response = self.alice.get(f'/api/crawls/{cid}{suffix}', headers=headers)
                    self.assert_opaque(response)
                    if response.status_code == 200 and response.is_json:
                        self.assertEqual(response.json.get('items', []), [])
                        self.assertEqual(response.json.get('total', 0), 0)
            for path in ('/api/crawls/search', '/api/sources', '/api/inbox?state=all'):
                self.assert_opaque(self.alice.get(path, headers=headers))

    def test_locked_private_actions_cannot_modify_or_return_task_metadata(self):
        with patch.object(crawl.pipeline, 'add_job_ex') as add:
            for action in ('stop', 'resume', 'refresh', 'settings', 'retry-failed', 'import', 'delete'):
                with self.subTest(action=action):
                    cid, iid = self.private_root()
                    before = dict(core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True))
                    body = ({'refresh_hours': 2} if action == 'settings' else
                            {'generation': before['generation']} if action == 'retry-failed' else
                            {'ids': [iid]} if action == 'import' else {})
                    response = self.alice.post(f'/api/crawls/{cid}/{action}', json=body)
                    self.assertIn(response.status_code, (403, 404))
                    self.assert_opaque(response)
                    self.assertEqual(dict(core.q('SELECT * FROM crawls WHERE id=?', (cid,), one=True)), before)
            add.assert_not_called()

    def test_valid_reveal_recovers_metadata_but_expired_foreign_and_other_owners_do_not(self):
        cid, _ = self.private_root()
        headers = self.token()
        response = self.alice.get(f'/api/crawls/{cid}', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('SECRET_ADULT_TITLE', response.get_data(as_text=True))
        self.assertEqual(len(response.json['items']), 1)
        for invalid in (self.token('expired', expired=True), self.token('foreign', owner='user:bob')):
            for path in ('/api/crawls', f'/api/crawls/{cid}', f'/api/crawls/{cid}/issues', '/api/sources', '/api/inbox'):
                response = self.alice.get(path, headers=invalid)
                self.assertEqual(response.status_code, 403, path)
                self.assert_opaque(response)
        for client in (self.bob, self.admin):
            self.assert_opaque(client.get('/api/crawls'))
            response = client.get(f'/api/crawls/{cid}', headers=headers)
            self.assertIn(response.status_code, (403, 404))
            self.assert_opaque(response)

    def test_shared_general_result_cannot_expose_private_root_or_detail_provenance(self):
        private, _ = self.private_root()
        iid = self.item('user:alice', 'Public shared lesson', [self.a, private])
        core.q('UPDATE crawl_items SET sources=?,page_url=? WHERE id=?',
               (json.dumps(['https://example.test/catalog/a', 'https://secret.test/SECRET_HISTORY_ROOT/']),
                'https://secret.test/SECRET_HISTORY_DETAIL/', iid))
        for path in ('/api/crawls/search?q=shared', f'/api/crawls/{self.a}?q=shared', f'/api/crawls/{self.a}'):
            response = self.alice.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(iid, [row['id'] for row in response.json['items']])
            self.assert_opaque(response)
        revealed = self.alice.get(f'/api/crawls/{self.a}?q=shared', headers=self.token())
        self.assertIn('SECRET_HISTORY_DETAIL', revealed.get_data(as_text=True))
        for mode in ('exact', 'wildcard', 'regex'):
            for path in ('/api/crawls/search', f'/api/crawls/{self.a}'):
                response = self.alice.get(path, query_string={'q': 'SECRET_HISTORY', 'mode': mode})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((response.json['items'], response.json['total']), ([], 0))
                self.assert_opaque(response)

    def test_sources_and_inbox_recheck_root_and_privacy_after_snapshot(self):
        self.incremental()
        iid = self.discover(title='SECRET_SNAPSHOT_TITLE')
        jid = self.job('user:alice', 'SECRET_SNAPSHOT_JOB')
        core.q('UPDATE crawl_items SET job_id=? WHERE id=?', (jid, iid))
        original = sources._snapshot
        for path in ('/api/sources', '/api/inbox?state=all'):
            for change in ('classification', 'url_key', 'manual'):
                with self.subTest(path=path, change=change):
                    old = dict(core.q('SELECT * FROM crawls WHERE id=?', (self.a,), one=True))
                    def mutate_after_snapshot(*args, **kwargs):
                        result = original(*args, **kwargs)
                        if change == 'classification':
                            core.q("UPDATE crawls SET source_classification='adult' WHERE id=?", (self.a,))
                        elif change == 'url_key':
                            core.q("UPDATE crawls SET url_key='SECRET_CHANGED_ROOT',url='https://secret.test/changed' WHERE id=?", (self.a,))
                        else:
                            core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET ids=excluded.ids',
                                   ('user:alice', '[]', json.dumps([jid])))
                        return result
                    try:
                        with patch.object(sources, '_snapshot', side_effect=mutate_after_snapshot):
                            response = self.alice.get(path)
                        self.assertEqual(response.status_code, 403)
                        self.assertNotIn('items', response.json)
                        self.assertNotIn('total', response.json)
                        self.assertNotIn('unread', response.json)
                        self.assert_opaque(response)
                    finally:
                        core.q('UPDATE crawls SET source_classification=?,url_key=?,url=? WHERE id=?',
                               (old['source_classification'], old['url_key'], old['url'], self.a))
                        core.q('DELETE FROM privacy WHERE owner=?', ('user:alice',))

    def test_sources_and_inbox_drop_private_snapshot_when_token_revoked(self):
        self.private_root()
        original = sources._snapshot
        for path in ('/api/sources', '/api/inbox?state=all'):
            headers = self.token()
            def revoke(*args, **kwargs):
                result = original(*args, **kwargs)
                web.privacy_tokens.pop('show')
                return result
            with patch.object(sources, '_snapshot', side_effect=revoke):
                response = self.alice.get(path, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('items', response.json)
            self.assertNotIn('total', response.json)
            self.assert_opaque(response)


if __name__ == '__main__':
    unittest.main()
