"""Review regressions: missing media and independently recycled jobs sharing an old ID."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend
from shiguang import core, library, pipeline, trash


class TrashReviewTests(unittest.TestCase):
    job = backend.BackendReliability.job

    def setUp(self):
        backend.BackendReliability.setUp(self)
        self.root = Path(self.temp.name)
        for module, name, value in ((trash, 'MEDIA', self.root), (pipeline, 'MEDIA', self.root),
                                    (pipeline, 'INCOMPLETE', self.root / '.incomplete'),
                                    (library, 'PLAY_CACHE', self.root / '.cache')):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def file(self, name):
        path = self.root / 'Videos' / 'Other' / (name + '.mp4')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture only')
        return path

    def trash_id(self, jid, owner):
        return core.q('SELECT id FROM job_trash WHERE job_id=? AND owner=? ORDER BY id DESC',
                      (jid, owner), one=True)['id']

    def index(self, jid, label):
        core.q("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,0,1,'subtitle',?)", (jid, label))
        core.q("INSERT INTO vec(kind,ref,part,t,src,base,v) VALUES('job',?,0,1,?,0,?)", (jid, label, b'\0\0\0\0'))

    def collision(self):
        a_path, b_path = self.file('a'), self.file('b')
        jid = self.job(title='A', key='a', files=json.dumps([str(a_path)]))
        self.index(jid, 'A')
        pipeline.remove_job(jid)
        a_trash = self.trash_id(jid, self.owner)
        core.q('INSERT INTO jobs(id,url,key,owner,status,title,files) VALUES(?,?,?,?,?,?,?)',
               (jid, 'https://example.test/b', 'b', 'bob', 'done', 'B', json.dumps([str(b_path)])))
        self.index(jid, 'B')
        core.q('INSERT INTO watch(owner,job_id,pos,updated) VALUES(?,?,?,?)', ('bob', jid, 35, time.time()))
        core.q('INSERT INTO shares(code,job_id,owner) VALUES(?,?,?)', ('b-share', jid, 'bob'))
        core.q('INSERT INTO crawl_downloads(job_id,context) VALUES(?,?)', (jid, '{"title":"B"}'))
        pipeline.remove_job(jid)
        return jid, a_trash, self.trash_id(jid, 'bob'), a_path, b_path

    def assert_index(self, label, jid):
        self.assertEqual([row['ref'] for row in core.q('SELECT ref FROM seg WHERE text=?', (label,))], [jid])
        self.assertEqual([row['ref'] for row in core.q('SELECT ref FROM vec WHERE src=?', (label,))], [jid])

    def assert_no_index(self, label):
        self.assertEqual(core.q('SELECT id FROM seg WHERE text=?', (label,)), [])
        self.assertEqual(core.q('SELECT id FROM vec WHERE src=?', (label,)), [])

    def test_missing_media_restores_as_visible_retryable_failure(self):
        path = self.file('missing')
        jid = self.job(title='Missing file', key='missing', files=json.dumps([str(path)]))
        pipeline.remove_job(jid)
        tid = self.trash_id(jid, self.owner)
        path.unlink()
        response = self.client.post(f'/api/trash/{tid}/restore')
        self.assertEqual(response.status_code, 200)
        row = core.q('SELECT status,retry_at,error FROM jobs WHERE id=?', (jid,), one=True)
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['retry_at'])
        self.assertIn('重试', row['error'])
        self.assertEqual([item['id'] for item in self.client.get('/api/jobs').get_json()['jobs']], [jid])
        self.assertFalse(core.q('SELECT id FROM job_trash WHERE id=?', (tid,), one=True))
        self.assertEqual(self.client.post(f'/api/jobs/{jid}/retry').status_code, 200)
        self.assertEqual(core.q('SELECT status FROM jobs WHERE id=?', (jid,), one=True)['status'], 'queued')

    def test_never_downloaded_job_restores_without_automatic_download(self):
        jid = self.job(title='Not started', key='pending', status='queued')
        pipeline.remove_job(jid)
        restored = trash.restore(self.trash_id(jid, self.owner), self.owner)
        row = core.q('SELECT status,retry_at FROM jobs WHERE id=?', (restored,), one=True)
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['retry_at'])

    def test_purging_older_collision_retains_later_indexes_and_ancillary_metadata(self):
        jid, a, b, a_path, b_path = self.collision()
        trash.purge(a, self.owner)
        self.assert_no_index('A')
        self.assert_index('B', jid)
        self.assertFalse(a_path.exists())
        self.assertTrue(b_path.exists())
        self.assertTrue(core.q('SELECT id FROM job_trash WHERE id=?', (b,), one=True))
        for table in ('watch', 'shares', 'crawl_downloads'):
            self.assertTrue(core.q(f'SELECT 1 FROM {table} WHERE job_id=?', (jid,), one=True))
        restored = trash.restore(b, 'bob')
        self.assert_index('B', restored)
        self.assertEqual(core.q('SELECT pos FROM watch WHERE owner=? AND job_id=?', ('bob', restored), one=True)['pos'], 35)

    def test_purging_later_collision_does_not_delete_earlier_claimed_indexes(self):
        jid, a, b, a_path, b_path = self.collision()
        trash.purge(b, 'bob')
        self.assert_index('A', jid)
        self.assert_no_index('B')
        self.assertTrue(a_path.exists())
        self.assertFalse(b_path.exists())
        self.assertEqual(trash.restore(a, self.owner), jid)
        self.assert_index('A', jid)

    def test_restoring_later_collision_uses_new_id_and_moves_only_its_indexes(self):
        jid, a, b, _, _ = self.collision()
        restored = trash.restore(b, 'bob')
        self.assertNotEqual(restored, jid)
        self.assert_index('A', jid)
        self.assert_index('B', restored)
        self.assertEqual(trash.restore(a, self.owner), jid)
        self.assert_index('A', jid)
        self.assert_index('B', restored)

    def test_restoring_earlier_collision_preserves_later_recycled_indexes(self):
        jid, a, b, _, _ = self.collision()
        restored = trash.restore(a, self.owner)
        self.assertNotEqual(restored, jid)
        self.assert_index('A', restored)
        self.assert_index('B', jid)
        self.assertEqual(trash.restore(b, 'bob'), jid)
        self.assert_index('A', restored)
        self.assert_index('B', jid)

    def test_last_purge_cleans_ancillary_records_after_other_entry_is_gone(self):
        jid, a, b, _, _ = self.collision()
        trash.purge(a, self.owner)
        self.assertTrue(core.q('SELECT 1 FROM watch WHERE job_id=?', (jid,), one=True))
        trash.purge(b, 'bob')
        for table in ('watch', 'shares', 'crawl_downloads'):
            self.assertFalse(core.q(f'SELECT 1 FROM {table} WHERE job_id=?', (jid,), one=True))
        self.assert_no_index('A')
        self.assert_no_index('B')


if __name__ == '__main__':
    unittest.main()
