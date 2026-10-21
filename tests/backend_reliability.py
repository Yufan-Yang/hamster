#!/usr/bin/env python3
"""Standalone regressions using a fresh database, with no production data or network.

Run with the app's dependencies: python tests/backend_reliability.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shiguang import board, core, heal, web  # noqa: E402


class BackendReliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grabber-backend-test-")
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        for target, name, value in (
            (core, "STATE", directory), (core, "DB_PATH", directory / "test.db"),
            (core, "DB", None), (web, "MEDIA", directory), (web, "_seen_cache", {}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        config = patch.dict(core.app.config, TESTING=True, SECRET_KEY="backend-test")
        config.start()
        self.addCleanup(config.stop)
        core.init_db()
        self.addCleanup(core.DB.close)
        self.owner = "a" * 32
        self.client = core.app.test_client()
        self.client.set_cookie(web.DEVICE_COOKIE, self.owner)

    def job(self, owner=None, status="done", ref=None, title="测试视频", **fields):
        values = dict(owner=owner or self.owner, status=status, ref=ref, title=title,
                      url="https://example.test/video", created=time.time(), updated=time.time(), **fields)
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        core.q(f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(values.values()))
        return core.q("SELECT MAX(id) id FROM jobs", one=True)["id"]

    def test_first_page_load_reads_archive_even_with_epoch_timestamp(self):
        folder = Path(self.temp.name)
        page = folder / "index.html"
        page.write_text("<!doctype html><html>first request</html>")
        os.utime(page, (0, 0))
        with patch.object(web, "HERE", folder), patch.object(web, "_page", [0, b""]):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"first request", response.data)

    def task(self, kind="live_render", attempts=1):
        core.q("INSERT INTO tasks(kind,target,state,worker,attempts,lease_until,payload) "
               "VALUES (?,?,'running','old',?,?,?)",
               (kind, f"test:{time.time_ns()}", attempts, time.time() + 300,
                json.dumps({"clip": 1, "version": 1, "reference": 1})))
        return core.q("SELECT MAX(id) id FROM tasks", one=True)["id"]

    def test_stale_failure_cannot_overwrite_reclaimed_or_completed_task(self):
        for retry in (True, False):
            for worker, state, attempts in (("new", "running", 2), ("old", "running", 2),
                                             ("old", "done", 1), (None, "queued", 1)):
                with self.subTest(retry=retry, worker=worker, state=state):
                    tid = self.task()
                    after_reclaim = {}

                    def reclaim_then_write(sql, args=()):
                        core.q("UPDATE tasks SET worker=?,state=?,attempts=?,result='new result' WHERE id=?",
                               (worker, state, attempts, tid))
                        after_reclaim.update(core.q("SELECT * FROM tasks WHERE id=?", (tid,), one=True))
                        return core._write(sql, args)

                    with patch.object(board, "_write", side_effect=reclaim_then_write), \
                            patch.object(heal, "task_failed") as report:
                        self.assertFalse(board.fail_task(tid, "old", "late failure", retry=retry))
                    self.assertEqual(dict(core.q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)),
                                     after_reclaim)
                    report.assert_not_called()

    def test_stale_terminal_failure_does_not_fail_live_media(self):
        core.q("INSERT INTO live_clips(id,owner,source_id,session_key,lane,start,end,title,state) "
               "VALUES (1,?,1,'test','topic',0,30,'片段','rendering')", (self.owner,))
        core.q("INSERT INTO live_references(id,owner,source_id,bvid,state) "
               "VALUES (1,?,1,'test','running')", (self.owner,))
        for kind in ("live_render", "live_reference", "live_transcribe"):
            with self.subTest(kind=kind):
                tid = self.task(kind)

                def reclaim_then_write(sql, args=()):
                    core.q("UPDATE tasks SET worker='new',attempts=2 WHERE id=?", (tid,))
                    return core._write(sql, args)

                with patch.object(board, "_write", side_effect=reclaim_then_write), \
                        patch.object(heal, "task_failed") as report:
                    self.assertFalse(board.fail_task(tid, "old", "late failure", retry=False))
                report.assert_not_called()
        self.assertEqual(core.q("SELECT state FROM live_clips WHERE id=1", one=True)["state"], "rendering")
        self.assertEqual(core.q("SELECT state FROM live_references WHERE id=1", one=True)["state"], "running")

    def test_current_failure_retries_with_existing_backoff(self):
        tid = self.task(attempts=2)
        before = time.time()
        with patch.object(heal, "task_failed") as report:
            self.assertTrue(board.fail_task(tid, "old", "temporary"))
        row = core.q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        self.assertEqual((row["state"], row["worker"], row["lease_until"]), ("queued", None, None))
        self.assertEqual(row["error"], "temporary")
        self.assertGreaterEqual(row["not_before"], before + 240)
        self.assertLessEqual(row["not_before"], time.time() + 240)
        report.assert_not_called()

    def test_current_terminal_failure_updates_media_and_reports(self):
        core.q("INSERT INTO live_clips(id,owner,source_id,session_key,lane,start,end,title,state) "
               "VALUES (1,?,1,'test','topic',0,30,'片段','rendering')", (self.owner,))
        tid = self.task(attempts=5)
        with patch.object(heal, "task_failed") as report:
            self.assertTrue(board.fail_task(tid, "old", "failed", trace="trace", expected=True))
        self.assertEqual(core.q("SELECT state FROM tasks WHERE id=?", (tid,), one=True)["state"], "failed")
        self.assertEqual(core.q("SELECT state FROM live_clips WHERE id=1", one=True)["state"], "failed")
        report.assert_called_once_with(tid, "failed", "trace", True)

    def test_subtitle_search_only_reads_visible_sources_including_shared_downloads(self):
        own = self.job()
        shared = self.job(owner="another-account")
        alias = self.job(ref=shared)
        foreign = self.job(owner="another-account")
        cancelled = self.job(status="cancelled")
        for jid in (own, shared, foreign, cancelled):
            for timestamp in (20, 10):
                core.q("INSERT INTO seg(kind,ref,part,t,src,text) VALUES ('job',?,0,?,'字幕','共同词语')",
                       (jid, timestamp))
        fetched = []

        def observed_query(sql, args=(), one=False):
            rows = core.q(sql, args, one)
            if "FROM seg " in sql:
                fetched.extend(rows)
            return rows

        with patch.object(web, "q", side_effect=observed_query):
            response = self.client.get("/api/jobs", query_string={"q": "共同词", "compact": "1"})
        self.assertEqual(response.status_code, 200)
        jobs = response.get_json()["jobs"]
        self.assertEqual({j["id"] for j in jobs}, {own, alias})
        self.assertEqual({r["ref"] for r in fetched}, {own, shared})
        self.assertEqual(len(fetched), 4)
        self.assertTrue(all([h["t"] for h in j["hits"]] == [10, 20] for j in jobs))
        restricted = self.client.get("/api/jobs", query_string={
            "q": "共同词", "compact": "1", "only": "1", "ids": str(alias),
        }).get_json()
        self.assertEqual([j["id"] for j in restricted["jobs"]], [alias])

    def test_search_without_visible_sources_does_not_query_subtitle_index(self):
        foreign = self.job(owner="another-account")
        core.q("INSERT INTO seg(kind,ref,text) VALUES ('job',?,'共同词语')", (foreign,))
        with patch.object(web, "q", wraps=core.q) as query:
            response = self.client.get("/api/jobs", query_string={"q": "共同词", "compact": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["jobs"], [])
        self.assertFalse(any("FROM seg " in call.args[0] for call in query.call_args_list))

    def test_linked_progress_is_fetched_once_for_all_cards(self):
        source = self.job(owner="another-account", status="downloading", title="共享下载",
                          stage="下载中", progress=42, speed="1 MB/s")
        aliases = [self.job(status="linked", ref=source, title="") for _ in range(20)]
        missing = self.job(status="linked", ref=99999, title="原文件已删除")
        with patch.object(web, "q", wraps=core.q) as query:
            response = self.client.get("/api/jobs", query_string={"compact": "1"})
        self.assertEqual(response.status_code, 200)
        jobs = response.get_json()["jobs"]
        self.assertEqual([j["id"] for j in jobs], sorted([*aliases, missing], reverse=True))
        for job in jobs:
            if job["id"] in aliases:
                self.assertEqual((job["status"], job["title"], job["progress"], job["stage"], job["speed"]),
                                 ("downloading", "共享下载", 42, "下载中", "1 MB/s"))
            else:
                self.assertEqual((job["status"], job["title"]), ("linked", "原文件已删除"))
        source_queries = [call for call in query.call_args_list
                          if "status, stage, progress, speed, title, thumb FROM jobs" in call.args[0]]
        self.assertEqual(len(source_queries), 1)


if __name__ == "__main__":
    unittest.main()
