"""DLNA startup regression: a LeTV acknowledges Play long before it opens the new media."""
import importlib.util
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("cast_under_test", Path(__file__).resolve().parents[1] / "shiguang/cast.py")
cast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cast)
Thread = threading.Thread


def position(uri="http://pi/new.mp4", duration="00:01:30", relative="00:00:01"):
    return f"<r><TrackURI>{uri}</TrackURI><TrackDuration>{duration}</TrackDuration><RelTime>{relative}</RelTime></r>".encode()


class CastProtocol(unittest.TestCase):
    def setUp(self):
        cast._pending.clear()
        cast._start_errors.clear()
        self.calls = []
        self.position = position()
        self.state = "PLAYING"
        self.worker_gate = threading.Event()
        self.workers = []
        threads = patch.object(cast.threading, "Thread", side_effect=self.worker)
        threads.start()
        self.addCleanup(threads.stop)
        self.addCleanup(self.stop_workers)

    def worker(self, *, target, args, daemon):
        def run():
            self.worker_gate.wait()
            target(*args)
        worker = Thread(target=run, daemon=daemon)
        self.workers.append(worker)
        return worker

    def stop_workers(self):
        for did in list(cast._pending):
            with cast._control_lock(did):
                cast._cancel_pending(did)
        self.release_workers()

    def release_workers(self):
        self.worker_gate.set()
        for worker in self.workers:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "startup worker did not finish")

    def soap(self, did, action, **args):
        self.calls.append((action, args))
        if action == "GetPositionInfo":
            return self.position
        if action == "GetTransportInfo":
            return f"<r><CurrentTransportState>{self.state}</CurrentTransportState></r>".encode()
        if action == "GetVolume":
            return b"<r><CurrentVolume>32</CurrentVolume></r>"
        if action == "Seek":
            self.position = position(relative=args["Target"])
        if action == "Pause":
            self.state = "PAUSED_PLAYBACK"
        return b"<r/>"

    def start(self, url="http://pi/new.mp4", seek=35):
        with patch.object(cast, "_device", return_value={"name": "LeTV"}):
            cast.start("tv", url, "Test", position=seek)

    def test_seek_waits_for_new_track_and_real_duration(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start()
            pending = cast._pending["tv"]
            self.assertNotIn("Seek", [action for action, _ in self.calls])
            for stale in (position(duration="00:00:00"), position(uri="http://pi/old.mp4")):
                self.position = stale
                self.assertTrue(cast._advance_start("tv", pending))
                result = cast.status("tv")
                self.assertEqual(result["state"], "TRANSITIONING")
                self.assertEqual(result["position"], 35)
                self.assertNotIn("Seek", [action for action, _ in self.calls])
            self.position = position()
            self.assertFalse(cast._advance_start("tv", pending))
            result = cast.status("tv")
            self.assertEqual(result["position"], 35)
            self.assertNotIn("tv", cast._pending)
            seeks = [args for action, args in self.calls if action == "Seek"]
            self.assertEqual(seeks, [{"Unit": "REL_TIME", "Target": "00:00:35"}])

    def test_next_episode_replaces_pending_seek_and_early_pause_is_retained(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start("http://pi/old.mp4", 50)
            old = cast._pending["tv"]
            self.start(seek=12)
            cast.control("tv", "seek", position=23)
            cast.control("tv", "pause")
            self.assertTrue(old["cancel"].is_set())
            self.release_workers()
            result = cast.status("tv")
            self.assertEqual(result["position"], 23)
            self.assertEqual(result["state"], "PAUSED_PLAYBACK")
            self.assertEqual([args["Target"] for action, args in self.calls if action == "Seek"], ["00:00:23"])

    def test_stop_cancels_queued_seek(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start(); cast.control("tv", "stop"); self.release_workers(); cast.status("tv")
            self.assertNotIn("Seek", [action for action, _ in self.calls])
            self.assertNotIn("tv", cast._pending)

    def test_stalled_start_is_reported_instead_of_false_playing(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start()
            cast._pending["tv"]["until"] = time.monotonic() - 1
            self.position = position(duration="00:00:00")
            self.release_workers()
            self.assertNotIn("tv", cast._pending)
            with self.assertRaisesRegex(RuntimeError, "尚未开始播放"):
                cast.status("tv")

    def test_server_applies_resume_and_pause_without_browser_status_requests(self):
        first_probe = threading.Event()
        self.position = position(duration="00:00:00")
        def soap(did, action, **args):
            result = self.soap(did, action, **args)
            if action == "GetPositionInfo":
                first_probe.set()
            return result
        with patch.object(cast, "_soap", side_effect=soap):
            self.start(seek=12)
            cast.control("tv", "seek", position=23)
            cast.control("tv", "pause")
            self.worker_gate.set()
            self.assertTrue(first_probe.wait(2), "server did not poll the TV")
            self.position = position()
            self.release_workers()
            self.assertNotIn("tv", cast._pending)
            self.assertEqual([args["Target"] for action, args in self.calls if action == "Seek"], ["00:00:23"])
            self.assertEqual([action for action, _ in self.calls].count("Pause"), 1)
            # Subsequent UI polling cannot execute the delayed controls again.
            cast.status("tv"); cast.status("tv")
            self.assertEqual([action for action, _ in self.calls].count("Seek"), 1)
            self.assertEqual([action for action, _ in self.calls].count("Pause"), 1)

    def test_status_reads_do_not_apply_pending_controls(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start()
            for _ in range(2):
                self.assertEqual(cast.status("tv")["state"], "TRANSITIONING")
            self.assertNotIn("Seek", [action for action, _ in self.calls])
            self.release_workers()
            self.assertEqual([action for action, _ in self.calls].count("Seek"), 1)

    def test_worker_retries_transient_status_error_and_stops_after_completion(self):
        failed = []
        def soap(did, action, **args):
            if action == "GetPositionInfo" and not failed:
                failed.append(True)
                raise RuntimeError("TV is still opening media")
            return self.soap(did, action, **args)
        with patch.object(cast, "_soap", side_effect=soap):
            self.start()
            self.release_workers()
            self.assertTrue(failed)
            self.assertNotIn("tv", cast._pending)
            self.assertEqual([action for action, _ in self.calls].count("Seek"), 1)

    def test_failed_deferred_control_is_reported_without_repeated_seek(self):
        def soap(did, action, **args):
            if action == "Seek":
                self.calls.append((action, args))
                raise RuntimeError("Seek failed")
            return self.soap(did, action, **args)
        with patch.object(cast, "_soap", side_effect=soap):
            self.start()
            self.release_workers()
            self.assertNotIn("tv", cast._pending)
            with self.assertRaisesRegex(RuntimeError, "Seek failed"):
                cast.status("tv")
            cast.status("tv")
            self.assertEqual([action for action, _ in self.calls].count("Seek"), 1)

    def test_replacement_cannot_interleave_with_old_deferred_seek(self):
        seeking, release_seek = threading.Event(), threading.Event()
        replacing, replacement_uri = threading.Event(), threading.Event()
        errors = []
        def soap(did, action, **args):
            if action == "SetAVTransportURI":
                self.position = position(uri=args["CurrentURI"])
                if args["CurrentURI"] == "http://pi/next.mp4":
                    replacement_uri.set()
            if action == "Seek" and not seeking.is_set():
                seeking.set()
                if not release_seek.wait(2):
                    raise RuntimeError("test did not release Seek")
            return self.soap(did, action, **args)
        def replace():
            replacing.set()
            try:
                self.start("http://pi/next.mp4", 12)
            except Exception as exc:
                errors.append(exc)
        with patch.object(cast, "_soap", side_effect=soap):
            self.start(seek=35)
            self.worker_gate.set()
            self.assertTrue(seeking.wait(2))
            replacement = Thread(target=replace, daemon=True)
            self.workers.append(replacement)
            replacement.start()
            try:
                self.assertTrue(replacing.wait(2))
                self.assertFalse(replacement_uri.wait(.1), "new URI replaced the track before the old Seek finished")
            finally:
                release_seek.set()
            self.release_workers()
            self.assertFalse(errors)
            self.assertTrue(replacement_uri.is_set())
            actions = [(action, args.get("CurrentURI") or args.get("Target"))
                       for action, args in self.calls if action in ("SetAVTransportURI", "Seek")]
            self.assertEqual(actions, [("SetAVTransportURI", "http://pi/new.mp4"), ("Seek", "00:00:35"),
                                       ("SetAVTransportURI", "http://pi/next.mp4"), ("Seek", "00:00:12")])

    def test_description_honors_urlbase_and_newer_service_versions(self):
        xml = b'''<root><URLBase>http://192.168.3.214:49152/dlna/</URLBase><device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:2</deviceType><friendlyName>LeTV</friendlyName>
        <service><serviceType>urn:schemas-upnp-org:service:AVTransport:2</serviceType><controlURL>play</controlURL></service>
        <service><serviceType>urn:schemas-upnp-org:service:RenderingControl:3</serviceType><controlURL>volume</controlURL></service>
        </device></root>'''
        response = Mock(content=xml)
        with patch.object(cast._http, "get", return_value=response):
            descriptor = cast._description("http://192.168.3.214:49152/description.xml")
        self.assertEqual(descriptor["control"], "http://192.168.3.214:49152/dlna/play")
        self.assertEqual(descriptor["transport_type"], "urn:schemas-upnp-org:service:AVTransport:2")
        with patch.object(cast, "_device", return_value=descriptor), patch.object(cast._http, "post", return_value=Mock(ok=True, content=b"")) as post:
            cast._soap("tv", "Play", Speed="1")
            self.assertIn("AVTransport:2#Play", post.call_args.kwargs["headers"]["SOAPACTION"])
        self.assertFalse(cast._http.trust_env)


if __name__ == "__main__":
    unittest.main()
