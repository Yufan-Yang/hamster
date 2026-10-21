#!/usr/bin/env python3
"""Isolated task lifecycle regressions; no production database, media or model calls."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shiguang import board, core, heal, llm, tasks  # noqa: E402


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="grabber-task-lifecycle-")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        for target, name, value in (
            (core, "STATE", folder), (core, "DB_PATH", folder / "test.db"), (core, "DB", None),
            (board, "_seen_written", {}), (board, "TASK_KINDS", dict(board.TASK_KINDS)),
            (board, "COMPUTE_TOKEN", "lifecycle-test-token"),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        healing = patch.object(heal, "task_failed")
        healing.start()
        self.addCleanup(healing.stop)
        core.init_db()
        self.addCleanup(core.DB.close)
        configuration = patch.dict(core.app.config, TESTING=True, SECRET_KEY="lifecycle-test")
        configuration.start()
        self.addCleanup(configuration.stop)

    def register(self, name="audit", run=None, then=None):
        return board.task(name, name, "light", then=then)(run or (lambda task, beat: {}))

    def claim(self, kind="audit", worker="worker", payload=None):
        tid = board.publish(kind, f"audit:{time.time_ns()}", payload=payload or {})
        claimed = board.claim_task(worker, [kind])
        self.assertEqual(claimed["id"], tid)
        return claimed

    def row(self, tid):
        return core.q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)

    def retry_followup(self):
        child = core.q("SELECT * FROM tasks WHERE kind='task_followup' ORDER BY id DESC", one=True)
        core.q("UPDATE tasks SET not_before=0 WHERE id=?", (child["id"],))
        claim = board.claim_task("retry", ["task_followup"])
        self.assertEqual(claim["id"], child["id"])
        return board.run_claimed(claim, "retry")

    def test_followup_failure_keeps_result_and_retries_without_compute(self):
        calls = []

        def finish(task, result):
            calls.append(result)
            if len(calls) == 1:
                raise OSError("temporary write failure")
            core.kv_set("finished", result)

        self.register(then=finish)
        task = self.claim()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(board.complete_task(task["id"], "worker", {"answer": 42}))
        original = self.row(task["id"])
        self.assertEqual(original["state"], "done")
        self.assertEqual(json.loads(original["result"]), {"answer": 42})
        followup = core.q("SELECT * FROM tasks WHERE kind='task_followup'", one=True)
        self.assertEqual(followup["state"], "queued")
        self.assertIn("temporary write failure", followup["error"])
        self.assertEqual(self.retry_followup(), "done")
        self.assertEqual(core.kv_get("finished"), {"answer": 42})
        self.assertEqual(calls, [{"answer": 42}, {"answer": 42}])
        self.assertFalse(board.complete_task(task["id"], "worker", {"answer": "duplicate"}))
        self.assertEqual(core.q("SELECT count(*) n FROM tasks WHERE kind='task_followup'", one=True)["n"], 1)

    def test_followup_survives_interruption_after_completion_commit(self):
        called = []
        self.register(then=lambda task, result: called.append(result))
        task = self.claim()
        with patch.object(board, "run_claimed", return_value="interrupted"):
            self.assertTrue(board.complete_task(task["id"], "worker", {"answer": 42}))
        core.q("UPDATE tasks SET lease_until=0 WHERE kind='task_followup'")
        self.assertEqual(self.retry_followup(), "done")
        self.assertEqual(called, [{"answer": 42}])

    def test_followup_skips_result_consumed_or_source_rerun(self):
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                core.q("DELETE FROM tasks")
                called = []
                self.register(then=lambda task, result: called.append(result))
                task = self.claim()
                with patch.object(board, "run_claimed", return_value="interrupted"):
                    board.complete_task(task["id"], "worker", {})
                if consumed:
                    core.q("UPDATE tasks SET result='{\"saved\":true}' WHERE id=?", (task["id"],))
                else:
                    core.q("UPDATE tasks SET state='queued',updated=updated+1 WHERE id=?", (task["id"],))
                core.q("UPDATE tasks SET lease_until=0 WHERE kind='task_followup'")
                self.assertEqual(self.retry_followup(), "done")
                self.assertEqual(called, [])

    def test_followup_manual_retry_inherits_source_permissions(self):
        core.q("INSERT INTO jobs(id,url,owner,status) VALUES(1,'https://example.test/1','alice','done')")
        original = board.publish("transcribe", "job:1:0", payload={"job": 1, "part": 0})
        child = board.publish("task_followup", "audit:retry", payload={"_completed_task": original})
        self.assertTrue(board.retry_allowed(self.row(child), "alice"))
        self.assertFalse(board.retry_allowed(self.row(child), "bob"))
        self.assertTrue(board.retry_allowed(self.row(child), "admin", admin=True))
        core.q("UPDATE tasks SET kind='live_upload' WHERE id=?", (original,))
        self.assertFalse(board.retry_allowed(self.row(child), "alice"))

    def test_transcription_followup_is_idempotent_for_usage_and_saver(self):
        core.q("INSERT INTO jobs(id,url,status,title,files,analysis) VALUES(1,'https://example.test/1','done','test','[]','{}')")
        task = {
            "id": 10, "worker": "mac", "attempts": 1, "updated": 123.0,
            "target": "job:1:0", "priority": 50, "payload": '{"job":1,"part":0}',
        }
        with patch.object(board, "task_payload", return_value={"job": 1, "part": 0}):
            tasks.after_transcribe(task, {"audio_seconds": 60, "seconds": 2})
            tasks.after_transcribe(task, {"audio_seconds": 60, "seconds": 2})
        self.assertEqual(core.q("SELECT count(*) n FROM usage WHERE kind='whisper'", one=True)["n"], 1)
        self.assertEqual(core.q("SELECT count(*) n FROM tasks WHERE kind='save_subs'", one=True)["n"], 1)
        task["updated"] = 124.0
        with patch.object(board, "task_payload", return_value={"job": 1, "part": 0}):
            tasks.after_transcribe(task, {"audio_seconds": 60, "seconds": 2})
        self.assertEqual(core.q("SELECT count(*) n FROM usage WHERE kind='whisper'", one=True)["n"], 2)

    def test_blocked_local_work_renews_lease(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        def slow(task, beat):
            entered.set()
            self.assertTrue(release.wait(2))
            return {"done": True}

        self.register(run=slow)
        with patch.object(board, "LEASE", .15):
            task = self.claim()
            worker = threading.Thread(target=lambda: results.append(board.run_claimed(task, "worker")))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                time.sleep(.28)  # beyond the original lease, but below the renewed one
                self.assertIsNone(board.claim_task("other", ["audit"]))
                self.assertEqual(self.row(task["id"])["worker"], "worker")
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(results, ["done"])

    def make_summary(self):
        before = {"title": "original title", "summary": "old summary", "tags": ["original"],
                  "usage": {"calls": 2}, "chapters": {"0": [{"t": 0, "title": "original"}]}}
        core.q("INSERT INTO jobs(id,url,status,analysis,files,transcript) "
               "VALUES(1,'https://example.test/1','done',?,'[]','original transcript')",
               (json.dumps(before),))
        return self.claim("summarize", payload={"job": 1})

    def run_summary(self, task, model):
        with patch.object(llm, "timed_transcript", return_value=("", 0, 0)), \
                patch.object(llm, "summarize", side_effect=model), patch.object(llm, "merge_tags_if_new"):
            return board.run_claimed(task, "worker")

    def test_reclaimed_attempt_cannot_write_even_when_worker_name_is_reused(self):
        for owner in ("other", "worker"):
            with self.subTest(owner=owner):
                core.q("DELETE FROM jobs")
                core.q("DELETE FROM tasks")
                task = self.make_summary()

                def model(jid, analysis, transcript, note):
                    core.q("UPDATE tasks SET worker=?,attempts=attempts+1 WHERE id=?", (owner, task["id"]))
                    analysis["summary"] = "stale result"
                    return analysis

                self.assertEqual(self.run_summary(task, model), "lost")
                self.assertEqual(json.loads(core.q("SELECT analysis FROM jobs WHERE id=1", one=True)["analysis"])["summary"],
                                 "old summary")
                self.assertEqual(self.row(task["id"])["state"], "running")
                self.assertEqual(self.row(task["id"])["attempts"], 2)
                self.assertFalse(board.complete_task(task["id"], "worker", {}, attempt=1))
                self.assertFalse(board.heartbeat_task(task["id"], "worker", attempt=1))
                self.assertFalse(board.fail_task(task["id"], "worker", "late failure", attempt=1))
                board.release_task(task["id"], "worker", attempt=1)
                self.assertEqual(self.row(task["id"])["state"], "running")

    def test_summary_preserves_concurrent_metadata_and_merges_only_additions(self):
        task = self.make_summary()

        def model(jid, analysis, transcript, note):
            latest = json.loads(core.q("SELECT analysis FROM jobs WHERE id=1", one=True)["analysis"])
            latest.update(title="user title", tags=["user-tag"], usage={"calls": 5})
            latest["chapters"]["1"] = [{"t": 0, "title": "other part"}]
            core.update(1, analysis=latest)
            analysis.update(summary="fresh summary", tags=["original", "model-tag"], usage={"calls": 3})
            return analysis

        self.assertEqual(self.run_summary(task, model), "done")
        result = json.loads(core.q("SELECT analysis FROM jobs WHERE id=1", one=True)["analysis"])
        self.assertEqual(result["title"], "user title")
        self.assertEqual(result["tags"], ["user-tag", "model-tag"])
        self.assertEqual(result["chapters"]["1"], [{"t": 0, "title": "other part"}])
        self.assertEqual(result["summary"], "fresh summary")
        self.assertEqual(result["usage"]["calls"], 6)

    def test_summary_keeps_a_concurrent_manual_summary(self):
        task = self.make_summary()

        def model(jid, analysis, transcript, note):
            core.q("UPDATE jobs SET analysis=json_set(analysis,'$.summary','manual summary') WHERE id=1")
            analysis["summary"] = "generated summary"
            return analysis

        self.assertEqual(self.run_summary(task, model), "done")
        self.assertEqual(json.loads(core.q("SELECT analysis FROM jobs WHERE id=1", one=True)["analysis"])["summary"],
                         "manual summary")

    def test_changed_transcript_does_not_receive_stale_summary(self):
        task = self.make_summary()

        def model(jid, analysis, transcript, note):
            core.update(1, transcript="new transcript")
            analysis["summary"] = "stale summary"
            return analysis

        self.assertEqual(self.run_summary(task, model), "done")
        self.assertEqual(json.loads(core.q("SELECT analysis FROM jobs WHERE id=1", one=True)["analysis"])["summary"],
                         "old summary")

    def test_guarded_mutation_rolls_back_on_error(self):
        def broken(task, beat):
            with board.mutation() as conn:
                conn.execute("INSERT INTO kv(k,v) VALUES('partial','true')")
                raise OSError("mid-transaction failure")

        self.register(run=broken)
        task = self.claim()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(board.run_claimed(task, "worker"), "failed")
        self.assertIsNone(core.kv_get("partial"))
        self.assertEqual(self.row(task["id"])["state"], "queued")

    def test_remote_reports_use_token_when_pause_reuses_the_attempt_number(self):
        self.register()
        old = self.claim()
        client = core.app.test_client()
        client.set_cookie("device", "a" * 32)

        def report(action, token):
            return client.post(f"/api/tasks/{old['id']}/{action}",
                               json={"worker": "worker", "claim_token": token, "result": {}},
                               headers={"X-Compute-Token": "lifecycle-test-token"})

        self.assertTrue(report("release", old["claim_token"]).get_json()["ok"])
        new = board.claim_task("worker", ["audit"])
        self.assertEqual(new["attempt"], old["attempt"])
        self.assertNotEqual(new["claim_token"], old["claim_token"])
        for action in ("heartbeat", "fail", "release"):
            response = report(action, old["claim_token"])
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.get_json()["ok"])
            self.assertEqual(self.row(old["id"])["state"], "running")
        self.assertEqual(report("done", old["claim_token"]).status_code, 409)
        self.assertTrue(report("heartbeat", new["claim_token"]).get_json()["ok"])
        self.assertTrue(report("done", new["claim_token"]).get_json()["ok"])

    def test_remote_reports_validate_tokens_and_keep_legacy_compatibility(self):
        self.register()
        task = self.claim()
        client = core.app.test_client()
        headers = {"X-Compute-Token": "lifecycle-test-token"}
        for action in ("heartbeat", "done", "fail", "release"):
            for invalid in ({"claim_token": {}}, {"claim_token": "wrong"}, {"attempt": True}):
                response = client.post(f"/api/tasks/{task['id']}/{action}",
                                       json={"worker": "worker", **invalid}, headers=headers)
                self.assertEqual(response.status_code, 400)
        response = client.post(f"/api/tasks/{task['id']}/done", json={"worker": "worker", "result": {}}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
