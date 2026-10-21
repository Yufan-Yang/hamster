#!/usr/bin/env python3
"""Live review, pagination and action explanations, with an isolated database."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend  # noqa: E402
from shiguang import core, live, live_calibration  # noqa: E402


class LiveReviewTests(unittest.TestCase):
    def setUp(self):
        backend.BackendReliability.setUp(self)
        core.q("INSERT INTO users(name,pw) VALUES ('reviewer','')")
        core.q("INSERT INTO devices(id,user) VALUES (?,'reviewer')", (self.owner,))
        with self.client.session_transaction() as session:
            session['user'] = 'reviewer'
        self.owner = 'user:reviewer'
        config = json.dumps({**live.settings(), 'auto_topics': True})
        for sid, owner in ((1, self.owner), (2, self.owner), (3, 'user:foreign')):
            core.q("INSERT INTO live_sources(id,owner,room_id,name,url,enabled,settings) VALUES (?,?,?,'主播',?,1,?)",
                   (sid, owner, sid, f'https://live.bilibili.com/{sid}', config))
            core.q("INSERT INTO live_sessions(key,source_id,owner) VALUES (?,?,?)", (f's{sid}', sid, owner))
        core.q("INSERT INTO live_accounts(owner,status) VALUES (?,'ready')", (self.owner,))
        for target, name, value in (
            (live.bili, 'ready', Mock(return_value=True)),
            (live.bili, 'account_path', Mock(return_value=Mock(exists=Mock(return_value=True)))),
            (live_calibration, 'active_rubric', Mock(return_value={'validated': True})),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def clip(self, **changes):
        values = dict(owner=self.owner, source_id=1, session_key='s1', lane='highlight',
                      start=0, end=30, title='片段', state='ready', file='/unused/test.mp4',
                      version=1, render_version=1, score=90,
                      scores=json.dumps({'topic': 90, 'traffic': 90, 'fun': 90, 'completeness': 90}),
                      created=time.time(), updated=time.time())
        values.update(changes)
        core.q(f"INSERT INTO live_clips ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
               tuple(values.values()))
        return core.q('SELECT MAX(id) id FROM live_clips', one=True)['id']

    def get(self, **params):
        response = self.client.get('/api/live', query_string={'tab': 'review', **params})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_filters_and_counts_find_old_items_before_limit(self):
        highlight = self.clip(needs_review=1)
        failed = self.clip(lane='topic', state='failed')
        source_two = self.clip(source_id=2, session_key='s2', lane='topic', needs_review=1)
        for _ in range(170):
            self.clip(lane='topic', state='queued')
        self.clip(owner='user:foreign', source_id=3, session_key='s3', state='uncertain')
        self.assertEqual([c['id'] for c in self.get(tab='highlight')['clips']], [highlight])
        data = self.get(tab='topic', state='failed')
        self.assertEqual([c['id'] for c in data['clips']], [failed])
        self.assertEqual(data['counts'], {'review': 3, 'highlight': 1, 'topic': 172})
        source = self.get(tab='topic', source=2)
        self.assertEqual([c['id'] for c in source['clips']], [source_two])
        self.assertEqual(source['counts'], {'review': 1, 'highlight': 0, 'topic': 1})
        self.assertEqual(self.get(source=3)['total'], 0)
        self.assertEqual({c['id'] for c in self.get()['clips']}, {highlight, failed, source_two})

    def test_cursor_pages_preserve_review_priority_and_full_total(self):
        ready = [self.clip(needs_review=1) for _ in range(4)]
        failed = [self.clip(state='failed') for _ in range(3)]
        uncertain = [self.clip(state='uncertain') for _ in range(2)]
        expected = list(reversed(uncertain)) + list(reversed(failed)) + list(reversed(ready))
        found, cursor = [], None
        while True:
            data = self.get(limit=2, **({'cursor': cursor} if cursor else {}))
            self.assertEqual(data['total'], 9)
            self.assertEqual(data['counts']['review'], 9)
            found.extend(c['id'] for c in data['clips'])
            if not data['has_more']:
                self.assertIsNone(data['next_cursor'])
                break
            cursor = data['next_cursor']
        self.assertEqual(found, expected)
        first = self.get(tab='highlight', limit=2)
        second = self.get(tab='highlight', limit=2, cursor=first['next_cursor'])
        self.assertEqual([c['id'] for c in first['clips'] + second['clips']],
                         sorted(expected, reverse=True)[:4])

    def test_waiting_automatic_and_scoring_jobs_do_not_require_attention(self):
        automatic = self.clip()
        queued = self.clip(state='queued', needs_review=1)
        scoring = self.clip(lane='topic', scores='{}', score=0)
        core.q("INSERT INTO tasks(kind,target,state,payload) VALUES ('live_score','score','queued',?)",
               (json.dumps({'clip': scoring}),))
        self.assertEqual(self.get()['total'], 0)
        data = self.get(tab='highlight')
        self.assertEqual({c['id'] for c in data['clips']}, {automatic, queued})
        self.assertTrue(all(c['action'] == 'wait' and not c['needs_attention'] for c in data['clips']))
        self.assertEqual(self.get(tab='topic')['clips'][0]['action'], 'wait')

    def test_clear_actions_match_account_calibration_score_and_daily_limits(self):
        cid = self.clip(needs_review=1)
        self.assertEqual(self.get()['clips'][0]['action'], 'edit')
        core.q("UPDATE live_accounts SET status='needs_login' WHERE owner=?", (self.owner,))
        item = self.get()['clips'][0]
        self.assertEqual(item['action'], 'bind_account')
        self.assertFalse(item['can_upload'])
        core.q("UPDATE live_accounts SET status='ready' WHERE owner=?", (self.owner,))
        core.q("UPDATE live_clips SET needs_review=0 WHERE id=?", (cid,))
        live_calibration.active_rubric.return_value = {}
        item = self.get()['clips'][0]
        self.assertEqual(item['action'], 'calibrate')
        self.assertTrue(item['can_upload'])  # Manual publishing remains available without auto calibration.
        live_calibration.active_rubric.return_value = {'validated': True}
        core.q("UPDATE live_clips SET lane='topic',scores='{}' WHERE id=?", (cid,))
        self.assertEqual(self.get()['clips'][0]['action'], 'score')
        core.q("UPDATE live_sources SET settings=json_set(settings,'$.daily_limit',1) WHERE id=1")
        self.clip(state='submitted', submit_started=time.time(), bvid='BV1234567890')
        item = self.get()['clips'][0]
        self.assertEqual(item['action'], 'configure')
        self.assertFalse(item['can_upload'])
        self.assertIn('上限', item['review_reason'])

    def test_overlap_requires_manual_decision_and_uncertain_is_first(self):
        topic = self.clip(lane='topic')
        self.clip(state='submitted', bvid='BV1234567890')
        uncertain = self.clip(state='uncertain')
        data = self.get()
        self.assertEqual(data['clips'][0]['id'], uncertain)
        self.assertEqual(data['clips'][0]['action'], 'check_publish')
        self.assertFalse(data['clips'][0]['can_edit'])
        item = next(c for c in data['clips'] if c['id'] == topic)
        self.assertEqual(item['action'], 'publish')
        self.assertIn('相同时间', item['review_reason'])

    def test_removed_merged_and_advertising_items_are_not_review_counts(self):
        self.clip(needs_review=1, removed_at=time.time())
        self.clip(needs_review=1, merged_into=999)
        self.clip(needs_review=1, content_kind='advertisement')
        data = self.get()
        self.assertEqual(data['counts'], {'review': 0, 'highlight': 0, 'topic': 0})
        self.assertEqual(data['clips'], [])

    def test_invalid_filters_return_400_and_anonymous_is_denied(self):
        for params in ({'source': '-1'}, {'source': '9' * 30}, {'state': 'unknown'},
                       {'limit': 'bad'}, {'limit': 0}, {'limit': 301},
                       {'cursor': 'wrong'}, {'cursor': '0:9999999999999999999'}):
            self.assertEqual(self.client.get('/api/live', query_string={'tab': 'review', **params}).status_code, 400)
        self.assertEqual(core.app.test_client().get('/api/live?tab=review').status_code, 401)

    def test_automatic_policy_keeps_manual_and_unscored_guards(self):
        cid = self.clip()
        row = core.q('SELECT * FROM live_clips WHERE id=?', (cid,), one=True)
        source = core.q('SELECT * FROM live_sources WHERE id=1', one=True)
        self.assertTrue(live.automatic_allowed(row, source))
        for changes in ({'needs_review': 1}, {'error': 'problem'}, {'render_version': 0},
                        {'lane': 'topic', 'scores': '{}'}, {'content_kind': 'advertisement'},
                        {'score': 50}, {'merged_into': 999}):
            self.assertFalse(live.automatic_allowed({**dict(row), **changes}, source), changes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
