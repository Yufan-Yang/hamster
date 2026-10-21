#!/usr/bin/env python3
"""Multipart result uploads cannot replace media after losing their task claim."""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_reliability as backend  # noqa: E402
from shiguang import board, core, live  # noqa: E402


class LiveResultTests(unittest.TestCase):
    def setUp(self):
        backend.BackendReliability.setUp(self)
        for target, name, value in (
            (board, "COMPUTE_TOKEN", "live-result-test"),
            (live, "ROOT", Path(self.temp.name) / "live"),
            (board, "_seen_written", {}),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        core.q("INSERT INTO live_sources(id,owner,room_id,name,url) "
               "VALUES(1,?,1,'test','https://live.bilibili.com/1')", (self.owner,))
        core.q("INSERT INTO live_sessions(key,source_id,owner) VALUES('session',1,?)", (self.owner,))
        core.q("INSERT INTO live_clips(id,owner,source_id,session_key,lane,start,end,title,state,version) "
               "VALUES(1,?,1,'session','highlight',0,30,'test','rendering',1)", (self.owner,))
        tid = board.publish("live_render", "audit:render", payload={"clip": 1, "version": 1})
        self.task = board.claim_task("mac-test", ["live_render", "gpu"])
        self.assertEqual(self.task["id"], tid)
        self.destination = live.ROOT / "clips/1-v1.mp4"
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"previous accepted render")

    def upload(self, token, duration=30):
        query = {"worker": "mac-test"}
        if token is not None:
            query["claim_token"] = token
        with patch.object(live.library, "duration_of", side_effect=duration if callable(duration) else None,
                          return_value=duration) as probe:
            response = self.client.post(
                f"/api/tasks/{self.task['id']}/live-result", query_string=query,
                headers={"X-Compute-Token": "live-result-test"},
                data={"video": (io.BytesIO(b"new render"), "clip.mp4")},
            )
        self.assertFalse(list(self.destination.parent.glob("*.remote.partial.mp4")))
        return response, probe

    def test_current_claim_accepts_render(self):
        response, _ = self.upload(self.task["claim_token"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.destination.read_bytes(), b"new render")

    def test_old_token_is_rejected_before_upload_validation(self):
        response, probe = self.upload("f" * 32)
        self.assertEqual(response.status_code, 409)
        probe.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"previous accepted render")

    def test_claim_changed_during_probe_cannot_replace_existing_render(self):
        def reclaim(_path):
            board.release_task(self.task["id"], "mac-test", claim_token=self.task["claim_token"])
            replacement = board.claim_task("mac-test", ["live_render", "gpu"])
            self.assertEqual(replacement["attempt"], self.task["attempt"])
            self.assertNotEqual(replacement["claim_token"], self.task["claim_token"])
            return 30

        response, _ = self.upload(self.task["claim_token"], duration=reclaim)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.destination.read_bytes(), b"previous accepted render")

    def test_clip_changed_during_probe_cannot_replace_existing_render(self):
        def changed(_path):
            core.q("UPDATE live_clips SET version=2 WHERE id=1")
            return 30

        response, _ = self.upload(self.task["claim_token"], duration=changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.destination.read_bytes(), b"previous accepted render")

    def test_missing_token_remains_compatible_but_duration_is_checked(self):
        response, _ = self.upload(None, duration=90)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.destination.read_bytes(), b"previous accepted render")
        response, _ = self.upload(None)
        self.assertEqual(response.status_code, 200)

    def test_expired_lease_and_invalid_token_do_not_replace_render(self):
        response, _ = self.upload("invalid")
        self.assertEqual(response.status_code, 400)
        core.q("UPDATE tasks SET lease_until=0 WHERE id=?", (self.task["id"],))
        response, _ = self.upload(self.task["claim_token"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.destination.read_bytes(), b"previous accepted render")


if __name__ == "__main__":
    unittest.main()
