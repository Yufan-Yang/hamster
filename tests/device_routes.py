"""TV API authorization: local read-only status; local admin-only screen/actions."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="shiguang-tv-routes-")
os.environ.update(DB_PATH=TEMP.name + "/test.db", STATE_DIR=TEMP.name, MEDIA_ROOT=TEMP.name)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shiguang import core, web  # noqa: E402
from shiguang import television  # noqa: E402
from shiguang import cast  # noqa: E402


class DeviceRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.init_db()
        core.app.config.update(TESTING=True, SECRET_KEY="tv-test")

    @classmethod
    def tearDownClass(cls):
        core.DB.close()
        TEMP.cleanup()

    def setUp(self):
        self.client = core.app.test_client()
        self.owner = "d" * 32
        self.client.set_cookie(web.DEVICE_COOKIE, self.owner)

    def admin(self):
        core.q("INSERT OR REPLACE INTO users(name,pw,admin,created) VALUES ('tv-admin','unused',1,0)")
        core.q("INSERT OR REPLACE INTO devices(id,ip,seen,user) VALUES (?,'127.0.0.1',0,'tv-admin')", (self.owner,))
        with self.client.session_transaction() as session:
            session["user"] = "tv-admin"

    def test_status_and_page_are_local_and_do_not_mutate_tv(self):
        with patch.object(television, "status", return_value={"online": False}) as status:
            response = self.client.get("/api/tv/status")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["admin"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            status.assert_called_once_with(force=False)
        response = self.client.get("/devices")
        self.assertEqual(response.status_code, 200)
        response.close()
        with patch.object(television, "status") as status:
            response = self.client.get("/api/tv/status", environ_overrides={"SERVER_PORT": "8090"})
            self.assertIn(response.status_code, (401, 403))
            status.assert_not_called()

    def test_screenshots_require_admin_and_are_never_cached(self):
        with patch.object(television, "screenshot", return_value=b"\x89PNG\r\n\x1a\n") as capture:
            self.assertEqual(self.client.get("/api/tv/screenshot").status_code, 403)
            capture.assert_not_called()
            self.admin()
            response = self.client.get("/api/tv/screenshot")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            capture.assert_called_once()

    def test_screenshot_failure_is_actionable(self):
        self.admin()
        with patch.object(television, "screenshot", side_effect=television.TVError("电视离线")):
            response = self.client.get("/api/tv/screenshot")
        self.assertEqual(response.status_code, 502)
        self.assertIn("离线", response.json["error"])

    def test_receiver_recovery_is_local_admin_only(self):
        with patch.object(television, "restore_receiver") as restore, patch.object(cast, "renderer_at", return_value={"name": "LeTV"}):
            self.assertEqual(self.client.post("/api/tv/receiver/start").status_code, 403)
            restore.assert_not_called()
            self.admin()
            self.assertEqual(self.client.post("/api/tv/receiver/start").json["name"], "LeTV")
            restore.assert_called_once()
            response = self.client.post("/api/tv/receiver/start", headers={"Origin": "http://untrusted.example"})
            self.assertEqual(response.status_code, 403)

    def test_ad_actions_are_fixed_and_admin_only(self):
        with patch.object(television, "change_ads", return_value={"ads": {}, "changed": []}) as change:
            self.assertEqual(self.client.post("/api/tv/ads", json={"action": "block"}).status_code, 403)
            change.assert_not_called()
            self.admin()
            self.assertEqual(self.client.post("/api/tv/ads", json={"action": "shell"}).status_code, 400)
            self.assertEqual(self.client.post("/api/tv/ads", json=["block"]).status_code, 400)
            change.assert_not_called()
            response = self.client.post("/api/tv/ads", json={"action": "block"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(change.call_args.args[0], "block")
            self.assertEqual(self.client.post("/api/tv/ads", json={"action": "restore"}, headers={"Origin": "http://untrusted.example"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
