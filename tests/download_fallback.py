#!/usr/bin/env python3
"""Isolated download regressions: python tests/download_fallback.py [--local-http].

The optional integration tests generate tiny synthetic videos and serve only localhost.
No external sites, credentials, production database or media are used.
"""
import errno
import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shiguang import download, pipeline  # noqa: E402

LOCAL_HTTP = "--local-http" in sys.argv
if LOCAL_HTTP:
    sys.argv.remove("--local-http")


class DownloadFallback(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grabber-download-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.work = self.directory / "work"
        self.work.mkdir()
        self.cookiefile = self.directory / "cookies.txt"
        self.cookiefile.write_text("# Netscape HTTP Cookie File\n")
        cookie_patch = patch.object(download, "COOKIES", self.directory / "unused-cookies.txt")
        cookie_patch.start()
        self.addCleanup(cookie_patch.stop)
        for module in (download, pipeline):
            for name in ("check_cancel", "update"):
                mocked = patch.object(module, name)
                mocked.start()
                self.addCleanup(mocked.stop)
        self.found = {"title": "Public test", "media_urls": ["https://media.test/main.mp4"],
                      "cookiefile": str(self.cookiefile)}

    def stream_ok(self, job_id, url, directory, *args):
        (directory / "video.mkv").write_bytes(b"verified video")
        return {"source": "sniffed-ffmpeg", "duration": 1}

    def test_primary_success_keeps_resume_and_never_sniffs(self):
        (self.work / "old.mp4.part").write_bytes(b"partial")

        def primary(job, url, directory, opts):
            self.assertTrue((directory / "old.mp4.part").exists())
            (directory / "good.mp4").write_bytes(b"video")
            return {"title": "Original"}

        with patch.object(download, "ytdlp_download", side_effect=primary), patch.object(download, "sniff_page") as sniff:
            meta, directory = download.page_video_download(1, "https://page.test/v", self.work, {"title": "Original"})
        self.assertEqual(meta["source"], "ytdlp")
        self.assertEqual(directory, self.work / ".ytdlp")
        sniff.assert_not_called()

    def test_primary_failure_uses_isolated_alternatives_and_stops_after_success(self):
        self.found["media_urls"] = [f"https://media.test/{name}.mp4" for name in ("expired", "good", "other-quality")]

        def primary(job, url, directory, opts):
            (directory / "failed-primary.mp4").write_bytes(b"broken")
            raise RuntimeError("Unable to download: HTTP Error 403")

        def stream(job, url, directory, *args):
            if "expired" in url:
                (directory / "failed-candidate.mp4").write_bytes(b"broken")
                raise RuntimeError("HTTP Error 403: expired")
            return self.stream_ok(job, url, directory, *args)

        with patch.object(download, "ytdlp_download", side_effect=primary), \
             patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "ffmpeg_download", side_effect=stream) as ffmpeg:
            meta, directory = download.page_video_download(1, "https://page.test/v", self.work, {"title": "Original"})
        self.assertEqual(ffmpeg.call_count, 2)
        self.assertEqual(meta["source"], "sniffed-ffmpeg")
        self.assertEqual([p.name for p in directory.rglob("*") if p.is_file()], ["Public test 01.mkv"])
        self.assertTrue((self.work / ".ytdlp" / "failed-primary.mp4").exists())
        self.assertFalse(self.cookiefile.exists())

    def test_explicit_independent_players_are_preserved(self):
        self.found["media_groups"] = [["https://media.test/one.mp4"], ["https://media.test/two.mp4"]]
        with patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "ffmpeg_download", side_effect=self.stream_ok) as ffmpeg:
            _, directory = download.page_video_download(1, "https://page.test/v", self.work)
        self.assertEqual(ffmpeg.call_count, 2)
        self.assertEqual(len(list(directory.rglob("*.mkv"))), 2)

    def test_terminal_errors_do_not_sniff_or_retry(self):
        for error in (download.Cancelled(), OSError(errno.ENOSPC, "full"),
                      PermissionError(errno.EACCES, "denied"), RuntimeError("DRM protected"),
                      RuntimeError("Login required"), RuntimeError("Private video")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(download, "ytdlp_download", side_effect=error), \
                 patch.object(download, "sniff_page") as sniff:
                with self.assertRaises((download.Cancelled, download.DownloadFailure)):
                    download.page_video_download(1, "https://page.test/v", self.work, {"title": "Original"})
                sniff.assert_not_called()
                self.assertFalse(download.can_fallback(error))

    def test_cancellation_between_primary_failure_and_fallback_stops(self):
        with patch.object(download, "ytdlp_download", side_effect=RuntimeError("HTTP Error 403")), \
             patch.object(download, "check_cancel", side_effect=[None, download.Cancelled()]), \
             patch.object(download, "sniff_page") as sniff:
            with self.assertRaises(download.Cancelled):
                download.page_video_download(1, "https://page.test/v", self.work, {"title": "Original"})
        sniff.assert_not_called()

    def test_cancellation_during_candidate_cleans_up_without_trying_another(self):
        self.found["media_urls"] *= 2
        with patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "ffmpeg_download", side_effect=download.Cancelled()) as ffmpeg:
            with self.assertRaises(download.Cancelled):
                download.page_video_download(1, "https://page.test/v", self.work)
        self.assertEqual(ffmpeg.call_count, 1)
        self.assertFalse((self.work / ".fallback").exists())
        self.assertFalse(self.cookiefile.exists())

    def test_candidate_limit_and_failure_summary_redacts_signed_urls(self):
        self.found["media_urls"] = [f"https://media.test/{i}.mp4?secret=token" for i in range(10)]
        with patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "ffmpeg_download", side_effect=RuntimeError("HTTP Error 403 https://media.test/a?secret=token")) as ffmpeg:
            with self.assertRaises(download.DownloadFailure) as caught:
                download.page_video_download(1, "https://page.test/v", self.work)
        self.assertEqual(ffmpeg.call_count, 3)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("403", str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        self.assertFalse((self.work / ".fallback").exists())

    def test_cookie_domain_path_expiry_and_secure_matching(self):
        cookies = [
            {"domain": ".media.test", "name": "domain", "path": "/", "value": "ok"},
            {"domain": "cdn.media.test", "name": "host", "path": "/video", "value": "ok"},
            {"domain": "page.test", "name": "private", "path": "/", "value": "secret"},
            {"domain": ".media.test", "name": "wrong_path", "path": "/other", "value": "no"},
            {"domain": ".media.test", "name": "secure", "path": "/", "value": "ok", "secure": True},
            {"domain": ".media.test", "name": "expired", "path": "/", "value": "no", "expires": 1},
        ]
        download.write_cookie_file(self.cookiefile, [{"secure": False, **c} for c in cookies])
        text = download.ffmpeg_cookies(self.cookiefile, "https://cdn.media.test/video/main.mp4")
        for name in ("domain=ok", "host=ok", "secure=ok"):
            self.assertIn(name, text)
        for name in ("private", "wrong_path", "expired"):
            self.assertNotIn(name, text)
        self.assertNotIn("secure=", download.ffmpeg_cookies(self.cookiefile, "http://cdn.media.test/video/main.mp4"))
        self.assertEqual(download.ffmpeg_cookies(self.cookiefile, "https://evilmedia.test/video/main.mp4"), "")
        self.assertNotIn("host=", download.ffmpeg_cookies(self.cookiefile, "https://sub.cdn.media.test/video/main.mp4"))
        self.assertEqual(self.cookiefile.stat().st_mode & 0o777, 0o600)

    def test_dedupe_removes_ads_segments_and_repeated_quality_urls(self):
        urls = ["https://cdn.test/main.mp4?token=1", "https://cdn.test/main.mp4?token=2",
                "https://ads.doubleclick.net/video.mp4", "https://cdn.test/ads/main.mp4",
                "https://cdn.test/segment001.mp4", "https://cdn.test/init.mp4",
                "https://cdn.test/chunk-2.m4s", "https://cdn.test/frag.ts", "file:///tmp/main.mp4"]
        self.assertEqual(download.dedupe_media(urls), urls[:1])

    def test_manifest_drm_is_rejected_before_ffmpeg(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b'<MPD><ContentProtection schemeIdUri="widevine"/></MPD>']
        with patch.object(download.requests, "get", return_value=response), \
             patch.object(download.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(download.DownloadFailure, "DRM"):
                download.ffmpeg_download(1, "https://media.test/video.mpd", self.work, "https://page.test", self.cookiefile)
        popen.assert_not_called()

    def test_missing_dash_demuxer_has_clear_error_and_can_use_ytdlp_stream_reader(self):
        with patch.object(download, "check_manifest"), \
             patch.object(download, "ffmpeg_demuxers", return_value=frozenset({"hls", "mov"})), \
             patch.object(download.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(download.UnsupportedStream, "DASH"):
                download.ffmpeg_download(1, "https://media.test/main.mpd", self.work, "https://page.test")
            popen.assert_not_called()
        self.found["media_urls"] = ["https://media.test/main.mpd"]

        def primary(job, url, directory, opts):
            (directory / "complete.mp4").write_bytes(b"video")
            return {"duration": 1}

        with patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "check_manifest"), \
             patch.object(download, "ffmpeg_download", side_effect=download.UnsupportedStream("DASH unavailable")), \
             patch.object(download, "ytdlp_download", side_effect=primary) as ytdlp:
            meta, _ = download.page_video_download(1, "https://page.test/v", self.work)
        self.assertEqual(meta["source"], "sniffed-parallel")
        self.assertEqual(ytdlp.call_count, 1)

    def test_parallel_stream_failure_uses_clean_independent_fallback(self):
        def broken(job, url, directory, opts):
            (directory / "partial.mp4").write_bytes(b"broken")
            raise RuntimeError("unsupported stream layout")
        with patch.object(download, "check_manifest"), \
             patch.object(download, "ytdlp_download", side_effect=broken), \
             patch.object(download, "ffmpeg_download", side_effect=self.stream_ok) as ffmpeg:
            meta = download.fast_stream_download(1, "https://media.test/v.m3u8", self.work, "https://page.test")
        self.assertEqual(meta["source"], "sniffed-ffmpeg")
        ffmpeg.assert_called_once()
        self.assertFalse((self.work / ".fragments").exists())
        self.assertEqual([p.name for p in self.work.iterdir()], ["video.mkv"])

    def test_parallel_cancellation_does_not_restart_in_independent_downloader(self):
        with patch.object(download, "check_manifest"), \
             patch.object(download, "ytdlp_download", side_effect=download.Cancelled()), \
             patch.object(download, "ffmpeg_download") as ffmpeg:
            with self.assertRaises(download.Cancelled):
                download.fast_stream_download(1, "https://media.test/v.m3u8", self.work, "https://page.test")
        ffmpeg.assert_not_called()

    def test_fragment_progress_reports_percentage_speed_and_backup_stage(self):
        opts = download.ytdlp_opts(1, self.work, {"_stage": "并发下载备用视频流（8 路）"})
        opts["progress_hooks"][0]({"status":"downloading", "fragment_index":2,
                                   "fragment_count":8, "downloaded_bytes":1024, "speed":2048})
        self.assertEqual(download.update.call_args.kwargs["progress"], 25)
        self.assertIn("/s", download.update.call_args.kwargs["speed"])
        self.assertIn("8 路", download.update.call_args.kwargs["stage"])

    def test_ffmpeg_is_terminated_on_cancel_and_partial_output_removed(self):
        proc = Mock()
        proc.poll.side_effect = [None, None]
        with patch.object(download.subprocess, "Popen", return_value=proc), \
             patch.object(download, "check_cancel", side_effect=[None, None, download.Cancelled()]):
            with self.assertRaises(download.Cancelled):
                download.ffmpeg_download(1, "https://media.test/main.mp4", self.work, "https://page.test")
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        self.assertFalse((self.work / "video.part.mkv").exists())

    def test_probe_does_not_hide_login_failure(self):
        with patch("yt_dlp.YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("Login required")
            with self.assertRaisesRegex(download.DownloadFailure, "登录"):
                download.ytdlp_probe("https://page.test/private")

    def test_job_does_not_auto_retry_storage_or_explicit_terminal_failure(self):
        for error in (OSError(errno.ENOSPC, "full"), download.DownloadFailure("需要人工处理")):
            with self.subTest(error=str(error)), patch.object(pipeline, "process", side_effect=error), \
                 patch.object(pipeline, "q", return_value={"attempts": 0}), \
                 patch.object(pipeline, "finish_links"), patch.object(pipeline, "ring"), \
                 patch.object(pipeline, "LLM_API_KEY", ""), patch.object(pipeline.traceback, "print_exc"):
                pipeline.update.reset_mock()
                pipeline.run_job(1)
                self.assertIsNone(pipeline.update.call_args.kwargs["retry_at"])


@unittest.skipUnless(LOCAL_HTTP and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "pass --local-http with ffmpeg/ffprobe to test synthetic media")
class LocalHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="grabber-http-media-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.requests = []
        cls.headers_ok = True
        cls.header_failures = []
        cls.inflight, cls.peak = 0, 0
        cls.fragment_lock = threading.Lock()
        commands = [
            ["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "1.2", "-c:v", "libx264", "-g", "4", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-movflags", "+faststart", str(cls.directory / "sample.mp4")],
            ["-i", str(cls.directory / "sample.mp4"), "-c", "copy", "-hls_time", "0.4",
             "-hls_list_size", "0", str(cls.directory / "sample.m3u8")],
            ["-i", str(cls.directory / "sample.mp4"), "-c", "copy", "-seg_duration", "0.4",
             "-f", "dash", str(cls.directory / "sample.mpd")],
        ]
        for command in commands:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *command],
                           check=True, capture_output=True, timeout=30)
        (cls.directory / "bad.mp4").write_text("not video")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                if (self.headers.get("Referer") != cls.base + "/page"
                        or self.headers.get("User-Agent") != download.UA
                        or "fixture=ok" not in (self.headers.get("Cookie") or "")
                        or "private=" in (self.headers.get("Cookie") or "")):
                    cls.headers_ok = False
                    cls.header_failures.append({name: self.headers.get(name) for name in ("Referer", "User-Agent", "Cookie")})
                    self.send_error(403)
                    return
                is_fragment = self.path.endswith((".ts", ".m4s"))
                if is_fragment:
                    with cls.fragment_lock:
                        cls.inflight += 1
                        cls.peak = max(cls.peak, cls.inflight)
                    time.sleep(.12)  # Model network round-trip latency, not a real CDN.
                try:
                    return super().do_GET()
                finally:
                    if is_fragment:
                        with cls.fragment_lock:
                            cls.inflight -= 1

            def log_message(self, *_args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                      functools.partial(Handler, directory=str(cls.directory)))
        cls.base = "http://127.0.0.1:" + str(cls.server.server_address[1])
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.cookiefile = cls.directory / "cookies.txt"
        download.write_cookie_file(cls.cookiefile, [
            {"domain": "127.0.0.1", "path": "/", "secure": False, "name": "fixture", "value": "ok"},
            {"domain": "page.test", "path": "/", "secure": False, "name": "private", "value": "never-send"},
        ])

    def check_downloads(self, extensions):
        for extension in extensions:
            with self.subTest(extension=extension), patch.object(download, "check_cancel"), patch.object(download, "update"):
                directory = self.directory / ("out-" + extension)
                result = download.ffmpeg_download(1, self.base + "/sample." + extension, directory,
                                                  self.base + "/page", self.cookiefile, timeout=20)
                self.assertGreater(result["duration"], 0)
                self.assertTrue((directory / "video.mkv").is_file())
                info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                  str(directory / "video.mkv")], capture_output=True, text=True, check=True).stdout)
                self.assertEqual({s["codec_type"] for s in info["streams"]}, {"video", "audio"})
        self.assertTrue(self.headers_ok, self.header_failures)

    def test_mp4_hls_download_and_headers(self):
        self.check_downloads(("mp4", "m3u8"))
        self.assertTrue(any(".ts" in path for path in self.requests))

    def test_dash_download_and_headers(self):
        if "dash" not in download.ffmpeg_demuxers():
            self.skipTest("this FFmpeg build has no DASH demuxer (requires libxml2)")
        self.check_downloads(("mpd",))
        self.assertTrue(any(".m4s" in path for path in self.requests))

    def test_invalid_media_is_never_promoted(self):
        directory = self.directory / "invalid"
        with patch.object(download, "check_cancel"), patch.object(download, "update"):
            with self.assertRaises(RuntimeError):
                download.ffmpeg_download(1, self.base + "/bad.mp4", directory, self.base + "/page", self.cookiefile, timeout=20)
        self.assertFalse((directory / "video.mkv").exists())
        self.assertFalse((directory / "video.part.mkv").exists())

    def test_parallel_hls_actually_overlaps_fragment_requests(self):
        self.__class__.peak = 0
        directory = self.directory / "parallel-hls"
        directory.mkdir()
        with patch.object(download, "check_cancel"), patch.object(download, "update"), \
             patch.object(download, "ffmpeg_download", side_effect=AssertionError("native HLS must succeed")):
            meta = download.fast_stream_download(1, self.base + "/sample.m3u8", directory,
                                                 self.base + "/page", self.cookiefile)
        self.assertEqual(meta["source"], "sniffed-parallel")
        self.assertGreater(self.peak, 1, "fragment requests were still serial")
        self.assertTrue(self.headers_ok, self.header_failures)
        media = next(directory.glob("video.*"))
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(media)],
                                         capture_output=True, text=True, check=True).stdout)
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"video", "audio"})


if __name__ == "__main__":
    unittest.main()
