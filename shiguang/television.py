"""Bounded ADB access to the configured home TV; never accepts shell commands from HTTP."""
import copy
import ipaddress
import os
import re
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET


HOST = os.environ.get("TV_ADB_HOST", "192.168.3.214")
PORT = int(os.environ.get("TV_ADB_PORT", "5555"))
RECEIVER = "com.hpplay.happyplay.aw"
BUILTIN = "com.stv.smartControl"
BOOT_AD = "com.stv.bootadmanager"
# Reviewed against the installed 106.0.76 manifest. Keep SystemReceiver (fan
# alarm), UIService (shutdown handshake), AppService and the original launcher.
AD_COMPONENTS = (
    ("退出广告全屏页", "com.stv.admanager.ad.stickers.appexit.activity.AppExitAdFullActivity"),
    ("退出广告弹窗", "com.stv.admanager.ad.stickers.appexit.activity.AppExitUiActivity"),
    ("应用打开广告页", "com.stv.admanager.ad.stickers.appopen.activty.AppOpenAdActivity"),
    ("应用打开广告遮罩", "com.stv.admanager.ad.stickers.appopen.activty.AppOpenShadeActivity"),
    ("商业推送接收", "com.stv.admanager.push.PushMessageReceiver"),
    ("商业推送服务", "com.stv.admanager.push.PushMessageService"),
    ("屏保广告轮询", "com.stv.admanager.ad.screensaver.receiver.AlarmScreenLoopReceiver"),
)
_lock = threading.RLock()
_cache = None
_cache_at = 0


class TVError(RuntimeError):
    pass


class TVPermissionError(TVError):
    pass


def _can_manage_components(adb):
    permissions = adb.run("dumpsys package com.android.shell")
    if re.search(r"android\.permission\.CHANGE_COMPONENT_ENABLED_STATE:\s+granted=true", permissions):
        return True
    return adb.run("id").startswith("uid=0(")


class ADB:
    """Legacy CNXN/OPEN transport, sufficient for this Android 6 TV. No key generation."""
    def __init__(self, host=None, port=None, timeout=12):
        self.host, self.port = host or HOST, port or PORT
        address = ipaddress.ip_address(self.host)
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local:
            raise TVError("电视地址需要是局域网 IPv4 地址")
        self.deadline = time.monotonic() + timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=min(timeout, 3))
        self.local = 0
        try:
            self._send(b"CNXN", 0x01000000, 4096, b"host::\0")
            command, _, _, self.banner = self._recv()
            if command == b"AUTH":
                raise TVError("电视要求 ADB 授权，请在电视上允许连接")
            if command != b"CNXN":
                raise TVError("电视未返回有效的 ADB 握手")
        except Exception:
            self.sock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.sock.close()

    def _timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TVError("电视响应超时，请稍后重试")
        self.sock.settimeout(remaining)

    def _read(self, length):
        data = bytearray()
        while len(data) < length:
            self._timeout()
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise TVError("电视断开了 ADB 连接")
            data.extend(chunk)
        return bytes(data)

    def _send(self, command, arg0, arg1, payload=b""):
        self._timeout()
        value = int.from_bytes(command, "little")
        self.sock.sendall(struct.pack("<6I", value, arg0, arg1, len(payload), sum(payload), value ^ 0xffffffff) + payload)

    def _recv(self):
        command, arg0, arg1, length, checksum, magic = struct.unpack("<6I", self._read(24))
        if magic != command ^ 0xffffffff or length > 1024 * 1024:
            raise TVError("电视返回的 ADB 数据无效")
        payload = self._read(length)
        if checksum != sum(payload):
            raise TVError("电视返回的 ADB 校验失败")
        return command.to_bytes(4, "little"), arg0, arg1, payload

    def run(self, command, *, binary=False, limit=1024 * 1024):
        self.local += 1
        local, remote, result = self.local, None, bytearray()
        service = "exec:" if binary else "shell:"
        self._send(b"OPEN", local, 0, (service + command).encode() + b"\0")
        while True:
            kind, arg0, arg1, payload = self._recv()
            if arg1 != local or (remote is not None and arg0 != remote):
                raise TVError("电视返回了不匹配的 ADB 通道")
            remote = arg0
            if kind == b"WRTE":
                result.extend(payload)
                if len(result) > limit:
                    raise TVError("电视返回的数据超过大小限制")
                self._send(b"OKAY", local, remote)
            elif kind == b"CLSE":
                self._send(b"CLSE", local, remote)
                break
            elif kind != b"OKAY":
                raise TVError("电视返回了未知的 ADB 消息")
        return bytes(result) if binary else result.decode("utf-8", "replace").replace("\r", "")


def package_info(text):
    version = re.search(r"versionName=([^\s]+)", text)
    user = re.search(r"User 0:.*?installed=(true|false).*?stopped=(true|false).*?enabled=(\d+)", text)
    return {"installed": bool(user and user[1] == "true"), "version": version[1] if version else "",
            "stopped": bool(user and user[2] == "true"),
            "disabled": bool(user and user[3] in ("2", "3", "4")),
            "enabled_setting": int(user[3]) if user else None}


def _size(value):
    match = re.fullmatch(r"([\d.]+)([KMGT]?)", value, re.I)
    return int(float(match[1]) * 1024 ** ("KMGT".index(match[2].upper()) + 1)) if match and match[2] else int(float(value))


def storage_info(text):
    for line in text.splitlines():
        fields = line.split()
        if fields and (fields[0] == "/data" or fields[-1] == "/data") and len(fields) >= 4:
            try:
                total, used, free = map(_size, fields[1:4])
                return {"total": total, "used": used, "free": free}
            except (ValueError, TypeError):
                continue
    return None


def invalidate():
    global _cache, _cache_at
    with _lock:
        _cache, _cache_at = None, 0


def status(force=False):
    global _cache, _cache_at
    with _lock:
        if _cache is not None and time.monotonic() - _cache_at < (2 if force else 15):
            return copy.deepcopy(_cache)
        out = {"host": HOST, "port": PORT, "name": "乐视电视", "online": False, "adb": False,
               "checked_at": time.time(), "error": "", "packages": {}}
        try:
            with ADB(timeout=18) as adb:
                out.update(online=True, adb=True)
                props = adb.run("getprop ro.product.manufacturer; getprop ro.product.model; "
                                "getprop ro.build.version.release; getprop ro.letv.release.version").strip().splitlines()
                props += [""] * 4
                out.update(manufacturer=props[0], model=props[1], android=props[2], system=props[3])
                out["name"] = " ".join(filter(None, [props[0], props[1]])) or out["name"]
                uptime = adb.run("cat /proc/uptime").split()
                out["uptime"] = int(float(uptime[0])) if uptime else None
                mem = dict(re.findall(r"^(\w+):\s+(\d+)", adb.run("cat /proc/meminfo"), re.M))
                out["memory"] = {"total": int(mem.get("MemTotal", 0)) * 1024,
                                 "available": int(mem.get("MemAvailable", mem.get("MemFree", 0))) * 1024}
                out["storage"] = storage_info(adb.run("df /data"))
                for package in (RECEIVER, BUILTIN, BOOT_AD):
                    text = adb.run("dumpsys package " + package)
                    out["packages"][package] = package_info(text)
                    if package == BOOT_AD:
                        out["ads"] = ad_status(text)
                services = adb.run("dumpsys activity services " + RECEIVER)
                out["receiver_running"] = "ServiceRecord{" in services
                builtin = adb.run("dumpsys activity services " + BUILTIN)
                out["builtin_receiver_running"] = "com.stv.dmr.upnp.DMRService" in builtin or "com.stv.hpplay.LeboService" in builtin
                current = adb.run("dumpsys activity activities | grep mResumedActivity")
                match = re.search(r"u0 ([\w.]+)/", current)
                out["foreground_app"] = match[1] if match else ""
                out["ads"]["can_manage"] = _can_manage_components(adb)
                out["ads"]["reason"] = "" if out["ads"]["can_manage"] else "当前 ADB 没有修改系统广告组件的权限，广告屏蔽尚未应用。"
        except (OSError, TVError, ValueError) as exc:
            out["error"] = str(exc) if isinstance(exc, TVError) else "暂时连不上电视，请确认电视开机并连接家庭网络"
            # A partial probe is useful but never claims its remaining fields are fresh.
            out["adb"] = False
        _cache, _cache_at = out, time.monotonic()
        return copy.deepcopy(out)


def screenshot():
    with _lock, ADB(timeout=20) as adb:
        data = adb.run("screencap -p", binary=True, limit=16 * 1024 * 1024)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TVError("电视截图失败")
    return data


def _ui(adb):
    text = adb.run("uiautomator dump /dev/tty")
    start, end = text.find("<?xml"), text.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise TVError("无法读取电视设置界面，请在电视的网络设置中开启 DLNA & Airplay")
    try:
        return ET.fromstring(text[start:end + len("</hierarchy>")])
    except ET.ParseError as exc:
        raise TVError("电视设置界面无法识别") from exc


def _tap(adb, node):
    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not bounds:
        raise TVError("无法定位电视设置按钮")
    x1, y1, x2, y2 = map(int, bounds.groups())
    if x2 <= x1 or y2 <= y1:
        raise TVError("电视设置按钮当前不可见")
    adb.run(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")


def restore_receiver():
    """Use the vendor's own UI, which sends the privileged startup broadcast.

    No root, private broadcasts, force-stop, or guessed screen coordinates. Every
    click is bounded to the identified network settings widget on this firmware.
    """
    with _lock, ADB(timeout=45) as adb:
        result = adb.run("am start -n com.stv.globalsetting/.activity.NetworkActivity")
        if "Error" in result or "Exception" in result:
            raise TVError("无法打开乐视网络设置")
        root = _ui(adb)
        entry = next((n for n in root.iter("node") if n.get("text") == "多屏互动"
                      and n.get("package") == "com.stv.globalsetting"), None)
        if entry is None:
            raise TVError("没有找到多屏互动入口，请在电视网络设置中手动开启 DLNA & Airplay")
        _tap(adb, entry)
        root = _ui(adb)
        for attempt in range(2):
            switch = next((n for n in root.iter("node") if n.get("resource-id") == "com.stv.globalsetting:id/multi_screen"), None)
            if switch is None or not any(n.get("text") == "DLNA & Airplay" for n in switch.iter("node")):
                raise TVError("电视投屏开关与预期不符，已停止操作")
            states = [n.get("text") for n in switch.iter("node")]
            if not any(state in states for state in ("已开启", "已关闭")):
                raise TVError("无法确认电视投屏开关状态，已停止操作")
            # Cycle an already enabled but unresponsive receiver, otherwise turn it on once.
            _tap(adb, switch)
            root = _ui(adb)
            if "已关闭" in states:
                break
        enabled = adb.run("settings get system letv_multiscreen_select").strip()
        if enabled != "1":
            raise TVError("电视投屏尚未开启，请在电视网络设置中确认")
        current = adb.run("dumpsys activity activities | grep mResumedActivity")
        if "com.stv.globalsetting/.activity.NetworkActivity" in current:
            adb.run("input keyevent 3")
    invalidate()


def disabled_components(text):
    disabled, active, indent = set(), False, 0
    for line in text.splitlines():
        depth = len(line) - len(line.lstrip())
        if line.strip() == "disabledComponents:":
            active, indent = True, depth
            continue
        if active:
            if line.strip() and depth <= indent:
                active = False
            elif re.fullmatch(r"[\w.$]+", line.strip()):
                name = line.strip()
                disabled.add(BOOT_AD + name if name.startswith(".") else name)
    return disabled


def ad_status(text):
    disabled = disabled_components(text)
    package = package_info(text)
    return {"package_disabled": package["disabled"], "version": package["version"],
            "items": [{"name": name, "blocked": component in disabled} for name, component in AD_COMPONENTS]}


def change_ads(action, load_backup, save_backup):
    """Only the seven reviewed ad-only components, with a durable pre-change backup.

    Restoring enables only components that were enabled before this operation.
    A whole-package disable is explicitly rejected because it removes fan alarms.
    """
    if action not in ("block", "restore"):
        raise ValueError("不认识这个广告管理操作")
    try:
        with _lock, ADB(timeout=35) as adb:
            model = adb.run("getprop ro.product.model").strip()
            text = adb.run("dumpsys package " + BOOT_AD)
            package = package_info(text)
            if model != "Y55C" or package["version"] != "106.0.76" or not package["installed"]:
                raise TVError("电视型号或广告组件版本已变化，需要重新核实后才能操作")
            if package["disabled"]:
                raise TVError("广告管理包已被整体停用；拾光不会改变它的启用状态")
            if not _can_manage_components(adb):
                raise TVPermissionError("电视系统未授予当前 ADB 修改系统组件的权限，广告设置未改动")
            disabled = disabled_components(text)
            backup = load_backup()
            if backup is not None and not isinstance(backup, dict):
                raise TVError("广告屏蔽恢复记录无效，已停止操作")
            if backup and (backup.get("host") != HOST or backup.get("version") != package["version"]):
                raise TVError("恢复记录与当前电视不匹配，已停止操作")
            if action == "block" and not backup:
                backup = {"host": HOST, "model": model, "version": package["version"], "created": time.time(),
                          "disabled": {component: component in disabled for _, component in AD_COMPONENTS}}
                save_backup(backup)  # must complete before the first pm mutation
            if not backup or not isinstance(backup.get("disabled"), dict):
                raise TVError("没有可用的广告屏蔽恢复记录")
            expected_names = {component for _, component in AD_COMPONENTS}
            if set(backup["disabled"]) != expected_names or any(type(value) is not bool for value in backup["disabled"].values()):
                raise TVError("广告屏蔽恢复记录无效，已停止操作")
            changed = []
            for name, component in AD_COMPONENTS:
                wanted = True if action == "block" else backup["disabled"][component]
                if wanted == (component in disabled):
                    continue
                command = "disable-user" if wanted else "enable"
                reply = adb.run(f"pm {command} --user 0 {BOOT_AD}/{component}")
                if "Exception" in reply or "Error" in reply:
                    raise TVError(f"{name}操作失败；已保留恢复记录，请刷新后重试或恢复")
                changed.append(name)
            after = adb.run("dumpsys package " + BOOT_AD)
            actual = disabled_components(after)
            expected = {component for _, component in AD_COMPONENTS
                        if action == "block" or backup["disabled"][component]}
            if actual & expected_names != expected:
                raise TVError("电视未应用全部广告设置；已保留恢复记录，请刷新状态")
            if action == "restore":
                save_backup(None)
            return {"ads": ad_status(after), "changed": changed, "backup": backup}
    finally:
        invalidate()
