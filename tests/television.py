"""Offline ADB framing, bounded reads, authentication and status regressions (stdlib)."""
import importlib.util
from pathlib import Path
import struct
import time
import copy
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("tv_under_test", Path(__file__).resolve().parents[1] / "shiguang/television.py")
tv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv)


def packet(command, a=0, b=0, payload=b""):
    value = int.from_bytes(command, "little")
    return struct.pack("<6I", value, a, b, len(payload), sum(payload), value ^ 0xffffffff) + payload


class Wire:
    def __init__(self, data):
        self.data, self.sent, self.closed = data, [], False

    def recv(self, length):
        result, self.data = self.data[:min(length, 3)], self.data[min(length, 3):]
        return result

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


class TelevisionTests(unittest.TestCase):
    def tearDown(self):
        tv.invalidate()

    def connect(self, data):
        wire = Wire(packet(b"CNXN", payload=b"device::\0") + data)
        with patch.object(tv.socket, "create_connection", return_value=wire):
            return tv.ADB(), wire

    def test_fragmented_binary_stream_and_acknowledgements(self):
        payload = b"\x89PNG\r\n\x1a\n\0\xff"
        adb, wire = self.connect(packet(b"OKAY", 7, 1) + packet(b"WRTE", 7, 1, payload) + packet(b"CLSE", 7, 1))
        with adb:
            self.assertEqual(adb.run("screencap -p", binary=True), payload)
        self.assertTrue(wire.closed)
        self.assertIn(b"exec:screencap -p\0", wire.sent[1])
        self.assertEqual(wire.sent[-2][:4], b"OKAY")
        self.assertEqual(wire.sent[-1][:4], b"CLSE")

    def test_authentication_required_never_sends_shell_command(self):
        wire = Wire(packet(b"AUTH", 1, payload=b"challenge"))
        with patch.object(tv.socket, "create_connection", return_value=wire):
            with self.assertRaisesRegex(tv.TVError, "授权"):
                tv.ADB()
        self.assertTrue(wire.closed)
        self.assertEqual(len(wire.sent), 1)

    def test_closed_stream_is_reported(self):
        adb, _ = self.connect(packet(b"OKAY", 7, 1))
        with adb, self.assertRaisesRegex(tv.TVError, "断开"):
            adb.run("id")

    def test_bad_checksums_channels_and_oversized_output_are_rejected(self):
        invalid = bytearray(packet(b"WRTE", 7, 1, b"test")); invalid[-1] ^= 1
        for stream, message, limit in (
            (bytes(invalid), "校验", 100),
            (packet(b"WRTE", 7, 2, b"test"), "通道", 100),
            (packet(b"WRTE", 7, 1, b"test"), "大小限制", 3),
        ):
            adb, _ = self.connect(stream)
            with adb, self.assertRaisesRegex(tv.TVError, message):
                adb.run("id", limit=limit)

    def test_total_deadline_is_enforced(self):
        adb, _ = self.connect(b"")
        adb.deadline = time.monotonic() - 1
        with adb, self.assertRaisesRegex(tv.TVError, "超时"):
            adb.run("id")

    def test_host_must_be_private_ipv4(self):
        for host in ("127.0.0.1", "8.8.8.8", "169.254.1.1", "::1", "example.com"):
            with self.subTest(host=host), self.assertRaises((ValueError, tv.TVError)):
                tv.ADB(host=host)

    def test_package_flags_and_human_storage(self):
        self.assertEqual(tv.storage_info("Filesystem Size Used Free Blksize\n/data 8.9G 7.5G 1.4G 4096\n")["free"], int(1.4 * 1024 ** 3))
        info = tv.package_info("versionName=8.18.53\nUser 0: installed=true hidden=false stopped=true notLaunched=false enabled=3")
        self.assertTrue(info["installed"] and info["disabled"] and info["stopped"])
        self.assertEqual(info["version"], "8.18.53")
        self.assertFalse(tv.package_info("Unable to find package")["installed"])

    def test_offline_result_is_cached_bounded_and_not_mutable_by_callers(self):
        with patch.object(tv, "ADB", side_effect=OSError("unreachable")) as probe:
            result = tv.status()
            self.assertFalse(result["online"])
            result["packages"]["injected"] = True
            self.assertNotIn("injected", tv.status()["packages"])
            self.assertEqual(probe.call_count, 1)
            tv.invalidate(); tv.status()
            self.assertEqual(probe.call_count, 2)

    def test_receiver_recovery_refuses_an_unrecognized_settings_screen(self):
        with patch.object(tv, "ADB") as client:
            adb = client.return_value.__enter__.return_value
            adb.run.side_effect = ["Starting", '<?xml version="1.0"?><hierarchy><node text="恢复出厂" package="com.stv.globalsetting" bounds="[0,0][100,100]"/></hierarchy>']
            with self.assertRaisesRegex(tv.TVError, "多屏互动"):
                tv.restore_receiver()
            self.assertFalse(any("input tap" in call.args[0] for call in adb.run.call_args_list))

    def test_receiver_recovery_identifies_widgets_before_toggling(self):
        entry = '<?xml version="1.0"?><hierarchy><node text="多屏互动" package="com.stv.globalsetting" bounds="[150,409][306,462]"/></hierarchy>'
        def screen(state):
            return '<?xml version="1.0"?><hierarchy><node resource-id="com.stv.globalsetting:id/multi_screen" bounds="[654,296][1806,446]"><node text="DLNA &amp; Airplay"/><node text="' + state + '"/></node></hierarchy>'
        with patch.object(tv, "ADB") as client:
            adb = client.return_value.__enter__.return_value
            adb.run.side_effect = ["Starting", entry, "", screen("已开启"), "", screen("已关闭"), "", screen("已开启"), "1", "com.stv.globalsetting/.activity.NetworkActivity", ""]
            tv.restore_receiver()
            taps = [call.args[0] for call in adb.run.call_args_list if "input tap" in call.args[0]]
            self.assertEqual(taps, ["input tap 228 435", "input tap 1230 371", "input tap 1230 371"])

    def test_ads_save_before_mutation_and_restore_only_originally_enabled_components(self):
        original = tv.AD_COMPONENTS[0][1]
        disabled, saved, commands = {original}, [None], []

        def snapshot():
            return ('versionName=106.0.76\nUser 0: installed=true hidden=false stopped=false notLaunched=false enabled=0\n'
                    '      disabledComponents:\n' + ''.join('        ' + name + '\n' for name in sorted(disabled)) + 'Shared users:\n')

        def run(command):
            commands.append(command)
            if command == "getprop ro.product.model":
                return "Y55C\n"
            if command == "dumpsys package com.android.shell":
                return "android.permission.CHANGE_COMPONENT_ENABLED_STATE: granted=true\n"
            if command.startswith("dumpsys package "):
                return snapshot()
            self.assertIsNotNone(saved[0], "durable backup must precede first mutation")
            parts = command.split()
            self.assertEqual(parts[:1], ["pm"])
            self.assertEqual(parts[2:4], ["--user", "0"])
            package, component = parts[4].split("/", 1)
            self.assertEqual(package, tv.BOOT_AD)
            self.assertIn(component, dict((component, name) for name, component in tv.AD_COMPONENTS))
            (disabled.add if parts[1] == "disable-user" else disabled.discard)(component)
            return "Component new state\n"

        def save(value):
            saved[0] = copy.deepcopy(value)

        with patch.object(tv, "ADB") as client:
            client.return_value.__enter__.return_value.run.side_effect = run
            result = tv.change_ads("block", lambda: saved[0], save)
            self.assertTrue(all(item["blocked"] for item in result["ads"]["items"]))
            self.assertEqual(len(result["changed"]), len(tv.AD_COMPONENTS) - 1)
            tv.change_ads("restore", lambda: saved[0], save)
        self.assertEqual(disabled, {original})
        self.assertIsNone(saved[0])
        self.assertFalse(any(command == 'pm disable-user --user 0 ' + tv.BOOT_AD for command in commands))
        self.assertFalse(any('SystemReceiver' in command or 'UIService' in command or 'deskplatform' in command for command in commands))

    def test_ads_reject_unreviewed_versions_and_invalid_actions(self):
        with self.assertRaises(ValueError):
            tv.change_ads("shell", lambda: None, lambda value: None)
        with patch.object(tv, "ADB") as client:
            adb = client.return_value.__enter__.return_value
            adb.run.side_effect = ["Y55C", "versionName=next\nUser 0: installed=true stopped=false enabled=0"]
            with self.assertRaisesRegex(tv.TVError, "版本"):
                tv.change_ads("block", lambda: None, lambda value: self.fail("must not save a backup"))
            self.assertFalse(any("pm " in call.args[0] for call in adb.run.call_args_list))

    def test_ads_refuse_missing_device_permission_before_saving_or_mutating(self):
        with patch.object(tv, "ADB") as client:
            adb = client.return_value.__enter__.return_value
            adb.run.side_effect = ["Y55C", "versionName=106.0.76\nUser 0: installed=true stopped=false enabled=0", "", "uid=2000(shell)"]
            with self.assertRaisesRegex(tv.TVPermissionError, "权限"):
                tv.change_ads("block", lambda: self.fail("backup must not be read"), lambda value: self.fail("backup must not be written"))
            self.assertFalse(any("pm " in call.args[0] for call in adb.run.call_args_list))


if __name__ == "__main__":
    unittest.main()
