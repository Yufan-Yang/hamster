"""Real isolated file operations, durable recovery and owner/privacy boundaries."""
import importlib.util
import errno
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('care_fixtures', Path(__file__).with_name('steward.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import board, core, housekeeping as care, steward, media_workflows, heal  # noqa: E402


class CareTests(unittest.TestCase):
    setUp = fixtures.StewardTests.setUp
    client = fixtures.StewardTests.client
    collection = fixtures.StewardTests.collection
    job = fixtures.StewardTests.job
    media = fixtures.StewardTests.media
    row = fixtures.StewardTests.row
    subs = fixtures.StewardTests.subs

    def report(self, jid):
        return care.diagnose(self.row(jid))

    def request(self, jid, action='organize_files', client=None):
        return (client or self.alice).post('/api/steward/care/action', json={
            'job_id': jid, 'action': action, 'revision': self.report(jid)['revision']})

    def work(self, kind='steward_care_apply'):
        task = board.claim_task('care-test', [kind])
        self.assertIsNotNone(task)
        state = board.run_claimed(task, 'care-test')
        return state, core.q('SELECT * FROM tasks WHERE id=?', (task['id'],), one=True)

    def test_inspection_and_autonomous_diagnosis_never_move_or_download(self):
        jid, files = self.media()
        care.tick()
        self.assertEqual(self.work('steward_diagnose')[0], 'done')
        self.assertTrue(files[0].exists())
        self.assertEqual(core.q('SELECT count(*) n FROM media_file_ops', one=True)['n'], 0)
        d = self.alice.get('/api/steward').json['care']
        self.assertEqual(d['checked'], 1)
        self.assertEqual(d['items'][0]['job_id'], jid)
        self.assertEqual(d['items'][0]['actions'][0]['id'], 'organize_files')

    def test_real_move_includes_subtitles_cover_and_database_then_undo(self):
        jid, files = self.media()
        original = files[0]
        sub = self.subs(original)
        cover = original.with_suffix('.jpg'); cover.write_bytes(b'cover')
        core.q('UPDATE jobs SET files=?,thumb=? WHERE id=?', (json.dumps([str(original), str(sub), str(cover)]), str(cover), jid))
        old = dict(self.row(jid))
        self.assertEqual(self.request(jid).status_code, 202)
        self.assertEqual(self.work()[0], 'done')
        self.assertFalse(original.exists())
        row = self.row(jid); moved = json.loads(row['files'])
        self.assertTrue(all(Path(p).is_file() for p in moved))
        self.assertEqual(Path(moved[0]).read_bytes(), b'synthetic media; no decoder is used in inventory')
        self.assertEqual(Path(row['thumb']).read_bytes(), b'cover')
        op = core.q('SELECT * FROM media_file_ops', one=True)
        self.assertEqual(op['state'], 'done')
        self.assertEqual(self.alice.post('/api/steward/care/undo', json={'operation_id':op['id']}).status_code, 202)
        self.assertEqual(self.work('steward_care_undo')[0], 'done')
        self.assertEqual(self.row(jid)['files'], old['files'])
        self.assertEqual(self.row(jid)['thumb'], old['thumb'])
        self.assertTrue(original.exists() and sub.exists() and cover.exists())

    def test_collision_never_overwrites_either_file(self):
        jid, files = self.media()
        proposal = care.layout(self.row(jid)); target = Path(proposal['moves'][0]['destination'])
        target.parent.mkdir(parents=True); target.write_bytes(b'another movie')
        report = self.report(jid)
        self.assertFalse(report['actions'])
        self.assertIn('同名', report['issues'][0])
        self.assertEqual(target.read_bytes(), b'another movie')
        self.assertTrue(files[0].exists())

    def test_replaced_media_invalidates_queued_plan(self):
        jid, files = self.media()
        self.request(jid)
        files[0].write_bytes(b'replacement')
        state, task = self.work()
        self.assertEqual(state, 'done')
        self.assertIn('skipped', json.loads(task['result']))
        self.assertTrue(files[0].exists())

    def test_shared_and_recycled_references_block_move(self):
        jid, files = self.media()
        other, _ = self.media(owner='user:bob')
        core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(files[0])]), other))
        self.assertFalse(self.report(jid)['actions'])
        core.q('DELETE FROM jobs WHERE id=?', (other,))
        core.q("INSERT INTO job_trash(owner,snapshot,deleted_at) VALUES(?,?,?)", ('user:bob', json.dumps({'files':json.dumps([str(files[0])])}), time.time()))
        self.assertFalse(self.report(jid)['actions'])

    def test_file_symlink_and_parent_symlink_are_rejected(self):
        jid, files = self.media()
        outside = self.path / 'real'; outside.mkdir()
        (outside/'movie.mp4').write_bytes(b'movie')
        alias = self.path / 'alias'; alias.symlink_to(outside, target_is_directory=True)
        core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(alias/'movie.mp4')]), jid))
        self.assertFalse(self.report(jid)['actions'])
        files[0].unlink(); files[0].symlink_to(outside/'movie.mp4')
        core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(files[0])]), jid))
        self.assertFalse(self.report(jid)['actions'])

    def test_privacy_foreign_owner_and_stale_revision_cannot_execute(self):
        jid, _ = self.media()
        self.assertEqual(self.request(jid, client=self.bob).status_code, 404)
        self.assertEqual(self.alice.post('/api/steward/care/action', json={'job_id':jid,'action':'organize_files','revision':'old'}).status_code, 409)
        core.q('INSERT OR REPLACE INTO privacy(owner,tags,ids) VALUES(?,?,?)', (self.owner, '[]', json.dumps([jid])))
        self.assertEqual(self.request(jid).status_code, 404)
        care.observe(self.row(jid))
        self.assertEqual(self.alice.get('/api/steward').json['care']['total'], 0)
        self.assertEqual(self.bob.get('/api/steward').json['care']['checked'], 0)

    def test_keeping_layout_is_persistent_until_media_changes(self):
        jid, _ = self.media()
        self.assertEqual(self.request(jid, 'keep_layout').status_code, 200)
        care.observe(self.row(jid))
        self.assertEqual(self.alice.get('/api/steward').json['care']['total'], 0)
        a=json.loads(self.row(jid)['analysis']); a['title']='New title'
        core.q('UPDATE jobs SET analysis=? WHERE id=?', (json.dumps(a), jid))
        care.observe(self.row(jid))
        self.assertEqual(self.alice.get('/api/steward').json['care']['total'], 1)

    def test_retry_retains_partial_files_and_is_idempotently_queued(self):
        jid, _ = self.media()
        core.q("UPDATE jobs SET status='failed',error='network interrupted',attempts=3 WHERE id=?", (jid,))
        partial=self.path/'.incomplete'/str(jid); partial.mkdir(parents=True); (partial/'video.part').write_bytes(b'partial')
        first=self.request(jid,'retry_download'); second=self.request(jid,'retry_download')
        self.assertEqual(first.json['task_id'], second.json['task_id'])
        self.assertEqual(self.work()[0], 'done')
        self.assertEqual(self.row(jid)['status'], 'queued')
        self.assertEqual((partial/'video.part').read_bytes(), b'partial')

    def test_cancelled_and_scheduled_retries_are_not_restarted(self):
        jid, _ = self.media()
        for state,cancel,retry in [('cancelled',1,None),('failed',1,None),('failed',0,time.time()+100)]:
            core.q('UPDATE jobs SET status=?,cancel=?,retry_at=? WHERE id=?', (state,cancel,retry,jid))
            self.assertFalse(self.report(jid)['actions'])

    def test_identical_finished_download_is_reused_without_network(self):
        source, files = self.media()
        jid, _ = self.media()
        original=self.row(source)
        core.q("UPDATE jobs SET status='failed',url=?,key=? WHERE id=?", (original['url'], original['key'], jid))
        self.assertEqual(self.report(jid)['actions'][0]['id'], 'reuse_download')
        self.assertEqual(self.request(jid,'reuse_download').status_code, 202)
        self.assertEqual(self.work()[0], 'done')
        self.assertEqual(self.row(jid)['ref'], source)
        self.assertIn(str(files[0]), self.row(jid)['files'])

    def test_partial_move_recovers_and_database_only_changes_when_complete(self):
        jid, files = self.media(); self.subs(files[0])
        before=self.row(jid)['files']
        original=care._transfer
        calls=[]
        def fail_second(src,dst):
            calls.append(src)
            if len(calls)==2: raise OSError('simulated power loss')
            original(src,dst)
        self.request(jid)
        with patch.object(care,'_transfer',side_effect=fail_second):
            self.assertEqual(self.work()[0], 'failed')
        self.assertEqual(self.row(jid)['files'], before)
        care.recover()
        self.assertEqual(core.q('SELECT state FROM media_file_ops',one=True)['state'], 'done')
        self.assertTrue(Path(json.loads(self.row(jid)['files'])[0]).exists())

    def test_partial_move_can_be_rolled_back(self):
        jid, files = self.media(); self.subs(files[0])
        original=care._transfer; calls=[]
        def fail_second(src,dst):
            calls.append(src)
            if len(calls)==2: raise ValueError('blocked')
            original(src,dst)
        self.request(jid)
        with patch.object(care,'_transfer',side_effect=fail_second): self.work()
        op=core.q('SELECT id FROM media_file_ops',one=True)
        self.assertEqual(self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']}).status_code,202)
        self.assertEqual(self.work('steward_care_undo')[0],'done')
        self.assertTrue(files[0].exists())

    def test_changed_target_cannot_be_undone_or_overwrite_original(self):
        jid, files = self.media(); self.request(jid); self.work()
        target=Path(json.loads(self.row(jid)['files'])[0]); target.write_bytes(b'edited later')
        op=core.q('SELECT id FROM media_file_ops',one=True)
        self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']})
        self.assertEqual(self.work('steward_care_undo')[0],'failed')
        self.assertEqual(target.read_bytes(),b'edited later')
        self.assertFalse(files[0].exists())

    def test_active_media_processing_blocks_file_move(self):
        jid, files = self.media(); self.request(jid)
        board.publish('transcribe', f'job:{jid}:0', payload={'job':jid,'part':0})
        self.assertEqual(self.work()[0],'failed')
        self.assertTrue(files[0].exists())

    def test_autonomous_proposal_review_execution_and_pause(self):
        jid, files = self.media()
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True})
        care.observe(self.row(jid)); care.auto_apply(self.row(jid))
        task=core.q("SELECT payload FROM tasks WHERE kind='steward_care_apply'",one=True)
        self.assertTrue(json.loads(task['payload'])['review'])
        core.kv_set(steward.POLICY_PREFIX+self.owner,steward.DEFAULT_POLICY)
        self.assertEqual(self.work()[0],'done')
        self.assertTrue(files[0].exists())
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True})
        # A paused completed intent is not silently replayed. New diagnosis changes the source version.
        a=json.loads(self.row(jid)['analysis']);a['title']='Fresh layout'
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(a),jid))
        care.auto_apply(self.row(jid))
        self.assertEqual(self.work()[0],'done')
        self.assertFalse(files[0].exists())

    def test_undo_prevents_automatic_reorganization(self):
        jid,_=self.media();self.request(jid);self.work()
        op=core.q('SELECT id FROM media_file_ops',one=True)
        self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']});self.work('steward_care_undo')
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True})
        care.auto_apply(self.row(jid))
        self.assertEqual(core.q("SELECT count(*) n FROM tasks WHERE kind='steward_care_apply'",one=True)['n'],1)

    def test_automatic_retry_reviews_error_and_daily_limit(self):
        jid,_=self.media();core.q("UPDATE jobs SET status='failed',error='403 forbidden' WHERE id=?",(jid,))
        core.kv_set(steward.POLICY_PREFIX+self.owner,{**steward.DEFAULT_POLICY,'automatic':True})
        care.auto_apply(self.row(jid))
        self.assertIsNone(core.q("SELECT id FROM tasks WHERE kind='steward_care_apply'",one=True))
        for attempt in range(3):
            core.q("UPDATE jobs SET status='failed',error='network timeout',updated=? WHERE id=?",(time.time()+attempt,jid))
            care.auto_apply(self.row(jid))
            if attempt<2:self.work()
        self.assertEqual(core.q("SELECT count(*) n FROM tasks WHERE kind='steward_care_apply'",one=True)['n'],2)

    def test_video_type_proposals_and_independent_review(self):
        cases=[({'library':'Music'},False),({'folder':'Music Videos','needs_transcript':True},False),
               ({'library':'TV','needs_transcript':True},False),({'library':'Movies'},False),
               ({'folder':'Tutorials','needs_transcript':False},True),({'folder':'Talks'},True),
               ({'folder':'Gaming','needs_transcript':False},False)]
        jid,_=self.media()
        for analysis,semantic in cases:
            with self.subTest(analysis=analysis):
                self.assertEqual(media_workflows.propose(analysis)['semantic'],semantic)
                core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps(analysis),jid))
                item=steward.inspect(self.row(jid),steward.DEFAULT_POLICY)
                self.assertEqual(any(a['kind']=='transcribe' for a in item['plan']),semantic)
                self.assertTrue(item['observation']['review']['approved'])
        core.q('UPDATE jobs SET analysis=? WHERE id=?',(json.dumps({'library':'Music'}),jid))
        self.assertFalse(media_workflows.review(self.row(jid),[{'kind':'transcribe'}])['approved'])

    def test_new_shared_reference_after_plan_blocks_execution(self):
        jid,files=self.media();self.request(jid)
        original=care._recover
        def new_reference(op):
            other,_=self.media(owner='user:bob')
            core.q('UPDATE jobs SET files=? WHERE id=?',(json.dumps([str(files[0])]),other))
            return original(op)
        with patch.object(care,'_recover',side_effect=new_reference):self.assertEqual(self.work()[0],'failed')
        self.assertTrue(files[0].exists())

    def test_summary_updates_do_not_invalidate_file_plan_or_prevent_undo(self):
        jid,files=self.media();original=self.report(jid)['revision']
        a=json.loads(self.row(jid)['analysis']);a['summary']='New useful summary'
        core.q('UPDATE jobs SET analysis=?,updated=? WHERE id=?',(json.dumps(a),time.time(),jid))
        self.assertEqual(self.report(jid)['revision'],original)
        self.request(jid);self.work()
        a['summary']='Later summary'
        core.q('UPDATE jobs SET analysis=?,updated=? WHERE id=?',(json.dumps(a),time.time(),jid))
        op=core.q('SELECT id FROM media_file_ops',one=True)
        self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']})
        self.assertEqual(self.work('steward_care_undo')[0],'done')
        self.assertTrue(files[0].exists())
        self.assertEqual(json.loads(self.row(jid)['analysis'])['summary'],'Later summary')

    def test_replaying_a_completed_journal_is_idempotent(self):
        jid,files=self.media();self.request(jid);self.work()
        op=core.q('SELECT * FROM media_file_ops',one=True)
        self.assertEqual(care._recover(op)['outcome'],'已归档文件、字幕和封面')
        self.assertFalse(files[0].exists())
        self.assertEqual(core.q('PRAGMA synchronous',one=True)[0],1)

    def test_undo_can_be_retried_after_processing_finishes(self):
        jid,files=self.media();self.request(jid);self.work()
        op=core.q('SELECT id FROM media_file_ops',one=True)
        tid=board.publish('transcribe',f'job:{jid}:0',payload={'job':jid,'part':0})
        self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']})
        self.assertEqual(self.work('steward_care_undo')[0],'failed')
        core.q("UPDATE tasks SET state='done' WHERE id=?",(tid,))
        self.alice.post('/api/steward/care/undo',json={'operation_id':op['id']})
        self.assertEqual(self.work('steward_care_undo')[0],'done')
        self.assertTrue(files[0].exists())

    def test_unsupported_storage_does_not_become_a_code_repair_incident(self):
        jid,files=self.media();self.request(jid)
        core.q("UPDATE tasks SET attempts=4 WHERE kind='steward_care_apply'")
        with patch.object(care.os,'link',side_effect=OSError(errno.EXDEV,'cross-device link')):
            self.assertEqual(self.work()[0],'failed')
        self.assertTrue(files[0].exists())
        self.assertEqual(core.q('SELECT count(*) FROM incidents',one=True)[0],0)

    def test_terminal_local_failure_reaches_healer_without_leaking_to_other_accounts(self):
        jid,_=self.media();self.request(jid)
        core.q("UPDATE tasks SET attempts=4 WHERE kind='steward_care_apply'")
        with patch.object(care,'_recover',side_effect=RuntimeError('synthetic program defect')):
            self.assertEqual(self.work()[0],'failed')
        self.assertIsNotNone(core.q("SELECT id FROM tasks WHERE kind='heal'",one=True))
        self.assertTrue(self.alice.get('/api/tasks').json['heal'])
        self.assertFalse(self.bob.get('/api/tasks').json['heal'])
        self.assertNotEqual(heal.signature('steward_care_apply','same error','',{'owner':'user:alice'}),
                            heal.signature('steward_care_apply','same error','',{'owner':'user:bob'}))


if __name__ == '__main__':
    unittest.main()
