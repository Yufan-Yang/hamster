"""Real Chromium status UI checks at desktop/mobile sizes; TV requests are mocked."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class DeviceBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT)))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.play = sync_playwright().start()
        cls.browser = cls.play.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.play.stop(); cls.server.shutdown(); cls.server.server_close()

    def test_status_refresh_permissions_offline_and_mobile(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self.browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                data = {"name": "LeTV Y55C", "host": "192.168.3.214", "port": 5555,
                        "online": True, "adb": True, "admin": False, "checked_at": 100,
                        "packages": {}, "android": "6.0", "system": "8.0", "error": ""}
                page.route("**/api/tv/status*", lambda route: route.fulfill(json=data))
                page.goto(f"http://127.0.0.1:{self.server.server_port}/static/devices.html")
                page.wait_for_function("document.querySelector('#online').textContent === '已连接'")
                self.assertIn("Y55C", page.locator("#deviceName").inner_text())
                self.assertFalse(page.locator("#screenshot").is_visible())
                self.assertTrue(page.locator("#adminHint").is_visible())
                data.update(admin=True, online=False, adb=False, error="电视离线")
                page.click("#refresh")
                page.wait_for_function("document.querySelector('#online').textContent === '离线'")
                self.assertTrue(page.locator("#screenshot").is_disabled())
                self.assertIn("电视离线", page.locator("#message").inner_text())
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                data.update(online=True, adb=True, error="", ads={"items": [{"name": "商业推送", "blocked": False}], "can_restore": False, "can_manage": False, "reason": "电视未授予权限"})
                page.click("#refresh")
                page.wait_for_function("!document.querySelector('#adPermission').hidden")
                self.assertTrue(page.locator("#blockAds").is_disabled())
                self.assertIn("权限", page.locator("#adPermission").inner_text())
                data["ads"].update(can_manage=True, reason="")
                calls = []
                def ads_action(route):
                    action = route.request.post_data_json["action"]
                    calls.append(action)
                    data["ads"]["items"][0]["blocked"] = action == "block"
                    data["ads"]["can_restore"] = action == "block"
                    route.fulfill(json={"ok": True})
                page.route("**/api/tv/ads", ads_action)
                page.click("#refresh")
                page.wait_for_function("!document.querySelector('#blockAds').disabled")
                page.click("#blockAds")
                page.wait_for_function("document.querySelector('#adSummary').textContent.includes('1 / 1')")
                self.assertTrue(page.locator("#restoreAds").is_visible())
                page.click("#restoreAds")
                page.wait_for_function("document.querySelector('#adSummary').textContent.includes('0 / 1')")
                self.assertEqual(calls, ["block", "restore"])
                self.assertEqual(errors, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
