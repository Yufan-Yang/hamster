#!/usr/bin/env python3
"""Grounded copy suggestions, using a disposable database and a deterministic model."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shiguang import core, live_assistant, web  # noqa: E402


class LiveAssistant(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shiguang-assistant-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for target, name, value in (
            (core, "DB", None), (core, "DB_PATH", root / "db"), (core, "STATE", root),
            (web, "_seen_cache", {}),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(core.app.config, TESTING=True, SECRET_KEY="assistant-test")
        p.start()
        self.addCleanup(p.stop)
        core.init_db()
        self.addCleanup(core.DB.close)
        live_assistant._cache.clear()
        live_assistant._busy.clear()
        self.client = core.app.test_client()
        self.client.set_cookie(web.DEVICE_COOKIE, "c" * 32)
        core.q("INSERT INTO users(name,pw) VALUES ('writer','')")
        core.q("INSERT INTO devices(id,user) VALUES (?,'writer')", ("c" * 32,))
        with self.client.session_transaction() as session:
            session["user"] = "writer"
        core.q("INSERT INTO live_sources(id,owner,room_id,name,url) VALUES (1,'user:writer',123,'新别名','https://live.bilibili.com/123')")
        core.q("INSERT INTO live_sessions(key,source_id,owner,origin) VALUES ('one',1,'user:writer',?)",
               (json.dumps({"name": "原主播"}),))
        core.q("INSERT INTO live_recordings(id,source_id,session_key,owner,seq,path,offset,duration,state,segments) "
               "VALUES (1,1,'one','user:writer',0,'/unused',0,60,'ready',?)",
               (json.dumps([[0, 10, "开店前先计算租金。"], [10, 30, "再估算每天需要多少客户。"],
                            [50, 60, "不在切片范围内。"]]),))
        self.body = {"session": "one", "start": 0, "end": 30, "focus": "突出实用建议"}
        self.answer = {"options": [{"title": "开店前先算清两笔账", "cover_title": "你的店需要多少客户？",
                                   "description": "主播讨论租金和客流估算。",
                                   "tags": ["创业", "开店", "经营"], "reason": "聚焦实际决策", "evidence": [1, 2]}],
                       "limitation": ""}

    def post(self, body=None):
        return self.client.post("/api/live/assist", json=self.body if body is None else body)

    def test_grounded_options_do_not_modify_clips_and_cache_identical_input(self):
        with patch.object(live_assistant.llm, "llm_json", return_value=self.answer) as model:
            first = self.post()
            self.assertEqual(first.status_code, 200)
            second = self.post()
            self.assertTrue(second.json["cached"])
            self.assertEqual(model.call_count, 1)
            payload = json.loads(model.call_args.args[1])
            self.assertEqual(payload["source"], "原主播")
            self.assertEqual([e["text"] for e in payload["excerpts"]], ["开店前先计算租金。", "再估算每天需要多少客户。"])
            self.assertFalse(first.json["cached"])
            self.assertEqual(first.json["options"][0]["evidence"], [1, 2])
            self.assertEqual(core.q("SELECT COUNT(*) n FROM live_clips", one=True)["n"], 0)
            self.assertEqual(core.q("SELECT COUNT(*) n FROM tasks", one=True)["n"], 0)
            core.q("UPDATE live_recordings SET segments=?", (json.dumps([[0, 10, "转写已更正"]]),))
            changed = {**self.answer, "options": [{**self.answer["options"][0], "evidence": [1]}]}
            model.return_value = changed
            self.assertFalse(self.post().json["cached"])
            self.assertEqual(model.call_count, 2)

    def test_unauthorized_and_invalid_input_never_calls_model(self):
        with patch.object(live_assistant.llm, "llm_json") as model:
            stranger = core.app.test_client()
            self.assertEqual(stranger.post("/api/live/assist", json=self.body).status_code, 401)
            core.q("INSERT INTO live_sessions(key,source_id,owner) VALUES ('foreign',1,'user:another')")
            self.assertEqual(self.post({**self.body, "session": "foreign"}).status_code, 404)
            for body in ([], {**self.body, "start": True}, {**self.body, "end": "NaN"},
                         {**self.body, "end": 2}, {**self.body, "focus": "长" * 501},
                         {"session": "one", "end": 30}):
                self.assertEqual(self.post(body).status_code, 400, body)
            model.assert_not_called()

    def test_missing_transcript_skips_generation(self):
        core.q("UPDATE live_recordings SET state='transcribing'")
        with patch.object(live_assistant.llm, "llm_json") as model:
            response = self.post()
        self.assertEqual(response.json["options"], [])
        self.assertIn("转写", response.json["limitation"])
        model.assert_not_called()

    def test_unknown_citations_and_malformed_outputs_are_not_cached(self):
        with patch.object(live_assistant.llm, "llm_json") as model:
            for answer in (None, {"options": "bad"},
                           {**self.answer, "options": [{**self.answer["options"][0], "evidence": [999]}]},
                           {**self.answer, "options": [{**self.answer["options"][0], "evidence": [True]}]}):
                model.return_value = answer
                self.assertEqual(self.post().status_code, 502)
                self.assertFalse(live_assistant._cache)
            model.return_value = self.answer
            self.assertEqual(self.post().status_code, 200)
        self.assertFalse(live_assistant._busy)

    def test_inflight_requests_are_bounded_per_account(self):
        nested = []

        def generate(*args, **kwargs):
            nested.append(self.post({**self.body, "focus": "换个角度"}).status_code)
            return self.answer

        with patch.object(live_assistant.llm, "llm_json", side_effect=generate) as model:
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(nested, [409])
            self.assertEqual(model.call_count, 1)

    def test_long_transcripts_are_bounded_and_keep_beginning_and_end(self):
        core.q("UPDATE live_recordings SET segments=?", (json.dumps([[i / 10, (i + 1) / 10, f'第{i}句']
                                                                   for i in range(300)]),))
        evidence, sampled = live_assistant.excerpts("user:writer", "one", 0, 30)
        self.assertTrue(sampled)
        self.assertEqual(len(evidence), 60)
        self.assertEqual((evidence[0]["text"], evidence[-1]["text"]), ("第0句", "第299句"))

    def test_transcript_changes_during_generation_reject_stale_suggestions(self):
        def generate(*args, **kwargs):
            core.q("UPDATE live_recordings SET segments=?", (json.dumps([[0, 10, "已更正的字幕"]]),))
            return self.answer

        with patch.object(live_assistant.llm, "llm_json", side_effect=generate):
            response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertIn("字幕已更新", response.json["error"])
        self.assertFalse(live_assistant._cache)
        self.assertFalse(live_assistant._busy)


if __name__ == "__main__":
    unittest.main()
