#!/usr/bin/env python3
"""Offline learning-plan checks against a fresh database and mocked model calls."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend  # noqa: E402
from shiguang import core, intelligence, web  # noqa: E402


class LearningPlanTests(unittest.TestCase):
    job = backend.BackendReliability.job

    def setUp(self):
        backend.BackendReliability.setUp(self)
        for name, value in (("_cache", intelligence.OrderedDict()), ("_pending", set())):
            patcher = patch.object(intelligence, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, goal="学习 Python", **kwargs):
        return self.client.post("/api/intelligence/learning-plan", json={"goal": goal}, **kwargs)

    def answer(self, jid):
        return {
            "overview": "先理解基础，再练习。",
            "steps": [{"job_id": jid, "title": "入门", "reason": "简介介绍 Python 基础。",
                       "exercise": "用自己的话解释变量。"}],
            "limitations": ["还缺少项目实践材料。"],
        }

    def test_input_and_empty_library_do_not_call_llm(self):
        with patch.object(intelligence.llm, "llm_json") as model:
            for body in (None, [], {"goal": 123}, {"goal": " "}, {"goal": "a"}, {"goal": "字" * 301}):
                response = self.client.post("/api/intelligence/learning-plan", json=body)
                self.assertEqual(response.status_code, 400)
            self.job(status="queued", title="Python")
            self.job(owner="another-account", title="Python")
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["steps"], [])
        self.assertEqual(response.get_json()["candidate_count"], 0)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        model.assert_not_called()

    def test_plan_uses_only_visible_candidates_and_real_source_titles(self):
        own = self.job(title="真实 Python 教程", analysis=json.dumps({"summary": "Python 基础", "tags": ["教程"]}))
        self.job(owner="another-account", title="其他账号秘密")
        self.job(title="隐藏秘密", analysis=json.dumps({"tags": ["hidden"]}))
        core.q("INSERT INTO privacy(owner,tags) VALUES (?,?)", (self.owner, '["hidden"]'))
        result = self.answer(own)
        result["steps"][0]["source_title"] = "模型虚构标题"
        result["steps"].append(dict(result["steps"][0]))
        with patch.object(intelligence.llm, "llm_json", return_value=result) as model:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["steps"][0]["source_title"], "真实 Python 教程")
        context = json.loads(model.call_args.args[1])
        self.assertEqual([item["job_id"] for item in context["candidate_videos"]], [own])
        self.assertNotIn("秘密", model.call_args.args[1])
        self.assertEqual(model.call_args.args[5], "learning-plan")

    def test_unknown_references_and_bad_structures_are_rejected_without_cache(self):
        own = self.job(title="Python")
        foreign = self.job(owner="another-account", title="Foreign")
        for result in (None, {"overview": "x", "steps": {}, "limitations": []},
                       self.answer(foreign), self.answer(True), self.answer(str(own))):
            with self.subTest(result=result), patch.object(intelligence.llm, "llm_json", return_value=result):
                response = self.post()
            self.assertEqual(response.status_code, 502)
            self.assertFalse(intelligence._cache)
            self.assertFalse(intelligence._pending)
        with patch.object(intelligence.llm, "llm_json", side_effect=RuntimeError("secret API detail")):
            response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret API detail", response.get_data(as_text=True))

    def test_cache_reuses_results_and_invalidates_changed_metadata(self):
        own = self.job(title="Python")
        with patch.object(intelligence.llm, "llm_json", return_value=self.answer(own)) as model:
            first = self.post().get_json()
            second = self.post().get_json()
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(model.call_count, 1)
            core.q("UPDATE jobs SET analysis=? WHERE id=?", ('{"summary":"新内容"}', own))
            third = self.post().get_json()
            self.assertFalse(third["cached"])
            self.assertEqual(model.call_count, 2)

    def test_cache_separates_accounts_and_locked_privacy_state(self):
        own = self.job(title="Python")
        hidden = self.job(title="Python hidden", analysis='{"tags":["hidden"]}')
        core.q("INSERT INTO privacy(owner,tags) VALUES (?,?)", (self.owner, '["hidden"]'))
        token = "learning-test"
        token_value = {"owner": self.owner, "expires": time.time() + 300, "reveal": True}
        with patch.dict(web.privacy_tokens, {token: token_value}), \
                patch.object(intelligence.llm, "llm_json", side_effect=[self.answer(hidden), self.answer(own)]) as model:
            unlocked = self.post(headers={"X-Privacy-Token": token}).get_json()
            locked = self.post().get_json()
            self.assertEqual(unlocked["steps"][0]["job_id"], hidden)
            self.assertEqual(locked["steps"][0]["job_id"], own)
            self.assertFalse(locked["cached"])
            self.assertEqual(model.call_count, 2)
        stranger = core.app.test_client()
        stranger.set_cookie(web.DEVICE_COOKIE, "b" * 32)
        with patch.object(intelligence.llm, "llm_json") as model:
            data = stranger.post("/api/intelligence/learning-plan", json={"goal": "学习 Python"}).get_json()
        self.assertEqual(data["steps"], [])
        model.assert_not_called()

    def test_rechecks_privacy_and_deletion_after_generation(self):
        own = self.job(title="Python")

        def delete_source(*args, **kwargs):
            core.q("DELETE FROM jobs WHERE id=?", (own,))
            return self.answer(own)

        with patch.object(intelligence.llm, "llm_json", side_effect=delete_source):
            self.assertEqual(self.post().status_code, 409)
        self.assertFalse(intelligence._cache)
        hidden = self.job(title="Python hidden", analysis='{"tags":["hidden"]}')
        core.q("INSERT INTO privacy(owner,tags) VALUES (?,?)", (self.owner, '["hidden"]'))
        token = "learning-expiry"
        token_value = {"owner": self.owner, "expires": time.time() + 300, "reveal": True}

        def expire_unlock(*args, **kwargs):
            web.privacy_tokens[token]["expires"] = 0
            return self.answer(hidden)

        with patch.dict(web.privacy_tokens, {token: token_value}), \
                patch.object(intelligence.llm, "llm_json", side_effect=expire_unlock):
            self.assertEqual(self.post(headers={"X-Privacy-Token": token}).status_code, 409)
        self.assertFalse(intelligence._cache)

    def test_duplicate_inflight_request_does_not_repeat_model_call(self):
        own = self.job(title="Python")
        other = core.app.test_client()
        other.set_cookie(web.DEVICE_COOKIE, self.owner)

        def nested_request(*args, **kwargs):
            response = other.post("/api/intelligence/learning-plan", json={"goal": "学习 Python"})
            self.assertEqual(response.status_code, 409)
            return self.answer(own)

        with patch.object(intelligence.llm, "llm_json", side_effect=nested_request) as model:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        model.assert_called_once()

    def test_owner_cannot_generate_different_goals_concurrently(self):
        own = self.job(title="Python")
        other = core.app.test_client()
        other.set_cookie(web.DEVICE_COOKIE, self.owner)

        def nested_request(*args, **kwargs):
            response = other.post("/api/intelligence/learning-plan", json={"goal": "学习其他知识"})
            self.assertEqual(response.status_code, 409)
            return self.answer(own)

        with patch.object(intelligence.llm, "llm_json", side_effect=nested_request) as model:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        model.assert_called_once()
        self.assertFalse(intelligence._pending)

    def test_different_owners_can_generate_concurrently(self):
        own = self.job(title="Python")
        other_owner = "b" * 32
        foreign = self.job(owner=other_owner, title="其他课程")
        other = core.app.test_client()
        other.set_cookie(web.DEVICE_COOKIE, other_owner)

        def nested_request(system, prompt, *args, **kwargs):
            if json.loads(prompt)["goal"] == "其他目标":
                return self.answer(foreign)
            # Concurrent requests have independent Flask app contexts (and therefore independent g).
            with core.app.app_context():
                response = other.post("/api/intelligence/learning-plan", json={"goal": "其他目标"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["steps"][0]["job_id"], foreign)
            return self.answer(own)

        with patch.object(intelligence.llm, "llm_json", side_effect=nested_request) as model:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["steps"][0]["job_id"], own)
        self.assertEqual(model.call_count, 2)
        self.assertFalse(intelligence._pending)

    def test_candidates_and_prompt_are_bounded_and_old_relevant_items_rank_first(self):
        old = self.job(title="Python 基础", analysis=json.dumps({"summary": "Python 基础"}))
        core.q("UPDATE jobs SET created=1 WHERE id=?", (old,))
        for _ in range(35):
            self.job(title="无关内容", analysis=json.dumps({
                "summary": "无关" * 1000, "key_points": ["内容" * 200] * 20, "tags": ["tag"] * 50,
            }))
        with patch.object(intelligence.llm, "llm_json", return_value=self.answer(old)) as model:
            response = self.post("Python")
        self.assertEqual(response.status_code, 200)
        candidates = json.loads(model.call_args.args[1])["candidate_videos"]
        self.assertEqual(candidates[0]["job_id"], old)
        self.assertLessEqual(len(candidates), intelligence.MAX_CANDIDATES)
        self.assertLessEqual(sum(len(json.dumps(item, ensure_ascii=False)) for item in candidates),
                             intelligence.MAX_CONTEXT_CHARS)

    def test_external_anonymous_and_stale_owner_are_rejected(self):
        with patch.object(intelligence.llm, "llm_json") as model:
            self.assertEqual(self.post(environ_overrides={"SERVER_PORT": str(web.EXTERNAL_PORT)}).status_code, 401)
            self.assertEqual(self.post(headers={"X-Storage-Owner": "other"}).status_code, 409)
        model.assert_not_called()

    def test_goal_without_search_terms_still_has_valid_sql(self):
        own = self.job(title="绘画")
        with patch.object(intelligence.llm, "llm_json", return_value=self.answer(own)):
            self.assertEqual(self.post("🎨🎨").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
