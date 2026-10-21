"""DLNA startup regression: a LeTV acknowledges Play long before it opens the new media."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("cast_under_test", Path(__file__).resolve().parents[1] / "shiguang/cast.py")
cast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cast)


def position(uri="http://pi/new.mp4", duration="00:01:30", relative="00:00:01"):
    return f"<r><TrackURI>{uri}</TrackURI><TrackDuration>{duration}</TrackDuration><RelTime>{relative}</RelTime></r>".encode()


class CastProtocol(unittest.TestCase):
    def setUp(self):
        cast._pending.clear()
        self.calls = []
        self.position = position()

    def soap(self, did, action, **args):
        self.calls.append((action, args))
        if action == "GetPositionInfo":
            return self.position
        if action == "GetTransportInfo":
            return b"<r><CurrentTransportState>PLAYING</CurrentTransportState></r>"
        if action == "GetVolume":
            return b"<r><CurrentVolume>32</CurrentVolume></r>"
        if action == "Seek":
            self.position = position(relative=args["Target"])
        return b"<r/>"

    def start(self, url="http://pi/new.mp4", seek=35):
        with patch.object(cast, "_device", return_value={"name": "LeTV"}):
            cast.start("tv", url, "Test", position=seek)

    def test_seek_waits_for_new_track_and_real_duration(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start()
            self.assertNotIn("Seek", [action for action, _ in self.calls])
            for stale in (position(duration="00:00:00"), position(uri="http://pi/old.mp4")):
                self.position = stale
                result = cast.status("tv")
                self.assertEqual(result["state"], "TRANSITIONING")
                self.assertEqual(result["position"], 35)
                self.assertNotIn("Seek", [action for action, _ in self.calls])
            self.position = position()
            result = cast.status("tv")
            self.assertEqual(result["position"], 35)
            self.assertNotIn("tv", cast._pending)
            seeks = [args for action, args in self.calls if action == "Seek"]
            self.assertEqual(seeks, [{"Unit": "REL_TIME", "Target": "00:00:35"}])

    def test_next_episode_replaces_pending_seek_and_early_pause_is_retained(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start("http://pi/old.mp4", 50)
            self.start(seek=12)
            cast.control("tv", "seek", position=23)
            cast.control("tv", "pause")
            result = cast.status("tv")
            self.assertEqual(result["position"], 23)
            self.assertEqual(result["state"], "PAUSED_PLAYBACK")
            self.assertEqual([args["Target"] for action, args in self.calls if action == "Seek"], ["00:00:23"])

    def test_stop_cancels_queued_seek(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start(); cast.control("tv", "stop"); cast.status("tv")
            self.assertNotIn("Seek", [action for action, _ in self.calls])

    def test_stalled_start_is_reported_instead_of_false_playing(self):
        with patch.object(cast, "_soap", side_effect=self.soap):
            self.start()
            cast._pending["tv"]["until"] = time.monotonic() - 1
            self.position = position(duration="00:00:00")
            with self.assertRaisesRegex(RuntimeError, "尚未开始播放"):
                cast.status("tv")
            self.assertNotIn("tv", cast._pending)

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
