"""Media stewardship against isolated storage, real task leases and synthetic subtitles."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('steward_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import board, core, library, llm, steward, web  # noqa: E402


class StewardTests(unittest.TestCase):
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def setUp(self):
        fixtures.HomeSearchTests.setUp(self)
        for target in (core, library):
            mocked = patch.object(target, 'MEDIA', self.path)
            mocked.start()
            self.addCleanup(mocked.stop)
        library.folder_names.cache_clear() if hasattr(library.folder_names, 'cache_clear') else None
        self.owner = 'user:alice'

    def media(self, owner='user:alice', speech=True, summary='', transcript='', parts=1):
        jid = self.job(owner, f'Example media {time.time_ns()}', summary=summary, transcript=transcript)
        paths = []
        for part in range(parts):
            path = self.path / f'{jid}-{part}.mp4'
            path.write_bytes(b'synthetic media; no decoder is used in inventory')
            paths.append(str(path))
        a = {'library': 'Videos', 'needs_transcript': speech, 'summary': summary}
        core.q('UPDATE jobs SET files=?,analysis=? WHERE id=?', (json.dumps(paths), json.dumps(a), jid))
        return jid, [Path(p) for p in paths]

    def row(self, jid):
        return core.q('SELECT * FROM jobs WHERE id=?', (jid,), one=True)

    def item(self, jid, profile='standard'):
        return steward.inspect(self.row(jid), {**steward.DEFAULT_POLICY, 'profile': profile})

    def subs(self, path, lines=24):
        sub = path.with_suffix('.en.srt')
        sub.write_text('\n\n'.join(f'{i+1}\n00:00:{i:02}.000 --> 00:00:{i+1:02}.000\nDistinct subtitle line {i}' for i in range(lines)) + '\n')
        return sub

    def task(self, kind):
        return core.q('SELECT * FROM tasks WHERE kind=?', (kind,), one=True)

    def policy(self, **values):
        core.kv_set(steward.POLICY_PREFIX + self.owner, {**steward.DEFAULT_POLICY, **values})

    def test_inventory_is_read_only_and_prefers_existing_subtitles(self):
        jid, files = self.media()
        self.subs(files[0])
        with patch.object(library, 'ffprobe', side_effect=AssertionError('inventory must not decode')):
            result = steward.scan(self.owner)
        self.assertEqual(result['dispatched'], 0)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM tasks', one=True)['n'], 0)
        item = self.item(jid)
        self.assertEqual([x['kind'] for x in item['plan']], ['steward_index'])
        self.assertEqual(item['observation']['files'], 1)

    def test_import_then_summarize_then_verify_actual_outputs(self):
        jid, files = self.media()
        self.subs(files[0])
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 1)
        task = board.claim_task('test-steward-index', ['steward_index'])
        self.assertIsNotNone(task)
        self.assertEqual(board.run_claimed(task, 'test-steward-index'), 'done')
        self.assertIn('Distinct subtitle', self.row(jid)['transcript'])
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 1)
        task = board.claim_task('test-summary', ['summarize'])
        def summarize(job_id, analysis, *_):
            analysis.update(summary='A grounded summary', key_points=['A point'], chapters={'0': [{'t': 0, 'title': 'Beginning'}]})
            return True
        with patch.object(llm, 'summarize_timed', side_effect=summarize), patch.object(llm, 'merge_tags_if_new'):
            self.assertEqual(board.run_claimed(task, 'test-summary'), 'done')
        result = steward.scan(self.owner, apply=True)
        self.assertEqual(result['dispatched'], 0)
        self.assertEqual(self.item(jid)['state'], 'ready')
        provenance = json.loads(self.row(jid)['analysis'])['_summary_source']
        self.assertEqual(provenance['transcript'], steward.text_digest(self.row(jid)['transcript']))

    def test_repeat_and_concurrent_dispatch_are_idempotent(self):
        jid, _ = self.media()
        item = self.item(jid)
        action = item['plan'][0]
        self.assertTrue(steward.dispatch(self.owner, item, action, steward.DEFAULT_POLICY))
        self.assertFalse(steward.dispatch(self.owner, item, action, steward.DEFAULT_POLICY))
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM media_steward_actions', one=True)['n'], 1)
        self.assertEqual(self.item(jid)['state'], 'waiting')

    def test_two_workers_dispatch_one_durable_task(self):
        jid, _ = self.media()
        item = self.item(jid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: steward.dispatch(self.owner, item, item['plan'][0], steward.DEFAULT_POLICY), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(core.q('SELECT COUNT(*) n FROM media_steward_actions', one=True)['n'], 1)

    def test_restart_keeps_actions_and_does_not_resubmit_work(self):
        self.media()
        steward.scan(self.owner, apply=True)
        core.DB.close()
        core.init_db()
        self.addCleanup(core.DB.close)
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM media_steward_actions', one=True)['n'], 1)

    def test_version_35_database_upgrades_and_migration_is_repeatable(self):
        core.q('DROP TABLE media_steward_assets')
        core.q('DROP TABLE media_steward_actions')
        core.q('PRAGMA user_version=35')
        core.DB.close()
        core.init_db()
        self.addCleanup(core.DB.close)
        self.assertEqual(core.q('PRAGMA user_version', one=True)[0], 36)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM media_steward_actions', one=True)['n'], 0)

    def test_concurrent_manual_summary_edit_is_not_claimed_by_generator(self):
        jid, files = self.media(transcript='Source text', summary='Original human summary')
        self.subs(files[0])
        board.publish('summarize', f'job:{jid}')
        task = board.claim_task('test-human-edit', ['summarize'])
        def generate(job_id, analysis, *_):
            current = json.loads(self.row(jid)['analysis'])
            current['summary'] = 'Human edit made while model was running'
            core.q('UPDATE jobs SET analysis=? WHERE id=?', (json.dumps(current), jid))
            return {**analysis, 'summary':'Model output', 'key_points':['A point']}
        with patch.object(llm, 'summarize', side_effect=generate), patch.object(llm, 'merge_tags_if_new'):
            self.assertEqual(board.run_claimed(task, 'test-human-edit'), 'done')
        a = json.loads(self.row(jid)['analysis'])
        self.assertEqual(a['summary'], 'Human edit made while model was running')
        self.assertNotIn('_summary_source', a)

    def test_false_success_backs_off_and_stops_after_two_attempts(self):
        jid, _ = self.media()
        steward.scan(self.owner, apply=True)
        tid = self.task('transcribe')['id']
        core.q("UPDATE tasks SET state='done',result='{}' WHERE id=?", (tid,))
        self.assertEqual(self.item(jid)['plan'][0]['state'], 'backoff')
        core.q('UPDATE media_steward_actions SET created=?', (time.time() - steward.COOLDOWN - 1,))
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 1)
        core.q("UPDATE tasks SET state='done' WHERE id=?", (tid,))
        self.assertEqual(self.item(jid)['state'], 'blocked')
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)

    def test_failed_task_is_not_blindly_reset(self):
        jid, _ = self.media()
        steward.scan(self.owner, apply=True)
        core.q("UPDATE tasks SET state='failed',attempts=5,error='terminal' WHERE kind='transcribe'")
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)
        self.assertEqual(self.task('transcribe')['attempts'], 5)
        self.assertEqual(self.item(jid)['state'], 'blocked')

    def test_existing_subtitle_followup_blocks_duplicate_transcription(self):
        jid, _ = self.media()
        board.publish('save_subs', f'job:{jid}:0', payload={'job':jid, 'part':0})
        self.assertEqual(self.item(jid)['state'], 'waiting')
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)
        self.assertIsNone(self.task('transcribe'))

    def test_missing_part_blocks_all_actions_without_reindexing_parts(self):
        jid, files = self.media(parts=2)
        files[0].unlink()
        self.assertEqual(self.item(jid)['state'], 'blocked')
        self.assertEqual(self.item(jid)['plan'], [])
        self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)

    def test_unavailable_storage_outside_files_and_symlinks_never_dispatch(self):
        jid, files = self.media()
        with patch.object(core, 'MEDIA', self.path / 'offline'):
            self.assertEqual(self.item(jid)['state'], 'blocked')
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / 'outside.mp4'
            outside.write_bytes(b'outside')
            core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(outside)]), jid))
            self.assertEqual(self.item(jid)['plan'], [])
            files[0].unlink()
            files[0].symlink_to(outside)
            core.q('UPDATE jobs SET files=? WHERE id=?', (json.dumps([str(files[0])]), jid))
            self.assertEqual(self.item(jid)['state'], 'blocked')

    def test_unsafe_subtitle_does_not_trigger_processing(self):
        jid, files = self.media()
        self.subs(files[0]).unlink()
        files[0].with_suffix('.en.srt').symlink_to('/etc/hosts')
        self.assertEqual(self.item(jid)['state'], 'blocked')
        self.assertEqual(self.item(jid)['plan'], [])

    def test_policy_profiles_music_and_shared_media(self):
        jid, _ = self.media(speech=False)
        self.assertEqual(self.item(jid)['plan'], [])
        self.assertEqual(self.item(jid, 'deep')['plan'][0]['kind'], 'transcribe')
        self.assertEqual(self.item(jid, 'essential')['plan'], [])
        core.q("UPDATE jobs SET analysis=json_set(analysis,'$.library','Music') WHERE id=?", (jid,))
        self.assertEqual(self.item(jid, 'deep')['plan'], [])
        core.q('UPDATE jobs SET ref=999 WHERE id=?', (jid,))
        self.assertEqual(self.item(jid)['state'], 'shared')

    def test_changed_files_policy_or_owner_invalidates_a_plan(self):
        jid, files = self.media()
        item = self.item(jid)
        action = item['plan'][0]
        files[0].write_bytes(b'changed file')
        self.assertFalse(steward.dispatch(self.owner, item, action, steward.DEFAULT_POLICY))
        item = self.item(jid)
        self.policy(profile='essential')
        self.assertFalse(steward.dispatch(self.owner, item, action, steward.DEFAULT_POLICY))
        self.policy()
        core.q("UPDATE jobs SET owner='user:bob' WHERE id=?", (jid,))
        self.assertFalse(steward.dispatch(self.owner, item, action, steward.DEFAULT_POLICY))

    def test_pausing_automatic_mode_prevents_queued_scan_dispatch(self):
        self.media()
        self.policy(automatic=True)
        tid = steward.queue_scan(self.owner, apply=True, automatic=True)
        self.policy(automatic=False)
        task = board.claim_task('test-steward', ['steward_scan'])
        self.assertEqual(task['id'], tid)
        self.assertEqual(board.run_claimed(task, 'test-steward'), 'done')
        self.assertIsNone(self.task('transcribe'))

    def test_manual_batch_continues_after_subtitle_task_without_automatic_mode(self):
        _, files = self.media()
        self.subs(files[0])
        response = self.alice.post('/api/steward/scan', json={'apply':True})
        self.assertEqual(response.status_code, 202)
        request_task = board.claim_task('manual-scan', ['steward_scan'])
        board.run_claimed(request_task, 'manual-scan')
        subtitle_task = board.claim_task('manual-index', ['steward_index'])
        board.run_claimed(subtitle_task, 'manual-index')
        with patch.object(steward.time, 'sleep', side_effect=StopIteration), self.assertRaises(StopIteration):
            steward.loop()
        self.assertEqual(self.task('steward_scan')['state'], 'queued')
        next_scan = board.claim_task('manual-scan-next', ['steward_scan'])
        board.run_claimed(next_scan, 'manual-scan-next')
        self.assertIsNotNone(self.task('summarize'))
        self.assertFalse(steward.policy(self.owner)['automatic'])
        self.assertTrue(self.alice.get('/api/steward').json['busy'])

    def test_manual_batch_is_bounded_and_stops_at_terminal_assets(self):
        jid, files = self.media()
        self.subs(files[0])
        steward.scan(self.owner)
        tid = steward.queue_scan(self.owner, [jid], apply=True, requested_at=time.time() - steward.MANUAL_WINDOW - 1)
        core.q("UPDATE tasks SET state='done' WHERE id=?", (tid,))
        steward.tick()
        self.assertEqual(self.task('steward_scan')['state'], 'done')
        steward.queue_scan(self.owner, [jid], apply=True)
        core.q("UPDATE tasks SET state='done' WHERE id=?", (tid,))
        files[0].unlink()
        steward.scan(self.owner, [jid])
        steward.tick()
        self.assertEqual(self.task('steward_scan')['state'], 'done')

    def test_manual_batch_keeps_original_deadline_on_rescan(self):
        jid, _ = self.media()
        steward.scan(self.owner)
        requested = time.time() - 300
        tid = steward.queue_scan(self.owner, [jid], apply=True, requested_at=requested)
        core.q("UPDATE tasks SET state='done' WHERE id=?", (tid,))
        steward.tick()
        self.assertEqual(json.loads(self.task('steward_scan')['payload'])['requested_at'], requested)

    def test_completed_subtitle_task_invalidates_an_earlier_ready_observation(self):
        jid, files = self.media()
        self.subs(files[0])
        steward.queue_scan(self.owner, [jid], apply=True)
        task = board.claim_task('ready-race-scan', ['steward_scan'])
        board.run_claimed(task, 'ready-race-scan')
        # A concurrent observer can have read the old, valid artifacts just before import commits.
        core.q("UPDATE media_steward_assets SET state='ready',checked=? WHERE job_id=?", (time.time() - 1, jid))
        task = board.claim_task('ready-race-import', ['steward_index'])
        board.run_claimed(task, 'ready-race-import')
        steward.tick()
        self.assertEqual(self.task('steward_scan')['state'], 'queued')

    def test_paused_automatic_batch_does_not_become_manual_intent(self):
        jid, _ = self.media()
        steward.scan(self.owner)
        tid = steward.queue_scan(self.owner, [jid], apply=True, automatic=True)
        core.q("UPDATE tasks SET state='done' WHERE id=?", (tid,))
        steward.tick()
        self.assertEqual(self.task('steward_scan')['state'], 'done')

    def test_execution_rejects_media_replaced_after_dispatch(self):
        _, files = self.media()
        steward.scan(self.owner, apply=True)
        task = board.claim_task('test-transcribe', ['transcribe'])
        files[0].write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError, 'media changed'):
            board.task_media(task)

    def test_old_and_manually_edited_summaries_are_preserved(self):
        jid, _ = self.media(summary='My own summary', transcript='New transcript')
        self.assertNotIn('summarize', [x['kind'] for x in self.item(jid)['plan']])
        a = json.loads(self.row(jid)['analysis'])
        a['_summary_source'] = {'transcript': steward.text_digest('Old transcript'), 'output': steward.text_digest('My own summary')}
        core.q('UPDATE jobs SET analysis=? WHERE id=?', (json.dumps(a), jid))
        self.assertIn('summarize', [x['kind'] for x in self.item(jid)['plan']])
        a['summary'] = 'Manually corrected'
        core.q('UPDATE jobs SET analysis=? WHERE id=?', (json.dumps(a), jid))
        self.assertNotIn('summarize', [x['kind'] for x in self.item(jid)['plan']])

    def test_subtitle_change_is_detected_and_stale_summary_follows_import(self):
        jid, files = self.media()
        sub = self.subs(files[0])
        steward.scan(self.owner, apply=True)
        task = board.claim_task('test-index', ['steward_index'])
        board.run_claimed(task, 'test-index')
        os.utime(sub, (time.time() + 2, time.time() + 2))
        self.assertEqual(self.item(jid)['plan'][0]['kind'], 'steward_index')

    def test_ai_can_only_reorder_candidates_and_is_bounded(self):
        self.media()
        self.media()
        self.policy(ai_planning=True)
        with patch.object(llm, 'llm_json', return_value={'order': ['delete:everything']}) as model:
            result = steward.scan(self.owner, apply=True)
        self.assertEqual(result['planner'], 'rules_fallback')
        self.assertEqual(result['dispatched'], 2)
        self.assertEqual(model.call_count, 1)
        self.media()
        self.media()
        with patch.object(llm, 'llm_json') as model:
            result = steward.scan(self.owner, apply=True)
        self.assertEqual(result['planner'], 'rules_budget')
        model.assert_not_called()

    def test_ai_order_cache_is_reused_without_model_calls(self):
        candidates = [{'key':'1:a','kind':'transcribe'}, {'key':'2:b','kind':'summarize'}]
        with patch.object(llm, 'llm_json', return_value={'order':['2:b','1:a']}) as model:
            ordered, mode = steward._ordered(self.owner, candidates, True, lambda *_: None)
            second, cached = steward._ordered(self.owner, candidates, True, lambda *_: None)
        self.assertEqual([x['key'] for x in ordered], ['2:b','1:a'])
        self.assertEqual((mode, cached, model.call_count), ('ai', 'ai_cached', 1))
        self.assertEqual(second, ordered)

    def test_scan_round_and_daily_dispatch_limits(self):
        for _ in range(steward.BATCH + 2):
            self.media()
        result = steward.scan(self.owner, apply=True)
        self.assertEqual(result['checked'], steward.BATCH)
        self.assertEqual(result['dispatched'], steward.DISPATCH_LIMIT)
        with patch.object(steward, 'DAILY_LIMIT', steward.DISPATCH_LIMIT):
            self.assertEqual(steward.scan(self.owner, apply=True)['dispatched'], 0)

    def test_invalid_chapters_and_summary_are_not_accepted_as_artifacts(self):
        jid, files = self.media(transcript='Source text')
        self.subs(files[0])
        core.q("INSERT INTO seg(kind,ref,part,t,src,text) VALUES('job',?,0,0,'字幕','Source')", (jid,))
        a = json.loads(self.row(jid)['analysis'])
        a.update(summary={'invalid':'shape'},chapters={'0':[{'t':-1,'title':'bad'}]})
        core.q('UPDATE jobs SET analysis=? WHERE id=?', (json.dumps(a), jid))
        item = self.item(jid)
        self.assertFalse(item['observation']['summary'])
        self.assertEqual(item['observation']['chapters'], 0)
        self.assertIn('summarize', [x['kind'] for x in item['plan']])

    def test_failed_automatic_scan_observes_cooldown(self):
        tid = steward.queue_scan(self.owner, automatic=True, apply=True)
        core.q("UPDATE tasks SET state='failed',attempts=5 WHERE id=?", (tid,))
        self.assertEqual(steward.queue_scan(self.owner, automatic=True, apply=True), tid)
        self.assertEqual(core.q('SELECT state FROM tasks WHERE id=?', (tid,), one=True)['state'], 'failed')

    def test_accounts_admin_and_private_counts_are_isolated(self):
        public, _ = self.media()
        private, _ = self.media()
        foreign, _ = self.media(owner='user:bob')
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', (self.owner, '[]', json.dumps([private])))
        steward.scan(self.owner)
        normal = self.alice.get('/api/steward')
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(normal.headers['Cache-Control'], 'no-store')
        self.assertEqual({x['job_id'] for x in normal.json['items']}, {public})
        self.assertEqual(sum(normal.json['counts'].values()), 1)
        self.assertEqual(self.admin.get('/api/steward').json['items'], [])
        self.assertEqual({x['job_id'] for x in self.bob.get('/api/steward').json['items']}, {foreign})
        token = {'owner': self.owner, 'reveal': True, 'expires': time.time() + 60}
        with patch.dict(web.privacy_tokens, {'steward-test': token}):
            response = self.alice.get('/api/steward', headers={'X-Privacy-Token':'steward-test'})
            self.assertEqual({x['job_id'] for x in response.json['items']}, {private})
        self.assertEqual(self.alice.get('/api/steward', headers={'X-Privacy-Token':'expired'}).status_code, 403)
        self.assertNotIn(str(self.path), normal.get_data(as_text=True))

    def test_scan_only_queues_visible_ids_and_post_validation(self):
        public, _ = self.media()
        private, _ = self.media()
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', (self.owner, '[]', json.dumps([private])))
        response = self.alice.post('/api/steward/scan', json={'apply': False})
        self.assertEqual(response.status_code, 202)
        task = self.task('steward_scan')
        self.assertEqual(json.loads(task['payload'])['ids'], [public])
        self.assertIsNone(self.task('transcribe'))
        for body in ({'apply':'yes'}, {'ids':[private]}, [], {'apply':False,'owner':'user:bob'}):
            self.assertEqual(self.alice.post('/api/steward/scan', json=body).status_code, 400)
        for body in ({'automatic':True}, {**steward.DEFAULT_POLICY,'automatic':1}, {**steward.DEFAULT_POLICY,'profile':'unknown'}):
            self.assertEqual(self.alice.post('/api/steward/policy', json=body).status_code, 400)

    def test_live_permission_change_during_get_drops_rows_and_counts(self):
        self.media()
        with patch.object(steward.library_search, 'access_stamp', side_effect=['before','after']):
            response = self.alice.get('/api/steward')
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('items', response.json)

    def test_tag_change_during_get_cannot_release_newly_private_media(self):
        jid, _ = self.media()
        core.q('INSERT INTO privacy(owner,tags,ids) VALUES(?,?,?)', (self.owner, '["Secret"]', '[]'))
        original = steward.q
        def mutate(sql, args=(), one=False):
            result = original(sql, args, one)
            if sql.startswith('SELECT COALESCE(s.state'):
                core.q("UPDATE jobs SET analysis=json_set(analysis,'$.tags',json('[\"Secret\"]')) WHERE id=?", (jid,))
            return result
        with patch.object(steward, 'q', side_effect=mutate):
            response = self.alice.get('/api/steward')
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('items', response.json)

    def test_owner_change_during_get_drops_stale_rows(self):
        jid, _ = self.media()
        original = steward.q
        def mutate(sql, args=(), one=False):
            result = original(sql, args, one)
            if sql.startswith('SELECT COALESCE(s.state'):
                core.q("UPDATE jobs SET owner='user:bob' WHERE id=?", (jid,))
            return result
        with patch.object(steward, 'q', side_effect=mutate):
            response = self.alice.get('/api/steward')
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('items', response.json)

    def test_automatic_and_explicit_scans_do_not_coalesce_each_other(self):
        automatic = steward.queue_scan(self.owner, apply=True, automatic=True)
        readonly = steward.queue_scan(self.owner, ids=[], apply=False)
        explicit = steward.queue_scan(self.owner, ids=[], apply=True)
        self.assertEqual(len({automatic, readonly, explicit}), 3)

    def test_owner_transfer_keeps_existing_account_policy(self):
        jid, _ = self.media(owner='anonymous')
        steward.scan('anonymous', apply=True)
        core.kv_set(steward.POLICY_PREFIX + 'anonymous', {**steward.DEFAULT_POLICY, 'automatic':True})
        core.q('UPDATE jobs SET owner=? WHERE id=?', (self.owner, jid))
        self.policy(profile='essential')
        steward.move_owner('anonymous', self.owner)
        self.assertEqual(steward.policy(self.owner)['profile'], 'essential')
        self.assertFalse(steward.policy(self.owner)['automatic'])
        self.assertEqual(core.q('SELECT owner FROM media_steward_assets WHERE job_id=?', (jid,), one=True)['owner'], self.owner)


if __name__ == '__main__':
    unittest.main()
