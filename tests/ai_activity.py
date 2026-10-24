"""Routing receipts: real account/privacy scopes and no-network provenance checks."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('activity_fixtures', Path(__file__).with_name('crawl_home_search.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
from shiguang import ai_activity as activity, board, core, llm, web  # noqa: E402

route_spec = importlib.util.spec_from_file_location('route_fixtures', Path(__file__).with_name('ai_routing.py'))
routing = importlib.util.module_from_spec(route_spec)
route_spec.loader.exec_module(routing)


class ActivityTests(unittest.TestCase):
    setUp = fixtures.HomeSearchTests.setUp
    client = fixtures.HomeSearchTests.client
    collection = fixtures.HomeSearchTests.collection
    job = fixtures.HomeSearchTests.job

    def record(self, owner='user:alice', private=False, **values):
        with activity.context(owner, private):
            receipt = activity.begin('search')
        receipt.update(status='done', engine='codex', model='gpt-6-luna', **values)
        activity.finish(receipt)
        return core.q('SELECT * FROM ai_activity ORDER BY id DESC LIMIT 1', one=True)

    def get(self, client=None, **args):
        return (client or self.alice).get('/api/ai/activity', **args)

    def token(self):
        web.privacy_tokens['valid'] = {'owner': 'user:alice', 'expires': time.time() + 60, 'reveal': True}
        return {'X-Privacy-Token': 'valid'}

    def test_owner_admin_system_and_private_counts(self):
        self.record(); self.record('user:bob'); self.record('user:admin')
        self.record(private=True); self.record('', False)
        data = self.get().json
        self.assertEqual(data['total'], 1)
        self.assertEqual(sum(row['total'] for row in data['summary']), 1)
        self.assertEqual(self.get(self.admin).json['total'], 1)
        self.assertEqual(self.get(query_string={'scope': 'system'}).status_code, 403)
        self.assertEqual(self.get(self.admin, query_string={'scope': 'system'}).json['total'], 1)
        self.assertEqual(self.get(headers=self.token()).json['total'], 2)
        self.assertEqual(self.get().headers['Cache-Control'], 'no-store')

    def test_dynamic_job_privacy_alias_tags_and_deleted_jobs(self):
        job = self.job('user:alice', 'private title', tags=['secret'])
        self.record(job_id=job)
        self.assertEqual(self.get().json['total'], 1)
        web.privacy_set('user:alice', ['hidden'], [])
        core.kv_set('tag_aliases', {'secret': 'hidden'})
        self.assertEqual(self.get().json['total'], 0)
        self.assertEqual(self.get(headers=self.token()).json['total'], 1)
        web.privacy_set('user:alice', [], [job])
        self.assertEqual(self.get().json['total'], 0)
        core.q('DELETE FROM jobs WHERE id=?', (job,))
        self.assertEqual(self.get(headers=self.token()).json['total'], 0)

    def test_source_classification_hides_job_receipt(self):
        job = self.job('user:alice', 'source linked')
        self.record(job_id=job)
        core.q("INSERT INTO crawl_sources(owner,url_key,classification) VALUES('user:alice','root','adult')")
        core.q("INSERT INTO crawl_source_jobs(owner,job_id,url_key) VALUES('user:alice',?,'root')", (job,))
        self.assertEqual(self.get().json['total'], 0)
        self.assertEqual(self.get(headers=self.token()).json['total'], 1)

    def test_revoked_token_never_returns_private_rows_or_aggregate(self):
        self.record(private=True)
        self.token()
        original = activity.q
        def revoke(sql, *args, **kwargs):
            rows = original(sql, *args, **kwargs)
            if sql.startswith('SELECT id,ts'):
                web.privacy_tokens.clear()
            return rows
        with patch.object(activity, 'q', side_effect=revoke):
            response = self.get(headers={'X-Privacy-Token': 'valid'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('items', response.json)
        self.assertNotIn('summary', response.json)
        self.assertEqual(self.get(headers={'X-Privacy-Token': 'valid'}).status_code, 403)

    def test_pagination_summary_filter_and_validation(self):
        for purpose, fallback in [('search', None), ('search', 'queue_timeout'), ('ask', None)]:
            self.record(purpose=purpose, fallback_code=fallback)
        first = self.get(query_string={'limit': 1, 'purpose': 'search'}).json
        second = self.get(query_string={'limit': 1, 'offset': 1, 'purpose': 'search'}).json
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['summary'][0]['fallback_rate'], .5)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertFalse(second['more'])
        for args in ({'days': '0'}, {'offset': '-1'}, {'limit': '101'}, {'purpose': 'secret query'}, {'scope': 'alice'}):
            self.assertEqual(self.get(query_string=args).status_code, 400)

    def test_board_and_explicit_provenance_without_prompt_inspection(self):
        core.q("INSERT INTO live_sessions(key,owner,source_id) VALUES('session-a','user:alice',1)")
        core.q("INSERT INTO tasks(kind,target,payload) VALUES('live_analyze','target',?)", (json.dumps({'session': 'session-a'}),))
        tid = core.q('SELECT MAX(id) n FROM tasks', one=True)['n']
        with patch.object(board._execution, 'claim', (tid, 'worker', 1, 'token'), create=True):
            receipt = activity.begin('live-curator')
        self.assertEqual(receipt['owner'], 'user:alice')
        self.assertFalse(receipt['private'])
        with activity.context('user:bob', True):
            receipt = activity.begin('crawl-source')
        self.assertEqual(receipt['owner'], 'user:bob')
        self.assertTrue(receipt['private'])
        receipt = activity.begin('invented-prompt-text')
        self.assertEqual(receipt['purpose'], 'other')
        self.assertEqual(receipt['owner'], '')
        self.assertTrue(receipt['private'])

    def test_one_success_receipt_per_route_usage_never_contains_trace(self):
        usage = {}
        def codex(*args):
            args[5]['_route_activity'].update(engine='codex', model='gpt-6-luna', run_seconds=2.5, retries=1)
            return {'answer': 'private response'}
        with activity.context('user:alice', False), patch.object(llm, 'codex_ready', return_value=True), \
                patch.object(llm, 'codex_json', side_effect=codex), patch.object(llm, 'deepseek_json') as api:
            self.assertEqual(llm.llm_json('{fields}', 'SECRET PROMPT', {'answer': 'string'}, 50, usage), {'answer': 'private response'})
        api.assert_not_called()
        rows = core.q('SELECT * FROM ai_activity')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['status'], rows[0]['model'], rows[0]['retries']), ('done', 'gpt-6-luna', 1))
        self.assertNotIn('_route_activity', usage)
        self.assertNotIn('SECRET', str(dict(rows[0])))
        self.assertNotIn('private response', str(dict(rows[0])))

    def test_fallback_actual_api_model_and_failure_without_error_text(self):
        response = Mock(status_code=200)
        response.json.return_value = {'model': 'deepseek-actual', 'usage': {}, 'choices': [{'message': {'content': '{"answer":"ok"}'}}]}
        with activity.context('user:alice', False), patch.object(llm, 'codex_ready', return_value=False), \
                patch.object(llm, 'LLM_API_KEY', 'TOP-SECRET'), patch.object(llm, 'LLM_BASE_URL', 'https://api.deepseek.com'), \
                patch.object(llm.requests, 'post', return_value=response):
            llm.llm_json('{fields}', 'SECRET QUERY', {'answer': 'string'}, 50, {}, 'search')
        row = dict(core.q('SELECT * FROM ai_activity', one=True))
        self.assertEqual((row['engine'], row['model'], row['fallback_code']), ('deepseek', 'deepseek-actual', 'worker_unavailable'))
        self.assertEqual(row['retries'], 0)
        self.assertIsNotNone(row['run_seconds'])
        with activity.context('user:alice', False), patch.object(llm, 'codex_ready', return_value=False), \
                patch.object(llm, 'LLM_API_KEY', 'TOP-SECRET'), patch.object(llm.requests, 'post', side_effect=RuntimeError('SECRET exception')):
            with self.assertRaisesRegex(RuntimeError, 'SECRET exception'):
                llm.llm_json('{fields}', 'SECRET QUERY', {'answer': 'string'}, 50, {}, 'search')
        rows = [dict(r) for r in core.q('SELECT * FROM ai_activity')]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['status'], 'failed')
        self.assertNotIn('SECRET', str(rows))

    def test_missing_key_and_broken_logger_do_not_change_routing(self):
        with activity.context('user:alice', False), patch.object(llm, 'codex_ready', return_value=False), patch.object(llm, 'LLM_API_KEY', ''):
            with self.assertRaises(RuntimeError):
                llm.llm_json('{fields}', 'x', {'answer': 'string'}, 50, {}, 'ask')
        row = core.q('SELECT * FROM ai_activity', one=True)
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['engine'])
        with patch.object(activity, 'finish', side_effect=RuntimeError('disk full')), \
                patch.object(llm, 'codex_ready', return_value=True), patch.object(llm, 'codex_json', return_value={'answer': 'ok'}):
            self.assertEqual(llm.llm_json('{fields}', 'x', {'answer': 'string'}, 50, {}, 'ask'), {'answer': 'ok'})

    def test_codex_typed_failure_not_quota_guessed_from_raw_error(self):
        for metadata, expected in [({}, 'execution_failed'), ({'failure_code': 'invalid_result', 'retries': 0}, 'invalid_result'),
                                   ({'failure_code': 'quota_exhausted', 'retries': 0}, 'quota_exhausted')]:
            receipt = activity.begin('search')
            def query(sql, args=(), one=False):
                if sql.startswith('SELECT state'):
                    return {'state': 'failed', 'result': None, 'error': 'SECRET quota Extra data: line 1',
                            'progress': json.dumps({'ai_activity': metadata})}
                return []
            with patch.object(llm, 'q', side_effect=query), patch.object(board, 'publish', return_value=1):
                out = llm.codex_json('s', 'u', {}, 'search', None, {'_route_activity': receipt}, False)
            self.assertIsNone(out)
            self.assertEqual(receipt['fallback_code'], expected)
            self.assertNotIn('SECRET', str(receipt))

    def test_retention_bound_and_owner_move(self):
        with patch.object(activity, 'MAX_RECORDS', 3):
            self.record(ts=time.time() - 31 * 86400)
            for _ in range(5):
                self.record()
        self.assertEqual(core.q('SELECT COUNT(*) n FROM ai_activity', one=True)['n'], 3)
        activity.move_owner('user:alice', 'user:bob')
        self.assertEqual(self.get().json['total'], 0)
        self.assertEqual(self.get(self.bob).json['total'], 3)
        core.DB.execute('BEGIN')
        receipt = activity.begin('search')
        activity.finish(receipt)
        self.assertTrue(core.DB.in_transaction)
        core.DB.rollback()


    def test_log_cleanup_failure_rolls_back_only_its_transaction(self):
        receipt = activity.begin('search')
        real = core.DB
        proxy = Mock(wraps=real)
        proxy.in_transaction = False
        def execute(sql, args=()):
            if sql.startswith('DELETE FROM ai_activity'):
                raise RuntimeError('simulated cleanup failure')
            return real.execute(sql, args)
        proxy.execute.side_effect = execute
        with patch.object(core, 'DB', proxy):
            activity.finish(receipt)
        proxy.rollback.assert_called_once()
        self.assertFalse(real.in_transaction)
        self.assertEqual(core.q('SELECT COUNT(*) n FROM ai_activity', one=True)['n'], 0)
        self.record()
        self.assertEqual(self.get().json['total'], 1)


if __name__ == '__main__':
    unittest.main()
