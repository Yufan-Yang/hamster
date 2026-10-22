#!/usr/bin/env python3
"""Final live boundaries and complete-topic editing, using a disposable database."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend  # noqa: E402
from shiguang import core, live, live_calibration, live_editorial, live_recorder  # noqa: E402


class LiveFinalizationTests(unittest.TestCase):
    def setUp(self):
        backend.BackendReliability.setUp(self)
        core.q("INSERT INTO users(name,pw) VALUES ('live-editor','')")
        core.q("INSERT INTO devices(id,user) VALUES (?,'live-editor')", (self.owner,))
        with self.client.session_transaction() as session:
            session['user'] = 'live-editor'
        self.owner = 'user:live-editor'
        core.q("INSERT INTO live_sources(id,owner,room_id,name,url) "
               "VALUES (1,?,1,'主播','https://live.bilibili.com/1')", (self.owner,))
        core.q("INSERT INTO live_sessions(key,source_id,owner,topic_title,cursor,topic_start) "
               "VALUES ('session',1,?,'完整讨论',600,0)", (self.owner,))
        root = Path(self.temp.name) / 'live'
        root.mkdir()
        self.recording = root / 'recording.mkv'
        self.recording.write_bytes(b'local recording fixture')
        for target, name, value in ((live, 'ROOT', root), (live_calibration, 'active_rubric', lambda _: {})):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.ratings = {key: 90 for key in ('topic', 'traffic', 'fun', 'completeness')}
        self.switch = {'highlights': [], 'topic_changes': [
            {'at': 700, 'previous_title': '完整讨论', 'previous_kind': 'content',
             'previous_scores': self.ratings, 'next_title': '广告', 'next_kind': 'advertisement'}],
            'current_topic': '广告', 'current_kind': 'advertisement', 'current_summary': '广告'}
        self.approval = {'boundaries': [{'index': 0, 'previous_complete': True,
                                       'next_independent': True, 'confidence': .95,
                                       'reason': '前一个讨论已经结束，随后进入独立广告'}]}

    def recordings(self, count):
        for seq in range(count):
            core.q("INSERT INTO live_recordings(owner,source_id,session_key,seq,offset,duration,state,path,segments) "
                   "VALUES (?,1,'session',?,?,60,'ready',?,?)",
                   (self.owner, seq, seq * 60, str(self.recording), json.dumps([[0, 60, '完整讨论字幕']])) )

    def analyze(self, answers):
        with patch.object(live.llm, 'llm_json', side_effect=answers) as model, \
                patch.object(live, 'queue_render'), patch.object(live_editorial, 'queue_score'):
            result = live.analyze({'payload': {'session': 'session'}}, lambda _: True)
        return result, model

    def assert_content_preserved(self):
        clips = core.q("SELECT start,end,content_kind FROM live_clips WHERE session_key='session'")
        self.assertEqual([tuple(row) for row in clips], [(0, 700, 'content')])
        self.assertEqual(core.q("SELECT topic_start FROM live_sessions WHERE key='session'", one=True)[0], 720)

    def test_recording_stops_during_curation_before_advertising_boundary(self):
        self.recordings(12)
        answers = iter([self.switch, self.approval])

        def model(*_args, **_kwargs):
            core.q("UPDATE live_sessions SET status='ended' WHERE key='session'")
            return next(answers)

        _, calls = self.analyze(model)
        self.assertEqual(calls.call_count, 2)
        self.assert_content_preserved()

    def test_final_pass_rechecks_boundary_after_cursor_has_consumed_last_recording(self):
        self.recordings(12)
        _, model = self.analyze([self.switch])
        self.assertEqual(model.call_count, 1)
        session = core.q("SELECT cursor,topic_start,topic_kind FROM live_sessions WHERE key='session'", one=True)
        self.assertEqual(tuple(session), (720, 0, 'content'))
        core.q("UPDATE live_sessions SET status='ended' WHERE key='session'")
        live_recorder.schedule_analysis()
        task = core.q("SELECT * FROM tasks WHERE kind='live_analyze'", one=True)
        self.assertIsNotNone(task)
        self.assertEqual(task['state'], 'queued')
        self.assertFalse(core.q('SELECT id FROM live_clips'))
        _, model = self.analyze([self.switch, self.approval])
        self.assertEqual(model.call_count, 2)
        self.assert_content_preserved()
        result, model = self.analyze([])
        self.assertEqual(result, {'waiting': True})
        model.assert_not_called()

    def test_rejected_advertising_boundary_does_not_reclassify_previous_content(self):
        self.recordings(12)
        self.analyze([self.switch])
        core.q("UPDATE live_sessions SET status='ended' WHERE key='session'")
        self.analyze([self.switch, {'boundaries': []}])
        clip = core.q('SELECT start,end,content_kind FROM live_clips', one=True)
        self.assertEqual(tuple(clip), (0, 720, 'content'))

    def test_out_of_range_boundary_does_not_set_initial_topic_kind(self):
        self.recordings(2)
        core.q("UPDATE live_sessions SET cursor=0,topic_title='',status='ended' WHERE key='session'")
        self.analyze([{'highlights': [], 'topic_changes': [
            {'at': 20, 'previous_kind': 'advertisement', 'next_kind': 'content'}],
            'current_topic': '完整讨论', 'current_kind': 'content'}])
        clip = core.q('SELECT start,end,content_kind FROM live_clips', one=True)
        self.assertEqual(tuple(clip), (0, 120, 'content'))

    def test_complete_topic_over_an_hour_can_edit_metadata_and_trim_without_splitting(self):
        self.recordings(62)
        core.q("UPDATE live_sessions SET status='ended',cursor=3600 WHERE key='session'")
        self.analyze([{'highlights': [], 'topic_changes': [], 'current_topic': '完整讨论',
                       'current_kind': 'content', 'current_scores': self.ratings}])
        clip = core.q('SELECT * FROM live_clips', one=True)
        self.assertEqual((clip['start'], clip['end']), (0, 3720))
        core.q("UPDATE live_clips SET state='ready',file=?,render_version=version WHERE id=?",
               (str(self.recording), clip['id']))
        body = {'start': 0, 'end': 3720, 'title': '完整故事的新标题', 'cover_title': '新的封面',
                'description': '更新简介', 'tags': ['完整故事']}
        with patch.object(live, 'queue_render') as render, patch.object(live_editorial, 'queue_score'):
            response = self.client.post(f"/api/live/clips/{clip['id']}", json=body)
            self.assertEqual(response.status_code, 200, response.json)
            render.assert_not_called()
            saved = core.q('SELECT * FROM live_clips WHERE id=?', (clip['id'],), one=True)
            self.assertEqual((saved['end'], saved['version'], saved['state']), (3720, clip['version'], 'ready'))
            self.assertEqual(saved['description'], body['description'])
            response = self.client.post(f"/api/live/clips/{clip['id']}", json={**body, 'end': 3660})
            self.assertEqual(response.status_code, 200, response.json)
            render.assert_called_once_with(clip['id'])
            response = self.client.post(f"/api/live/clips/{clip['id']}", json={**body, 'end': 4000})
            self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
