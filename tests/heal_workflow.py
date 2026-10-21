#!/usr/bin/env python3
"""Offline automatic repair gates: no model, production or network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock

from ai_routing import load_functions

ROOT = Path(__file__).resolve().parents[1]


class HealWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.git('config', 'user.name', 'Heal Test')
        self.git('config', 'user.email', 'heal@example.invalid')
        (self.root / 'grabber.py').write_text('VALUE = 1\n')
        (self.root / 'tests').mkdir()
        (self.root / 'tests/existing.py').write_text('old\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.base = self.git('rev-parse', 'HEAD')

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.root).decode().strip()

    def scope(self, *names, **values):
        return load_functions('mac/mac_worker.py', set(names), HEAL_DIR=self.root,
            HEAL_PATHS=('shiguang/', 'grabber.py', 'index.html', 'static/', 'tests/'),
            REPO=self.root, git=self.git, tempfile=tempfile, shutil=shutil, os=os, sys=sys, **values)

    def test_existing_tests_cannot_be_weakened_or_exact_filename_prefix_bypassed(self):
        apply = self.scope('apply_heal_patch')['apply_heal_patch']
        for name in ('tests/existing.py', 'grabber.py.evil'):
            patch = f'diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-old\n+new\n'
            with self.assertRaises(RuntimeError):
                apply(patch)
        self.assertEqual((self.root / 'tests/existing.py').read_text(), 'old\n')

    def test_model_claim_needs_high_confidence_and_evidence(self):
        answer = dict(state='fixed', diagnosis='cause', summary='fix', retry=True,
                      confidence='low', evidence='file:1', patch='patch')
        apply = Mock()
        scope = self.scope('ask_healer', HEAL_SYSTEM='', HEAL_SCHEMA={'required':list(answer)},
            HEAL_MODEL='', CONFIDENCE={'high':'高','medium':'中','low':'低'},
            heal_prompt=Mock(return_value=''), ask_codex=Mock(return_value={'out':answer}), apply_heal_patch=apply)
        with self.assertRaisesRegex(RuntimeError, 'high-confidence'):
            scope['ask_healer']({}, Mock(), True)
        apply.assert_not_called()

    def test_uncertain_followup_cannot_publish_leftover_patch(self):
        for state, confidence in [('diagnosed', 'low'), ('diagnosed', 'high'), ('fixed', 'low')]:
            with self.subTest(state=state, confidence=confidence):
                initial = dict(state='fixed', diagnosis='first patch', summary='fix', retry=True,
                               confidence='high', evidence='grabber.py:1', patch='first patch')
                followup = dict(initial, state=state, confidence=confidence, patch='')
                ask = Mock(side_effect=[initial, followup])
                # The first patch remains in the worktree after a failed regression.
                git = Mock(side_effect=lambda *args, **kwargs:
                           ' M grabber.py' if args[0] == 'status' else '')
                regressions = Mock(side_effect=[
                    RuntimeError('baseline did not reproduce'),
                    AssertionError('uncertain followup must stop before validating leftover changes'),
                ])
                run = Mock(return_value=Mock(returncode=0))
                scope = load_functions('mac/mac_worker.py', {'heal'},
                    REPO=self.root, HEAL_DIR=self.root / 'mock-worktree',
                    HEAL_PATHS=('shiguang/', 'grabber.py', 'index.html', 'static/', 'tests/'),
                    HEAL_ROUNDS=2, CONFIDENCE={'high':'高', 'low':'低'},
                    deployed_revision=Mock(return_value=self.base), git=git,
                    pi_evidence=Mock(return_value='offline evidence'), ask_healer=ask,
                    heal_regressions=regressions, shutil=shutil)
                scope['subprocess'] = Mock(run=run, TimeoutExpired=subprocess.TimeoutExpired)
                result = scope['heal']({'payload': {'incident': 1}}, Mock())
                self.assertEqual(ask.call_count, 2)
                regressions.assert_called_once()
                self.assertEqual(result['state'], 'diagnosed')
                self.assertFalse(result['retry'])
                self.assertEqual(result['verification']['status'], 'rejected')
                self.assertEqual(result['verification']['base'], self.base)
                # Only the local baseline existence check ran; no deploy subprocess.
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][:3], ['git', 'cat-file', '-e'])
                self.assertFalse(any(call.args[0] in ('add', 'commit') for call in git.call_args_list))

    def test_regression_must_fail_baseline_and_pass_candidate(self):
        test = self.root / 'tests/heal_regression_value.py'
        test.write_text("import unittest\nfrom pathlib import Path\nclass Test(unittest.TestCase):\n"
                        " def test_value(self):\n  self.assertIn('VALUE = 2', Path('grabber.py').read_text())\n"
                        "if __name__ == '__main__': unittest.main()\n")
        (self.root / 'grabber.py').write_text('VALUE = 2\n')
        check = self.scope('heal_regressions')['heal_regressions']
        self.assertEqual(check(self.base, Mock()), ['tests/heal_regression_value.py'])
        test.write_text("import unittest\nclass Test(unittest.TestCase):\n"
                        " def test_noop(self): self.assertTrue(True)\n"
                        "if __name__ == '__main__': unittest.main()\n")
        with self.assertRaisesRegex(RuntimeError, '原版本应复现'):
            check(self.base, Mock())

    def test_release_receipt_must_match_revision_and_new_validation(self):
        receipt = {'revision':'a'*40,'verified_at':100,
                   'checks':['source_hashes','page','task_api','fresh_pi_cpu']}
        run = Mock(return_value=Mock(returncode=0, stdout=json.dumps(receipt)))
        scope = load_functions('mac/mac_worker.py', {'verify_heal_release'},
                               PI_SSH='local-test', deployed_revision=Mock(return_value='a'*40))
        scope['subprocess'] = Mock(run=run)
        self.assertEqual(scope['verify_heal_release']('a'*40, 99), receipt)
        for revision, started in [('b'*40,99),('a'*40,101)]:
            with self.assertRaises(RuntimeError):
                scope['verify_heal_release'](revision, started)

    def test_unverified_model_success_cannot_mark_fixed_or_retry_tasks(self):
        write = Mock()
        scope = load_functions('shiguang/heal.py', {'heal_done'}, _write=write, q=Mock(), board=Mock())
        scope['heal_done']({'payload':json.dumps({'incident':1})},
                           {'state':'fixed','diagnosis':'model says fixed','retry':True})
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.args[1][0], 'diagnosed')

    def test_healer_heartbeat_keeps_worker_identity_in_background_thread(self):
        class StopLoop(BaseException):
            pass
        heartbeats = []
        task = {'id':1, 'claim_token':'token', 'payload':{'title':'fixture'}}
        def call(path, **body):
            if path.endswith('/claim'):
                return {'task':task}
            if path.endswith('/heartbeat'):
                heartbeats.append(body)
                return {'ok':True}
            raise StopLoop()
        def heal(claimed, beat):
            thread = threading.Thread(target=beat)
            thread.start()
            thread.join()
            return {'state':'diagnosed'}
        scope = self.scope('heal_loop', 'task_heartbeat', NAME='mac-test',
            me=threading.local(), CODEX=Mock(), call=call, heal=heal, log=Mock(),
            requests=types.SimpleNamespace(RequestException=Exception))
        with self.assertRaises(StopLoop):
            scope['heal_loop']()
        self.assertEqual(heartbeats, [{'claim_token':'token','worker':'mac-test-heal'}])


if __name__ == '__main__':
    unittest.main()
