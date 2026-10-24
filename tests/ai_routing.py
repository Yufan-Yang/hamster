"""AI routing regression tests without network calls, credentials or optional ML dependencies.

Run: python3 tests/ai_routing.py
Only load the functions under test, so this also runs away from the Pi.
"""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, **values):
    tree = ast.parse((ROOT / path).read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    scope = dict(json=json, time=time, re=re, Path=Path, traceback=Mock(), subprocess=subprocess, **values)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), scope)
    return scope


class RoutingTests(unittest.TestCase):
    # Worker-only source is validated locally before packing the Pi release.
    def test_worker_reports_real_retry_count_and_typed_failures(self):
        ask = Mock()
        scope = load_functions('mac/mac_worker.py', {'answer_llm', 'UsageLimit', 'TransientCodexError'},
                                       CODEX=Mock(), CODEX_MODEL='gpt-6-luna', ask_codex=ask, log=Mock())
        scope['CODEX'].exists.return_value = True
        ask.side_effect = [scope['TransientCodexError']('SECRET 429'), {'out': {}, 'model': 'gpt-6-luna'}]
        metadata = {}
        with patch.object(time, 'sleep'):
            result = scope['answer_llm']({}, Mock(), {}, activity=metadata)
        self.assertEqual(result['_activity']['retries'], 1)
        self.assertIsNone(result['_activity']['failure_code'])
        self.assertNotIn('SECRET', str(metadata))
        for error, code in [(ValueError('Extra data: line 1'), 'invalid_result'),
                            (scope['UsageLimit']('SECRET quota', time.time() + 10), 'quota_exhausted')]:
            ask.side_effect = error
            metadata = {}
            with self.assertRaises(RuntimeError):
                scope['answer_llm']({}, Mock(), {}, activity=metadata)
            self.assertEqual(metadata['failure_code'], code)
            self.assertEqual(metadata['retries'], 0)

    def route(self, ready=True, result=None, error=None):
        mac = Mock(return_value=result, side_effect=error)
        api = Mock(return_value={'answer': 'deepseek'})
        scope = load_functions('shiguang/llm.py', {'llm_json'}, SUMMARY_LANG='Chinese',
                               CODEX_VIA_MAC=True, codex_ready=Mock(return_value=ready), codex_json=mac, deepseek_json=api)
        out = scope['llm_json']('Return {fields} in {lang}', 'question', {'answer': 'string'}, 100, {}, purpose='search')
        return out, mac, api

    def test_codex_first_for_search(self):
        out, mac, api = self.route(result={'answer': 'codex'})
        self.assertEqual(out, {'answer': 'codex'})
        mac.assert_called_once()
        api.assert_not_called()

    def test_fallback(self):
        for args in ({'ready': False}, {'result': None}, {'error': RuntimeError('failed')}):
            with self.subTest(args=args):
                out, mac, api = self.route(**args)
                self.assertEqual(out, {'answer': 'deepseek'})
                api.assert_called_once()
                if not args.get('ready', True):
                    mac.assert_not_called()

    def queued_request(self, status, ready=True, race=False, run_wait=360):
        now, completed = [0], [False]
        writes, usage = [], {}
        clock = types.SimpleNamespace(time=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        def query(sql, args=(), one=False):
            if not sql.startswith('SELECT'):
                return []
            state = 'done' if completed[0] else status(now[0])
            return {'state': state, 'error': '', 'result': json.dumps({'engine': 'codex', 'out': {'answer': 'ok'}})}
        def write(sql, args):
            writes.append((now[0], sql))
            if race:
                completed[0] = True
                return 0
            return 1
        board = types.SimpleNamespace(publish=Mock(return_value=1), _write=write)
        package = types.ModuleType('_routing_test'); package.board = board
        scope = load_functions('shiguang/llm.py', {'codex_json'}, q=query, secrets=secrets,
                               CODEX_URGENT={'search', 'live-curator'}, CODEX_CLAIM_WAIT=90,
                               CODEX_RUN_WAIT=run_wait, codex_ready=lambda: ready, log_usage=Mock(),
                               __package__='_routing_test')
        scope['time'] = clock
        with patch.dict(sys.modules, {'_routing_test': package}):
            out = scope['codex_json']('sys', 'question', {'answer': 'string'}, 'live-curator', None, usage, True)
        return out, usage, writes, now[0]

    def test_busy_curator_waits_past_old_twenty_second_deadline(self):
        out, usage, writes, _ = self.queued_request(lambda t: 'queued' if t < 30 else 'running' if t < 40 else 'done')
        self.assertEqual(out, {'answer': 'ok'})
        self.assertEqual(usage['codex'], 1)
        self.assertEqual(writes, [])

    def test_queue_stays_bounded_and_explains_fallback(self):
        out, usage, writes, elapsed = self.queued_request(lambda t: 'queued')
        self.assertIsNone(out)
        self.assertEqual(elapsed, 92)
        self.assertIn('queue wait', usage['fallback_reason'])
        self.assertEqual(len(writes), 1)

    def test_disappearing_worker_does_not_wait_the_whole_queue_budget(self):
        out, usage, _, elapsed = self.queued_request(lambda t: 'queued', ready=False)
        self.assertIsNone(out)
        self.assertLess(elapsed, 90)
        self.assertIn('fallback_reason', usage)

    def test_running_timeout_preserves_concurrent_success(self):
        out, usage, writes, _ = self.queued_request(lambda t: 'running', race=True, run_wait=10)
        self.assertEqual(out, {'answer': 'ok'})
        self.assertNotIn('fallback_reason', usage)
        self.assertIn("state IN ('queued','running')", writes[0][1])

    def test_only_active_codex_workers_count(self):
        query = Mock()
        scope = load_functions('shiguang/llm.py', {'codex_ready'}, CODEX_VIA_MAC=True, q=query)
        for caps, paused, expected in [(['llm', 'claude'], None, False), (['codex'], None, False),
                                       (['llm', 'codex'], 'quota', False),
                                       (['llm', 'codex'], None, True)]:
            query.return_value = [{'caps': json.dumps(caps), 'paused': paused}]
            self.assertEqual(scope['codex_ready'](), expected)
        scope['CODEX_VIA_MAC'] = False
        self.assertFalse(scope['codex_ready']())

    def test_old_worker_cannot_take_ai_but_can_still_sign_shortcuts(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript('''CREATE TABLE tasks (id INTEGER PRIMARY KEY, kind TEXT, state TEXT,
                priority INTEGER, created REAL, not_before REAL, worker TEXT, lease_until REAL,
                attempts INTEGER DEFAULT 0, updated REAL, payload TEXT DEFAULT '{}');
                CREATE TABLE workers (name TEXT, caps TEXT, seen REAL);
                INSERT INTO tasks (id, kind, state, priority, created) VALUES
                    (1, 'llm', 'queued', 90, 0), (2, 'shortcut', 'queued', 50, 0);''')

            def query(sql, args=(), one=False):
                rows = db.execute(sql, args)
                return rows.fetchone() if one else rows.fetchall()

            def write(sql, args=()):
                return db.execute(sql, args).rowcount

            scope = load_functions('shiguang/board.py', {'claim_task'}, q=query, _write=write,
                                   seen_worker=Mock(), task_dict=dict, TASK_KINDS={'llm': {}, 'shortcut': {}},
                                   WORKER_FRESH=300, LEASE=300, secrets=secrets)
            claim = scope['claim_task']
            self.assertEqual(claim('old-mac', ['llm', 'claude', 'shortcut'])['id'], 2)
            self.assertIsNone(claim('old-mac', ['llm', 'claude', 'shortcut']))
            self.assertEqual(query('SELECT state FROM tasks WHERE id=1', one=True)['state'], 'queued')
            self.assertEqual(claim('new-mac', ['llm', 'codex', 'shortcut'])['id'], 1)

    @unittest.skipUnless((ROOT / 'mac/mac_worker.py').is_file(), 'Mac worker source is tested in the repository')
    def test_worker_quota_skips_codex(self):
        ask = Mock()
        scope = load_functions('mac/mac_worker.py', {'answer_llm', 'UsageLimit', 'TransientCodexError'}, CODEX=Mock(),
                               ask_codex=ask, log=Mock())
        scope['CODEX'].exists.return_value = True
        limits = {}
        ask.side_effect = scope['UsageLimit']('quota', time.time() + 1800)
        with self.assertRaises(RuntimeError):
            scope['answer_llm']({}, Mock(), limits)
        self.assertIn('codex', limits)
        ask.reset_mock()
        with self.assertRaises(RuntimeError):
            scope['answer_llm']({}, Mock(), limits)
        ask.assert_not_called()

    @unittest.skipUnless((ROOT / 'mac/mac_worker.py').is_file(), 'Mac worker source is tested in the repository')
    def test_codex_rejects_failed_turn_even_with_partial_answer(self):
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"answer": "partial"}'}},
                  {'type': 'turn.failed', 'error': {'message': 'failed'}}]
        scope = load_functions('mac/mac_worker.py', {'ask_codex', 'UsageLimit', 'TransientCodexError'}, CODEX=Path('/codex'), CODEX_MODEL='',
                               run_cli=Mock(return_value=(1, '\n'.join(map(json.dumps, events)), '')))
        with self.assertRaises(RuntimeError):
            scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())

    def codex_events(self, events, code=0):
        return load_functions('mac/mac_worker.py', {'ask_codex', 'UsageLimit', 'TransientCodexError'},
                              CODEX=Path('/codex'), CODEX_MODEL='gpt-6-luna',
                              run_cli=Mock(return_value=(code, '\n'.join(map(json.dumps, events)), '')))

    def test_codex_accepts_completion_after_recovered_stream_error(self):
        events = [{'type': 'error', 'message': 'stream disconnected; reconnecting'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"answer":"complete"}'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}}]
        scope = self.codex_events(events)
        result = scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())
        self.assertEqual(result['out'], {'answer': 'complete'})
        self.assertEqual(result['tokens_out'], 3)
        self.assertEqual(result['model'], 'gpt-6-luna')
        # A terminal failure must still win over a previous completion.
        scope = self.codex_events(events + [{'type': 'turn.failed', 'error': {'message': 'failed'}}])
        with self.assertRaises(RuntimeError):
            scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())

    def test_codex_short_rate_limits_and_network_errors_are_not_quota(self):
        for message in ('HTTP 429 too many concurrent requests, retry in 1 second',
                        'rate limit exceeded', 'stream disconnected before completion',
                        '503 temporarily unavailable'):
            with self.subTest(message=message):
                scope = self.codex_events([{'type': 'turn.failed', 'error': {'message': message}}], 1)
                with self.assertRaises(scope['TransientCodexError']):
                    scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())

    def test_codex_explicit_exhausted_quota_keeps_cooldown(self):
        for detail in ({'message': "You've hit your usage limit. Try again Oct 27."},
                       {'message': 'Request denied', 'code': 'usage_limit_reached'},
                       {'message': 'You exceeded your current quota'},
                       {'message': 'insufficient_quota'}):
            with self.subTest(detail=detail):
                scope = self.codex_events([{'type': 'turn.failed', 'error': detail}], 1)
                with self.assertRaises(scope['UsageLimit']) as caught:
                    scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())
                self.assertGreater(caught.exception.until, time.time() + 1700)

    def worker_scope(self):
        clock = Mock()
        clock.monotonic.return_value = 100
        clock.time.return_value = 100
        ask, beat = Mock(), Mock()
        scope = load_functions('mac/mac_worker.py', {'answer_llm', 'UsageLimit', 'TransientCodexError'},
                               CODEX=Mock(), ask_codex=ask, log=Mock())
        scope['time'] = clock
        scope['CODEX'].exists.return_value = True
        return scope, ask, beat, clock

    def test_codex_transient_failure_retries_without_pausing_worker(self):
        scope, ask, beat, clock = self.worker_scope()
        limits = {}
        ask.side_effect = [scope['TransientCodexError']('429'), scope['TransientCodexError']('network'),
                           {'out': {'answer': 'codex'}}]
        self.assertEqual(scope['answer_llm']({}, beat, limits), {'out': {'answer': 'codex'}})
        self.assertEqual(ask.call_count, 3)
        self.assertEqual([c.args[0] for c in clock.sleep.call_args_list], [2, 5])
        self.assertEqual(beat.call_count, 2)
        self.assertEqual(limits, {})

    def test_codex_transient_retry_is_bounded_by_attempts_time_and_lease(self):
        scope, ask, beat, clock = self.worker_scope()
        ask.side_effect = scope['TransientCodexError']('network')
        with self.assertRaises(RuntimeError):
            scope['answer_llm']({}, beat, {})
        self.assertEqual(ask.call_count, 3)
        scope, ask, beat, clock = self.worker_scope()
        clock.monotonic.side_effect = [100, 100, 399]
        ask.side_effect = scope['TransientCodexError']('network')
        with self.assertRaises(RuntimeError):
            scope['answer_llm']({}, beat, {})
        self.assertEqual(ask.call_count, 1)
        clock.sleep.assert_not_called()
        scope, ask, beat, clock = self.worker_scope()
        ask.side_effect = scope['TransientCodexError']('network')
        beat.side_effect = RuntimeError('task lease was lost')
        with self.assertRaisesRegex(RuntimeError, 'lease was lost'):
            scope['answer_llm']({}, beat, {})
        self.assertEqual(ask.call_count, 1)
        clock.sleep.assert_not_called()

    def test_codex_invalid_output_does_not_repeat_model_calls(self):
        scope, ask, beat, clock = self.worker_scope()
        ask.side_effect = ValueError('invalid JSON')
        with self.assertRaises(RuntimeError):
            scope['answer_llm']({}, beat, {})
        self.assertEqual(ask.call_count, 1)
        clock.sleep.assert_not_called()

    @unittest.skipUnless((ROOT / 'mac/mac_worker.py').is_file() and shutil.which('git'),
                         'Mac healer requires its source and git; tested in the repository')
    def test_healer_patch_stays_within_allowed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (root / 'grabber.py').write_text('old\n')
            (root / 'deploy.sh').write_text('old\n')
            scope = load_functions('mac/mac_worker.py', {'apply_heal_patch'}, HEAL_DIR=root,
                                   HEAL_PATHS=('shiguang/', 'grabber.py', 'index.html', 'tests/'))
            diff = 'diff --git a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n@@ -1 +1 @@\n-old\n+new\n'
            with self.assertRaises(RuntimeError):
                scope['apply_heal_patch'](diff.format('deploy.sh'))
            self.assertEqual((root / 'deploy.sh').read_text(), 'old\n')
            # A mixed diff must be rejected before even its permitted part is applied.
            with self.assertRaises(RuntimeError):
                scope['apply_heal_patch'](diff.format('grabber.py') + diff.format('deploy.sh'))
            self.assertEqual((root / 'grabber.py').read_text(), 'old\n')
            scope['apply_heal_patch'](diff.format('grabber.py'))
            self.assertEqual((root / 'grabber.py').read_text(), 'new\n')


if __name__ == '__main__':
    unittest.main()
