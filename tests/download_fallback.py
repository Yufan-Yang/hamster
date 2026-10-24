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
import sqlite3
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
        incomplete_patch = patch.object(pipeline, 'INCOMPLETE', self.directory / 'incomplete')
        incomplete_patch.start()
        self.addCleanup(incomplete_patch.stop)
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

    def failure_update(self, job_id, **fields):
        pipeline.update(job_id, **fields)
        return False

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

    def test_collected_player_preserves_query_alternatives_after_failure(self):
        primary = "https://media.test/stream.m3u8?id=11&quality=hd&token=old"
        alternate = "https://media.test/stream.m3u8?id=11&quality=sd&token=new"
        context = {"title": "Collected lesson", "referer": "https://page.test/lesson/11",
                   "media_urls": [primary, primary + "#duplicate", alternate]}

        def stream(job, url, directory, referer, cookies):
            self.assertEqual(referer, context["referer"])
            if url == primary:
                raise download.DownloadFailure("503 temporary fixture failure", retryable=True)
            self.assertEqual(url, alternate)
            return self.stream_ok(job, url, directory, referer, cookies)

        with patch.object(download, "fast_stream_download", side_effect=stream) as fetch, \
             patch.object(download, "sniff_page") as sniff:
            meta, directory = download.page_video_download(1, primary, self.work, media_context=context)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [primary, alternate])
        self.assertEqual(meta["webpage_url"], context["referer"])
        self.assertEqual(len(list(directory.rglob("*.mkv"))), 1)
        sniff.assert_not_called()

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

    def test_stream_deadlines_keep_the_job_eligible_for_auto_retry(self):
        for extension, downloader, message in (
            ("mp4", "ffmpeg_download", "独立流下载超过 45 分钟，已停止；请检查网络或改用网站的官方下载链接。"),
            ("m3u8", "ytdlp_download", "备用视频流下载超过 45 分钟，已停止，请检查网络后重试。"),
        ):
            with self.subTest(downloader=downloader):
                self.found["media_urls"] = ["https://media.test/main." + extension]
                with patch.object(download, "sniff_page", return_value=self.found), \
                     patch.object(download, "check_manifest"), \
                     patch.object(download, downloader, side_effect=download.DownloadFailure(message, retryable=True)):
                    with self.assertRaises(download.DownloadFailure) as caught:
                        download.page_video_download(1, "https://page.test/v", self.work)
                self.assertTrue(caught.exception.retryable)
                self.assertFalse((self.work / ".fallback").exists())
                with patch.object(pipeline, "process", side_effect=caught.exception), \
                     patch.object(pipeline, '_commit_job_failure', side_effect=self.failure_update), \
                     patch.object(pipeline, "q", return_value={"attempts": 0}), \
                     patch.object(pipeline, "finish_links"), patch.object(pipeline, "ring"), \
                     patch.object(pipeline, "LLM_API_KEY", ""), patch.object(pipeline.traceback, "print_exc"), \
                     patch.object(pipeline.time, "time", return_value=1000):
                    pipeline.run_job(1)
                self.assertEqual(pipeline.update.call_args.kwargs["retry_at"], 1060)

    def test_terminal_failure_overrides_an_earlier_retryable_stream_error(self):
        self.found["media_urls"] = ["https://media.test/one.mp4", "https://media.test/two.mp4"]
        for terminal in (OSError(errno.ENOSPC, "full"), PermissionError(errno.EACCES, "denied"),
                         RuntimeError("DRM protected"), RuntimeError("Login required"),
                         download.DownloadFailure("需要人工处理")):
            with self.subTest(error=str(terminal)), \
                 patch.object(download, "sniff_page", return_value=self.found), \
                 patch.object(download, "ffmpeg_download", side_effect=[
                     download.DownloadFailure("备用视频流下载超过 45 分钟", retryable=True), terminal,
                 ]) as ffmpeg:
                with self.assertRaises(download.DownloadFailure) as caught:
                    download.page_video_download(1, "https://page.test/v", self.work)
                self.assertEqual(ffmpeg.call_count, 2)
                self.assertFalse(caught.exception.retryable)

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

    def test_fragment_disconnect_retries_with_lower_concurrency_and_keeps_resume(self):
        connections = []

        def fetch(job, url, directory, opts):
            connections.append(opts["concurrent_fragment_downloads"])
            journal = directory / "video.mp4.ytdl"
            if len(connections) == 1:
                journal.write_text("completed fragments")
                raise RuntimeError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
            self.assertEqual(journal.read_text(), "completed fragments")
            (directory / "video.mp4").write_bytes(b"complete")
            return {"duration": 1}

        with patch.object(download, "check_manifest"), \
             patch.object(download, "ytdlp_download", side_effect=fetch), \
             patch.object(download, "ffmpeg_download") as ffmpeg:
            result = download.fast_stream_download(1, "https://media.test/v.m3u8", self.work, "https://page.test")
        self.assertEqual(connections, [8, 2])
        self.assertEqual(result["source"], "sniffed-parallel")
        ffmpeg.assert_not_called()
        self.assertEqual((self.work / "video.mp4").read_bytes(), b"complete")

    def test_truncated_fragments_remain_retryable_after_decoder_failure(self):
        self.found["media_urls"] = ["https://media.test/main.m3u8"]
        with patch.object(download, "sniff_page", return_value=self.found), \
             patch.object(download, "check_manifest"), \
             patch.object(download, "ytdlp_download", side_effect=RuntimeError("0 bytes read, 524000 more expected")) as native, \
             patch.object(download, "ffmpeg_download", side_effect=RuntimeError("Invalid data found when processing input")):
            with self.assertRaises(download.DownloadFailure) as caught:
                download.page_video_download(1, "https://page.test/v", self.work,
                                             primary_error=RuntimeError("Unsupported URL"))
        self.assertEqual(native.call_count, 2)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("524000", str(caught.exception))
        self.assertFalse((self.work / ".fallback").exists())
        with patch.object(pipeline, "process", side_effect=caught.exception), \
             patch.object(pipeline, '_commit_job_failure', side_effect=self.failure_update), \
             patch.object(pipeline, "q", return_value={"attempts": 0}), \
             patch.object(pipeline, "finish_links"), patch.object(pipeline, "ring"), \
             patch.object(pipeline, "LLM_API_KEY", ""), patch.object(pipeline.traceback, "print_exc"), \
             patch.object(pipeline.time, "time", return_value=1000):
            pipeline.run_job(1)
        self.assertEqual(pipeline.update.call_args.kwargs["retry_at"], 1060)

    def test_next_task_attempt_resumes_exact_candidate_without_promoting_failed_files(self):
        context = {'title': 'Resumed lesson', 'referer': 'https://page.test/lesson',
                   'media_urls': ['https://media.test/stream.m3u8?token=exact&quality=hd']}
        connections = []
        directories = []

        def native(job, url, directory, opts):
            connections.append(opts['concurrent_fragment_downloads'])
            directories.append(directory)
            if len(connections) <= 2:
                (directory / 'video.mp4.part').write_bytes(b'completed fragment bytes')
                (directory / 'video.mp4.ytdl').write_text('resume journal')
                (directory / 'failed.mp4').write_bytes(b'incomplete unverified output')
                raise RuntimeError('HTTP Error 503: Service not available')
            self.assertEqual((directory / 'video.mp4.part').read_bytes(), b'completed fragment bytes')
            self.assertEqual((directory / 'video.mp4.ytdl').read_text(), 'resume journal')
            self.assertFalse((directory / 'failed.mp4').exists())
            (directory / 'video.mp4').write_bytes(b'complete verified output')
            return {'duration': 1}

        with patch.object(download, 'check_manifest'), \
             patch.object(download, 'ytdlp_download', side_effect=native), \
             patch.object(download, 'ffmpeg_download', side_effect=RuntimeError('HTTP Error 502: Bad Gateway')):
            with self.assertRaises(download.DownloadFailure) as caught:
                download.page_video_download(1, 'https://page.test/lesson', self.work, media_context=context)
            self.assertTrue(caught.exception.retryable)
            self.assertFalse((self.work / '.fallback').exists())
            self.assertTrue(list((self.work / '.resume').rglob('*.ytdl')))
            meta, output = download.page_video_download(1, 'https://page.test/lesson', self.work, media_context=context)
        self.assertEqual(connections, [8, 2, 2])
        self.assertEqual(len(set(directories)), 1)
        self.assertEqual(meta['source'], 'sniffed-parallel')
        self.assertEqual([p.name for p in output.rglob('*') if p.is_file()], ['Resumed lesson 01.mp4'])
        self.assertFalse((self.work / '.resume').exists())

    def test_candidate_resume_isolates_query_scheme_referer_and_discards_stale_cache(self):
        addresses = [
            ('https://media.test/v.m3u8?ref=one&token=a', 'https://page.test/one'),
            ('https://media.test/v.m3u8?ref=two&token=a', 'https://page.test/one'),
            ('https://media.test/v.m3u8?ref=two&token=b', 'https://page.test/one'),
            ('http://media.test/v.m3u8?ref=two&token=b', 'https://page.test/one'),
            ('http://media.test/v.m3u8?ref=two&token=b', 'https://page.test/two'),
        ]
        directories = []
        def fail(job, url, directory, referer, cookiefile):
            self.assertFalse(list(directory.iterdir()), 'different candidate reused another stream')
            directories.append(directory)
            (directory / 'video.mp4.part').write_bytes(url.encode())
            (directory / 'video.mp4.ytdl').write_text('journal')
            raise download.DownloadFailure('503 temporary network failure', retryable=True)
        with patch.object(download, 'fast_stream_download', side_effect=fail):
            for address, referer in addresses:
                context = {'title': 'Lesson', 'referer': referer, 'media_urls': [address]}
                with self.assertRaises(download.DownloadFailure):
                    download.page_video_download(1, 'https://page.test/lesson', self.work, media_context=context)
                self.assertEqual(len(list((self.work / '.resume').iterdir())), 1)
        self.assertEqual(len(set(directories)), len(addresses))

    def test_retryable_candidate_cache_never_enters_successful_alternative(self):
        urls = ['https://media.test/a.m3u8?quality=hd', 'https://media.test/a.m3u8?quality=sd']
        def fetch(job, url, directory, *args):
            if url == urls[0]:
                (directory / 'video.mp4.part').write_bytes(b'partial first quality')
                (directory / 'bad.mp4').write_bytes(b'not complete')
                raise download.DownloadFailure('502 temporarily unavailable', retryable=True)
            self.assertFalse(list(directory.iterdir()))
            (directory / 'video.mp4').write_bytes(b'complete second quality')
            (directory / 'obsolete.part.mp4').write_bytes(b'stale partial')
            (directory / 'video.mp4.ytdl').write_text('obsolete journal')
            return {'source': 'sniffed-parallel'}
        with patch.object(download, 'fast_stream_download', side_effect=fetch):
            _, output = download.page_video_download(1, 'https://page.test', self.work,
                        media_context={'title': 'Selected lesson', 'media_urls': urls})
        self.assertEqual([p.name for p in output.rglob('*') if p.is_file()], ['Selected lesson 01.mp4'])
        self.assertFalse((self.work / '.resume').exists())

    def test_terminal_error_or_cancellation_cleans_retained_candidate_cache(self):
        context = {'title': 'Lesson', 'media_urls': ['https://media.test/v.m3u8']}
        def temporary(job, url, directory, *args):
            (directory / 'video.mp4.part').write_bytes(b'partial')
            raise download.DownloadFailure('503 temporary failure', retryable=True)
        for terminal in (download.Cancelled(), OSError(errno.ENOSPC, 'full'), download.DownloadFailure('需要人工处理')):
            with self.subTest(error=type(terminal).__name__):
                with patch.object(download, 'fast_stream_download', side_effect=temporary):
                    with self.assertRaises(download.DownloadFailure):
                        download.page_video_download(1, 'https://page.test', self.work, media_context=context)
                self.assertTrue((self.work / '.resume').exists())
                with patch.object(download, 'fast_stream_download', side_effect=terminal):
                    with self.assertRaises((download.DownloadFailure, download.Cancelled)):
                        download.page_video_download(1, 'https://page.test', self.work, media_context=context)
                self.assertFalse((self.work / '.resume').exists())

    def test_low_concurrency_retry_cancellation_and_terminal_errors_stop(self):
        for final in (download.Cancelled(), OSError(errno.ENOSPC, "full"), RuntimeError("Login required")):
            with self.subTest(error=type(final).__name__), \
                 patch.object(download, "check_manifest"), \
                 patch.object(download, "ytdlp_download", side_effect=[RuntimeError("IncompleteRead"), final]) as native, \
                 patch.object(download, "ffmpeg_download") as ffmpeg:
                with self.assertRaises((download.Cancelled, download.DownloadFailure)):
                    download.fast_stream_download(1, "https://media.test/v.m3u8", self.work, "https://page.test")
                self.assertEqual(native.call_count, 2)
                ffmpeg.assert_not_called()

    def test_transport_error_does_not_override_terminal_ffmpeg_failure(self):
        with patch.object(download, "check_manifest"), \
             patch.object(download, "ytdlp_download", side_effect=RuntimeError("IncompleteRead")), \
             patch.object(download, "ffmpeg_download", side_effect=download.DownloadFailure("需要人工处理")):
            with self.assertRaises(download.DownloadFailure) as caught:
                download.fast_stream_download(1, "https://media.test/v.m3u8", self.work, "https://page.test")
        self.assertFalse(caught.exception.retryable)

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
                 patch.object(pipeline, '_commit_job_failure', side_effect=self.failure_update), \
                 patch.object(pipeline, "q", return_value={"attempts": 0}), \
                 patch.object(pipeline, "finish_links"), patch.object(pipeline, "ring"), \
                 patch.object(pipeline, "LLM_API_KEY", ""), patch.object(pipeline.traceback, "print_exc"):
                pipeline.update.reset_mock()
                pipeline.run_job(1)
                self.assertIsNone(pipeline.update.call_args.kwargs["retry_at"])

    def test_temporary_503_keeps_three_bounded_task_retries(self):
        for prior, delay in enumerate((60, 300, 900, None)):
            with self.subTest(attempt=prior + 1), \
                 patch.object(pipeline, 'process', side_effect=RuntimeError('HTTP Error 503: Service not available')), \
                 patch.object(pipeline, '_commit_job_failure', side_effect=self.failure_update), \
                 patch.object(pipeline, 'q', return_value={'attempts': prior}), \
                 patch.object(pipeline, 'finish_links'), patch.object(pipeline, 'ring'), \
                 patch.object(pipeline, 'LLM_API_KEY', ''), patch.object(pipeline.traceback, 'print_exc'), \
                 patch.object(pipeline.time, 'time', return_value=1000):
                pipeline.run_job(1)
            self.assertEqual(pipeline.update.call_args.kwargs['attempts'], prior + 1)
            self.assertEqual(pipeline.update.call_args.kwargs['retry_at'], None if delay is None else 1000 + delay)

    def test_waiting_retry_can_be_cancelled_without_resetting_attempts(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,retry_at REAL,cancel INTEGER,stage TEXT,speed TEXT,updated REAL,attempts INTEGER)')
        db.execute("INSERT INTO jobs VALUES(1,'failed',1000,0,'','',0,2)")
        db.commit()
        partial = pipeline.INCOMPLETE / '1' / '.resume' / 'exact-candidate'
        partial.mkdir(parents=True)
        (partial / 'video.mp4.part').write_bytes(b'partial')
        with patch.object(pipeline.core, 'DB', db), patch.object(pipeline, 'finish_links') as links, patch.object(pipeline, 'ring'):
            self.assertEqual(pipeline.cancel_job(1), 'Cancelling #1')
            # The exact worker requeue predicate must no longer match this job.
            db.execute("UPDATE jobs SET status='queued',retry_at=NULL WHERE status='failed' AND retry_at IS NOT NULL AND retry_at<=?", (2000,))
        row = db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
        self.assertEqual((row['status'], row['retry_at'], row['attempts']), ('cancelled', None, 2))
        self.assertFalse(partial.exists())
        links.assert_called_once_with(1)

    def test_cancel_and_claim_with_separate_connections_never_revive_stale_selection(self):
        path = self.directory / 'claim-race.sqlite'
        worker, browser = sqlite3.connect(path), sqlite3.connect(path)
        self.addCleanup(worker.close); self.addCleanup(browser.close)
        worker.row_factory = browser.row_factory = sqlite3.Row
        worker.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,retry_at REAL,cancel INTEGER,stage TEXT,speed TEXT,updated REAL,attempts INTEGER)')
        worker.execute("INSERT INTO jobs VALUES(1,'failed',1000,0,'','',0,2)")
        worker.execute("UPDATE jobs SET status='queued',retry_at=NULL WHERE status='failed' AND retry_at<=2000")
        worker.commit()
        selected = worker.execute("SELECT id FROM jobs WHERE status='queued'").fetchone()
        with patch.object(pipeline.core, 'DB', browser), patch.object(pipeline, 'finish_links'), patch.object(pipeline, 'ring'):
            pipeline.cancel_job(1)
        with patch.object(pipeline.core, 'DB', worker):
            self.assertFalse(pipeline._claim_job(selected['id']))
        self.assertEqual(tuple(worker.execute('SELECT status,cancel,attempts FROM jobs').fetchone()), ('cancelled', 1, 2))
        # Opposite ordering: cancellation sees a claimed download and signals
        # the running child instead of marking its bytes as a completed cancel.
        worker.execute("UPDATE jobs SET status='queued',cancel=0")
        worker.commit()
        with patch.object(pipeline.core, 'DB', worker):
            self.assertTrue(pipeline._claim_job(1))
        with patch.object(pipeline.core, 'DB', browser), patch.object(pipeline, 'finish_links') as links, patch.object(pipeline, 'ring'):
            pipeline.cancel_job(1)
            links.assert_not_called()
        self.assertEqual(tuple(worker.execute('SELECT status,cancel FROM jobs').fetchone()), ('downloading', 1))

    def test_cancel_during_last_network_failure_does_not_schedule_an_uncancellable_retry(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,retry_at REAL,cancel INTEGER,stage TEXT,speed TEXT,updated REAL,attempts INTEGER,error TEXT,advice TEXT)')
        db.execute("INSERT INTO jobs VALUES(1,'downloading',NULL,0,'','',0,1,'','')")
        db.commit()
        partial = self.directory / 'incomplete' / '1'
        partial.mkdir(parents=True)
        (partial / 'video.mp4.part').write_bytes(b'partial')
        def late_failure(_job_id):
            db.execute('UPDATE jobs SET cancel=1 WHERE id=1')
            db.commit()
            raise RuntimeError('HTTP Error 503: Service not available')
        with patch.object(pipeline.core, 'DB', db), patch.object(pipeline, 'process', side_effect=late_failure), \
             patch.object(pipeline, 'INCOMPLETE', partial.parent), patch.object(pipeline, 'finish_links'), \
             patch.object(pipeline, 'ring'), patch.object(pipeline, 'LLM_API_KEY', ''), \
             patch.object(pipeline.traceback, 'print_exc'):
            pipeline.run_job(1)
        row = db.execute('SELECT status,retry_at,attempts FROM jobs').fetchone()
        self.assertEqual(tuple(row), ('cancelled', None, 1))
        self.assertFalse(partial.exists())


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
        cls.retry_hits = 0
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
        manifest = (cls.directory / "sample.m3u8").read_text()
        first_segment = next(line for line in manifest.splitlines() if line and not line.startswith("#"))
        shutil.copyfile(cls.directory / first_segment, cls.directory / "retry.ts")
        (cls.directory / "retry.m3u8").write_text(manifest.replace(first_segment, "retry.ts", 1))
        (cls.directory / "missing.m3u8").write_text(manifest.replace(first_segment, "missing.ts", 1))
        segments = [line for line in manifest.splitlines() if line and not line.startswith('#')]
        cls.resume_segments = [f'resume-{index}.ts' for index in range(len(segments))]
        resume_manifest = manifest
        for original, renamed in zip(segments, cls.resume_segments):
            shutil.copyfile(cls.directory / original, cls.directory / renamed)
            resume_manifest = resume_manifest.replace(original, renamed)
        (cls.directory / 'resume.m3u8').write_text(resume_manifest)
        cls.resume_blocked = True

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
                if self.path == "/retry.ts":
                    cls.retry_hits += 1
                    if cls.retry_hits == 1:
                        self.send_response(503)
                        self.send_header("Retry-After", "0")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                if self.path == "/missing.ts":
                    self.send_error(503)
                    return
                if self.path == '/' + cls.resume_segments[-1] and cls.resume_blocked:
                    self.send_error(503)
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

    def test_ffmpeg_reconnects_after_temporary_segment_failure(self):
        work = self.directory / "retry-output"
        type(self).retry_hits = 0
        with patch.object(download, "check_cancel"), patch.object(download, "update"):
            result = download.ffmpeg_download(1, self.base + "/retry.m3u8", work,
                                               self.base + "/page", str(self.cookiefile), timeout=20)
        self.assertGreaterEqual(type(self).retry_hits, 2)
        self.assertGreater(result["duration"], 0)
        self.assertTrue((work / "video.mkv").is_file())
        self.assertTrue(any(".ts" in path for path in self.requests))

    def test_ffmpeg_does_not_promote_video_with_missing_segment(self):
        work = self.directory / "missing-output"
        with patch.object(download, "check_cancel"), patch.object(download, "update"):
            with self.assertRaises((download.DownloadFailure, RuntimeError)):
                download.ffmpeg_download(1, self.base + "/missing.m3u8", work,
                                         self.base + "/page", str(self.cookiefile), timeout=20)
        self.assertFalse((work / "video.mkv").exists())

    def test_task_retry_uses_real_hls_resume_journal_without_repeating_completed_segments(self):
        work = self.directory / 'resume-output'
        work.mkdir()
        cls = type(self)
        cls.resume_blocked = True
        original_opts = download.ytdlp_opts
        def quick_options(*args, **kwargs):
            return {**original_opts(*args, **kwargs), 'retries': 0, 'fragment_retries': 0,
                    'retry_sleep_functions': {'fragment': lambda _attempt: 0}}
        cookiefile = self.directory / 'resume-cookies.txt'
        context = {'title': 'Resume test', 'referer': self.base + '/page',
                   'media_urls': [self.base + '/resume.m3u8'], 'cookiefile': str(cookiefile)}
        with patch.object(download, 'check_cancel'), patch.object(download, 'update'), \
             patch.object(download, 'ytdlp_opts', side_effect=quick_options), \
             patch.object(download, 'ffmpeg_download', side_effect=RuntimeError('HTTP Error 503: Service not available')):
            shutil.copyfile(cls.cookiefile, cookiefile)
            with self.assertRaises(download.DownloadFailure) as caught:
                download.page_video_download(1, self.base + '/page', work, media_context=context)
            self.assertTrue(caught.exception.retryable, str(caught.exception))
            self.assertTrue(list((work / '.resume').rglob('*.ytdl')))
            completed = {path: cls.requests.count('/' + path) for path in cls.resume_segments[:-1]}
            self.assertTrue(all(count == 1 for count in completed.values()), completed)
            cls.resume_blocked = False
            shutil.copyfile(cls.cookiefile, cookiefile)
            _, output = download.page_video_download(1, self.base + '/page', work, media_context=context)
        self.assertEqual({path: cls.requests.count('/' + path) for path in completed}, completed)
        videos = [path for path in output.rglob('*') if path.is_file()]
        self.assertEqual(len(videos), 1)
        info = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-of', 'json', str(videos[0])],
                                       capture_output=True, text=True, check=True).stdout)
        self.assertGreater(float(info['format']['duration']), 1)
        self.assertFalse((work / '.resume').exists())

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
