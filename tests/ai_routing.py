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
import tempfile
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, **values):
    tree = ast.parse((ROOT / path).read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    scope = dict(json=json, time=time, re=re, Path=Path, traceback=Mock(), subprocess=subprocess, **values)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), scope)
    return scope


class RoutingTests(unittest.TestCase):
    def route(self, ready=True, result=None, error=None):
        mac = Mock(return_value=result, side_effect=error)
        api = Mock(return_value={'answer': 'deepseek'})
        scope = load_functions('shiguang/llm.py', {'llm_json'}, SUMMARY_LANG='Chinese',
                               codex_ready=Mock(return_value=ready), codex_json=mac, deepseek_json=api)
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
        scope = load_functions('mac/mac_worker.py', {'answer_llm', 'UsageLimit'}, CODEX=Mock(),
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
        scope = load_functions('mac/mac_worker.py', {'ask_codex', 'UsageLimit'}, CODEX=Path('/codex'), CODEX_MODEL='',
                               run_cli=Mock(return_value=(1, '\n'.join(map(json.dumps, events)), '')))
        with self.assertRaises(RuntimeError):
            scope['ask_codex']({'system': 'sys', 'user': 'text'}, Mock())

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
