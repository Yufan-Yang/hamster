"""Source privacy and recycle-bin ledger lifecycle, with isolated synthetic rows."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trash_privacy
from shiguang import core, crawl, trash


class TrashSourceTests(unittest.TestCase):
    setUp = trash_privacy.TrashPrivacyTests.setUp
    job = trash_privacy.TrashPrivacyTests.job
    select = trash_privacy.TrashPrivacyTests.select
    listing = trash_privacy.TrashPrivacyTests.listing
    assert_hidden = trash_privacy.TrashPrivacyTests.assert_hidden

    def sourced(self, classification='adult', owner=None, jid=None, slug='source'):
        owner = owner or self.owner
        if jid is None:
            jid = self.job(owner=owner, title=slug, files='[]', source='crawl:999')
        else:
            core.q("INSERT INTO jobs(id,owner,url,key,status,title,files,source) VALUES(?,?,?,?,'done',?,'[]','crawl:999')",
                   (jid, owner, f'https://example.test/video/{slug}', slug, slug))
        core.q('UPDATE jobs SET url=?,key=? WHERE id=?', (f'https://example.test/video/{slug}', slug, jid))
        key = f'https://example.test/root/{slug}'
        core.q('INSERT INTO crawl_sources(owner,url_key,classification,checked) VALUES(?,?,?,1)',
               (owner, key, classification))
        core.q('INSERT INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)', (owner, jid, key))
        return jid, key

    def archive(self, jid):
        self.assertEqual(trash.trash_job(jid, self.owner)[0], 'trashed')
        return core.q('SELECT MAX(id) id FROM job_trash WHERE job_id=?', (jid,), one=True)['id']

    def keys(self, jid, owner=None):
        return {row['url_key'] for row in core.q('SELECT url_key FROM crawl_source_jobs WHERE owner=? AND job_id=?',
                                                (owner or self.owner, jid))}

    def test_source_hidden_archives_require_reveal_and_restore_stays_hidden(self):
        jid, key = self.sourced()
        tid = self.archive(jid)
        self.assert_hidden(tid)
        self.assert_hidden(tid, self.headers)  # unlocked menu alone is insufficient
        self.tokens[self.token]['reveal'] = True
        self.assertEqual([row['id'] for row in self.listing(self.headers)['items']], [tid])
        response = self.client.post(f'/api/trash/{tid}/restore', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['job_id'], jid)
        self.assertEqual(self.keys(jid), {key})
        self.assertEqual(self.client.get('/api/jobs').get_json()['jobs'], [])

    def test_restore_collision_copies_only_original_sources_and_purge_cleans_owner(self):
        jid, original = self.sourced(slug='original')
        tid = self.archive(jid)
        _, replacement = self.sourced('general', owner='other-owner', jid=jid, slug='replacement')
        restored = trash.restore(tid, self.owner)
        self.assertNotEqual(restored, jid)
        self.assertEqual(self.keys(restored), {original})
        self.assertEqual(self.keys(jid), set())
        self.assertEqual(self.keys(jid, 'other-owner'), {replacement})
        self.assertIn(restored, crawl.hidden_source_job_ids(self.owner))
        self.assertNotIn(jid, crawl.hidden_source_job_ids('other-owner'))
        trash.purge(self.archive(restored), self.owner)
        self.assertEqual(self.keys(restored), set())
        self.assertEqual(self.keys(jid, 'other-owner'), {replacement})

    def test_multiple_snapshots_preserve_ledger_without_overriding_first_classification(self):
        jid, original = self.sourced(slug='first')
        first = self.archive(jid)
        _, later = self.sourced('general', jid=jid, slug='later')
        second = self.archive(jid)
        restored = trash.restore(first, self.owner)
        self.assertNotEqual(restored, jid)
        self.assertEqual(self.keys(restored), {original})
        self.assertEqual(self.keys(jid), {original, later})
        self.assertIn(restored, crawl.hidden_source_job_ids(self.owner))
        trash.purge(second, self.owner)
        self.assertEqual(self.keys(jid), set())
        self.assertEqual(self.keys(restored), {original})

    def test_legacy_snapshot_copies_source_ledger_on_collision(self):
        jid, key = self.sourced(classification='unknown', slug='legacy')
        tid = self.archive(jid)
        snapshot = json.loads(core.q('SELECT snapshot FROM job_trash WHERE id=?', (tid,), one=True)['snapshot'])
        snapshot.pop('_trash_source_keys')
        core.q('UPDATE job_trash SET snapshot=? WHERE id=?', (json.dumps(snapshot), tid))
        self.sourced('general', owner='other-owner', jid=jid, slug='active')
        restored = trash.restore(tid, self.owner)
        self.assertEqual(self.keys(restored), {key})
        self.assertIn(restored, crawl.hidden_source_job_ids(self.owner))


if __name__ == '__main__':
    unittest.main()
