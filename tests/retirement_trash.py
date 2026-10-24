"""Retirement/restore preserves each archive's own classification across ID reuse."""
import importlib.util
import json
from pathlib import Path
import time
import unittest

spec = importlib.util.spec_from_file_location('retire_trash_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import core, crawl, directory_retired as retired, sources, trash, web  # noqa: E402


class RetirementTrashTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def imported(self, classification, *, ledger=True):
        owner = 'user:alice'
        cid = self.collection(owner, 'root-' + classification, classification)
        jid = self.job(owner, 'Saved ' + classification + ' lesson')
        core.q('UPDATE jobs SET source=? WHERE id=?', (f'crawl:{cid}', jid))
        if ledger:
            key = core.q('SELECT url_key FROM crawls WHERE id=?', (cid,), one=True)['url_key']
            core.q('INSERT INTO crawl_sources(owner,url_key,classification) VALUES(?,?,?)', (owner, key, classification))
            core.q('INSERT INTO crawl_source_jobs(owner,job_id,url_key) VALUES(?,?,?)', (owner, jid, key))
        return jid

    def legacy_archive(self, jid):
        row = dict(core.q('SELECT * FROM jobs WHERE id=?', (jid,), one=True))
        row['_trash_source_keys'] = [r['url_key'] for r in core.q('SELECT url_key FROM crawl_source_jobs WHERE job_id=?', (jid,))]
        row['_trash_download_context'] = '{"referer":"https://example.test/old-root"}'
        core.q('INSERT INTO job_trash(owner,job_id,snapshot,deleted_at) VALUES(?,?,?,?)',
               ('user:alice', jid, json.dumps(row), time.time()))
        core.q('DELETE FROM jobs WHERE id=?', (jid,))
        return core.q('SELECT max(id) id FROM job_trash', one=True)['id']

    def cleanup(self):
        with sources.transaction() as db:
            retired.clear_cache(db)
            retired.clear_cache(db)

    def manual_ids(self):
        return set(web.privacy_get('user:alice')['ids'])

    def occupy(self, jid):
        core.q("INSERT INTO jobs(id,owner,url,key,title,analysis,files,status) VALUES(?,'user:alice',?,?,?,'{}','[]','done')",
               (jid, 'https://example.test/unrelated', 'example.test/unrelated', 'Unrelated public lesson'))

    def test_general_archive_restores_as_general_without_reviving_root_cache(self):
        jid = self.imported('general')
        tid = self.legacy_archive(jid)
        self.cleanup()
        snapshot = json.loads(core.q('SELECT snapshot FROM job_trash WHERE id=?', (tid,), one=True)['snapshot'])
        self.assertIs(snapshot['_trash_private'], False)
        self.assertEqual(snapshot['_trash_source_keys'], [])
        self.assertNotIn('_trash_download_context', snapshot)
        restored = trash.restore(tid, 'user:alice')
        self.assertEqual(restored, jid)
        self.assertNotIn(restored, self.manual_ids() | crawl.hidden_source_job_ids('user:alice'))
        self.assertEqual(core.q('SELECT count(*) n FROM crawl_source_jobs', one=True)['n'], 0)

    def test_legacy_private_archive_restored_with_new_id_stays_private_and_public_id_stays_public(self):
        jid = self.imported('adult', ledger=False)
        tid = self.legacy_archive(jid)
        self.cleanup()
        self.assertNotIn(jid, self.manual_ids())  # An archived ID must not classify a new live job.
        self.occupy(jid)
        restored = trash.restore(tid, 'user:alice')
        self.assertNotEqual(restored, jid)
        self.assertIn(restored, self.manual_ids())
        self.assertNotIn(jid, self.manual_ids())
        public_ids = {row['id'] for row in self.alice.get('/api/jobs').json['jobs']}
        self.assertIn(jid, public_ids)
        self.assertNotIn(restored, public_ids)

    def test_manual_hidden_archive_owns_its_flag_before_and_after_collision(self):
        jid = self.job('user:alice', 'Manually hidden lesson')
        web.privacy_set('user:alice', [], [jid])
        trash.trash_job(jid, 'user:alice')
        tid = core.q('SELECT max(id) id FROM job_trash', one=True)['id']
        self.assertNotIn(jid, self.manual_ids())
        self.cleanup()
        self.occupy(jid)
        restored = trash.restore(tid, 'user:alice')
        self.assertIn(restored, self.manual_ids())
        self.assertNotIn(jid, self.manual_ids())

    def test_legacy_manual_snapshot_captures_flag_before_losing_old_preferences(self):
        jid = self.job('user:alice', 'Legacy manually hidden lesson')
        web.privacy_set('user:alice', [], [jid])
        tid = self.legacy_archive(jid)
        self.cleanup()
        snapshot = json.loads(core.q('SELECT snapshot FROM job_trash WHERE id=?', (tid,), one=True)['snapshot'])
        self.assertIs(snapshot['_trash_private'], True)
        self.assertNotIn(jid, self.manual_ids())
        self.assertEqual(self.alice.get('/api/trash').json['total'], 0)
        self.assertEqual(trash.restore(tid, 'user:alice'), jid)
        self.assertIn(jid, self.manual_ids())


if __name__ == '__main__':
    unittest.main()
