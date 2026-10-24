"""Privacy remains enforced after videos move to the recycle bin; no network."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import privacy_selection
from shiguang import core, pipeline, trash, web


class TrashPrivacyTests(unittest.TestCase):
    job = privacy_selection.PrivacySelectionTests.job
    select = privacy_selection.PrivacySelectionTests.select

    def setUp(self):
        privacy_selection.PrivacySelectionTests.setUp(self)
        for module, name, value in ((trash, 'MEDIA', Path(self.temp.name)),
                                     (pipeline, 'MEDIA', Path(self.temp.name))):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def archive(self, title, *, manual=False, tags=None, owner=None):
        jid = self.job(title=title, owner=owner, files='[]', analysis=json.dumps({'tags': tags or []}))
        core.q('UPDATE jobs SET url=?,key=? WHERE id=?',
               (f'https://example.test/watch/{jid}', f'test:{jid}', jid))
        if manual:
            self.assertEqual(self.select([jid]).status_code, 200)
        self.assertEqual(trash.trash_job(jid, self.owner)[0], 'trashed')
        tid = core.q('SELECT id FROM job_trash WHERE job_id=?', (jid,), one=True)['id']
        return jid, tid

    def listing(self, headers=None):
        response = self.client.get('/api/trash', headers=headers or {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.get_json()

    def assert_hidden(self, tid, headers=None):
        data = self.listing(headers)
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)
        self.assertFalse(data['has_more'])
        for action in ('restore', 'delete'):
            response = self.client.post(f'/api/trash/{tid}/{action}', headers=headers or {})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json(), {'error': '回收站记录不存在'})
        self.assertIsNotNone(core.q('SELECT 1 FROM job_trash WHERE id=?', (tid,), one=True))

    def assert_denied(self, tid, headers):
        for method, url in [('get', '/api/trash'), ('post', f'/api/trash/{tid}/restore'), ('post', f'/api/trash/{tid}/delete')]:
            response = getattr(self.client, method)(url, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIsNotNone(core.q('SELECT 1 FROM job_trash WHERE id=?', (tid,), one=True))

    def test_manual_hidden_stays_private_with_missing_unrevealed_and_expired_tokens(self):
        _, tid = self.archive('Private selection', manual=True)
        self.assert_hidden(tid)
        self.assert_denied(tid, {'X-Privacy-Token': 'invalid'})
        self.assert_hidden(tid, self.headers)
        self.tokens[self.token]['reveal'] = True
        self.tokens[self.token]['expires'] = 0
        self.assert_denied(tid, self.headers)

    def test_revealed_token_from_another_account_does_not_expose_archives(self):
        _, tid = self.archive('Owner private selection', manual=True)
        self.tokens[self.token].update(owner='another-owner', reveal=True)
        self.assert_denied(tid, self.headers)

    def test_tag_hidden_snapshot_honors_current_tags_aliases_and_case(self):
        _, tid = self.archive('Private tag', tags=['Alias'])
        core.kv_set('tag_aliases', {'Alias': 'Secret'})
        web.privacy_set(self.owner, ['SECRET'], [])
        self.assert_hidden(tid)
        # Clearing a privacy category also affects already archived videos.
        web.privacy_set(self.owner, [], [])
        self.assertEqual([row['id'] for row in self.listing()['items']], [tid])

    def test_valid_reveal_allows_restore_and_relocks_restored_job(self):
        jid, tid = self.archive('Restore privately', manual=True)
        self.tokens[self.token]['reveal'] = True
        data = self.listing(self.headers)
        self.assertEqual([row['id'] for row in data['items']], [tid])
        self.assertEqual(data['total'], 1)
        response = self.client.post(f'/api/trash/{tid}/restore', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['job_id'], jid)
        self.assertEqual(self.client.get('/api/jobs').get_json()['jobs'], [])
        self.assertIsNone(core.q('SELECT 1 FROM job_trash WHERE id=?', (tid,), one=True))

    def test_valid_reveal_allows_permanent_delete(self):
        _, tid = self.archive('Purge privately', manual=True)
        self.tokens[self.token]['reveal'] = True
        response = self.client.post(f'/api/trash/{tid}/delete', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertIsNone(core.q('SELECT 1 FROM job_trash WHERE id=?', (tid,), one=True))

    def test_owner_scope_still_applies_with_reveal(self):
        own, own_tid = self.archive('Own archive')
        _, foreign_tid = self.archive('Foreign archive', owner='another-owner')
        self.tokens[self.token]['reveal'] = True
        data = self.listing(self.headers)
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)
        self.assertEqual([row['job_id'] for row in self.listing()['items']], [own])
        self.assertEqual(self.client.post(f'/api/trash/{own_tid}/restore', headers=self.headers).status_code, 404)
        for action in ('restore', 'delete'):
            self.assertEqual(self.client.post(f'/api/trash/{foreign_tid}/{action}', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post(f'/api/trash/{own_tid}/restore').status_code, 200)

    def test_hidden_rows_do_not_consume_page_limit_or_leak_into_total(self):
        # More than one batch of newer hidden rows must not displace an older,
        # visible entry or make has_more reveal the existence of hidden records.
        _, visible_tid = self.archive('Visible last row')
        web.privacy_set(self.owner, ['secret'], [])
        snapshot = json.dumps({'title': 'Hidden batch', 'analysis': {'tags': ['secret']}})
        stamp = time.time() + 1
        with core.db_lock:
            core.DB.executemany('INSERT INTO job_trash(owner,job_id,snapshot,deleted_at) VALUES (?,?,?,?)',
                               [(self.owner, 10000 + i, snapshot, stamp) for i in range(1005)])
            core.DB.commit()
        data = self.listing()
        self.assertEqual([row['id'] for row in data['items']], [visible_tid])
        self.assertEqual(data['total'], 1)
        self.assertFalse(data['has_more'])
        self.tokens[self.token]['reveal'] = True
        revealed = self.listing(self.headers)
        self.assertEqual(len(revealed['items']), 1000)
        self.assertEqual(revealed['total'], 1005)
        self.assertTrue(revealed['has_more'])

    def test_background_expiry_still_cleans_hidden_entries_without_a_token(self):
        _, tid = self.archive('Expired hidden', manual=True)
        now = time.time()
        core.q('UPDATE job_trash SET deleted_at=? WHERE id=?', (now - trash.RETENTION_SECONDS, tid))
        self.assert_hidden(tid)
        self.assertEqual(trash.cleanup_expired(now=now), 1)
        self.assertIsNone(core.q('SELECT 1 FROM job_trash WHERE id=?', (tid,), one=True))


if __name__ == '__main__':
    unittest.main()
