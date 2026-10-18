"""Room event subscriptions; only state changes wake the recording scheduler.

Wire protocol reference: https://github.com/xfgryujk/blivedm/tree/master/blivedm/clients
"""
import json
import logging
import random
import struct
import threading
import time
import zlib

from websockets.sync.client import connect

from . import channels, core
from . import live_bilibili as bili


POLL_SECONDS = 30 * 60
HEADER = struct.Struct(">IHHII")
MAX_PACKET = 8 << 20
LOG = logging.getLogger(__name__)


def packet(operation, data):
    body = json.dumps(data).encode()
    return HEADER.pack(HEADER.size + len(body), HEADER.size, 1, operation, 1) + body


def messages(raw, depth=0):
    """Decode concatenated packets, including nested zlib packets requested by protover=2."""
    if depth > 3 or len(raw) > MAX_PACKET:
        raise ValueError("直播事件数据超出限制")
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < HEADER.size:
            raise ValueError("直播事件数据不完整")
        length, size, version, operation, _ = HEADER.unpack_from(raw, offset)
        if not HEADER.size <= size <= length <= len(raw) - offset:
            raise ValueError("直播事件包长度无效")
        body = raw[offset + size:offset + length]
        offset += length
        if operation == 5 and version == 2:
            decoder = zlib.decompressobj()
            inflated = decoder.decompress(body, MAX_PACKET + 1)
            if len(inflated) > MAX_PACKET or decoder.unconsumed_tail or not decoder.eof:
                raise ValueError("直播事件压缩数据无效")
            yield from messages(inflated, depth + 1)
        elif operation in (5, 8):
            if version not in (0, 1):
                raise ValueError("直播事件使用了不支持的协议")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("直播事件内容无效")
            yield operation, value
        elif operation == 3:
            yield operation, {}


def request_check(source, reason, delay=0):
    core.kv_set(f"live-check:{source['id']}", {
        "at": time.time() + delay, "room_id": source["room_id"], "owner": source["owner"],
        "reason": reason, "token": random.getrandbits(64)})


def check_request(source):
    value = core.kv_get(f"live-check:{source['id']}")
    if value and value.get("room_id") == source["room_id"] and value.get("owner") == source["owner"]:
        return value
    return None


def check_due(source, now, request=None):
    if source["checked"] is None:  # Manual checks and newly added/edited sources bypass the timer.
        return True
    if request:
        return request["at"] <= now
    return source["checked"] <= now - POLL_SECONDS


def consume_check(source, request):
    if request:
        # An event arriving during a query must survive that query's completion.
        core.q("DELETE FROM kv WHERE k=? AND v=?", (f"live-check:{source['id']}", json.dumps(request)))


def connection_info(source):
    with bili.client(source["owner"], recording=True) as session:
        response = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
        response.raise_for_status()
        data = response.json().get("data") or {}  # Anonymous nav has code=-101 but still contains WBI keys.
        img = data.get("wbi_img") or {}
        raw = "".join(str(img.get(k) or "").rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url"))
        if len(raw) < 64:
            raise ValueError("暂时无法获取直播事件签名")
        session.wbi_key = "".join(raw[i] for i in channels.MIXIN_KEY)[:32]
        buvid = next((c.value for c in session.cookies if c.name == "buvid3"), "")
        if not buvid:
            buvid = bili.api(session, "https://api.bilibili.com/x/frontend/finger/spi").get("b_3") or ""
        config = bili.api(session, "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
                          **channels.bili_signed(session, {"id": source["room_id"], "type": 0}))
        urls = []
        for host in config.get("host_list") or []:
            name = str(host.get("host") or "")
            port = int(host.get("wss_port") or 443)
            if name.endswith(".chat.bilibili.com") and all(c.isalnum() or c in ".-" for c in name) and 0 < port < 65536:
                urls.append(f"wss://{name}:{port}/sub")
        if not urls or not config.get("token"):
            raise ValueError("暂时无法获取直播事件服务器")
        uid = int(data.get("mid") or 0) if data.get("isLogin") else 0
        return urls, {"uid": uid, "roomid": int(source["room_id"]), "protover": 2,
                      "platform": "web", "type": 2, "buvid": buvid, "key": config["token"]}


class Listener:
    def __init__(self, source):
        self.source = dict(source)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f"live-events-{source['id']}", daemon=True)
        self.last_event = {}

    def status(self, state, error="", retry_at=None):
        core.kv_set(f"live-monitor:{self.source['id']}", {
            "state": state, "error": error, "updated": time.time(), "retry_at": retry_at,
            "room_id": self.source["room_id"], "owner": self.source["owner"]})

    def handle(self, operation, data):
        if operation == 8:
            if data.get("code") != 0:
                raise ValueError("直播事件鉴权失败")
            self.status("connected")
            # Reconcile the initial status and anything missed while disconnected.
            request_check(self.source, "connected")
            return True
        if operation == 5:
            command = str(data.get("cmd") or "").split(":", 1)[0]
            room = data.get("roomid", data.get("room_id"))
            if room is not None and str(room) != str(self.source["room_id"]):
                return False
            if command in ("LIVE", "PREPARING"):
                now = time.monotonic()
                if now - self.last_event.get(command, -100) >= 10:
                    self.last_event[command] = now
                    request_check(self.source, command)
        return False

    def listen(self, url, auth):
        with connect(url, user_agent_header=core.UA, origin="https://live.bilibili.com", compression=None,
                     open_timeout=10, close_timeout=2, ping_interval=None, max_size=MAX_PACKET) as ws:
            ws.send(packet(7, auth))
            start = time.monotonic()
            authenticated = False
            heartbeat = start
            last_reply = start
            reported = start
            while not self.stop.is_set():
                now = time.monotonic()
                if not authenticated and now - start > 15:
                    raise ValueError("直播事件鉴权超时")
                if authenticated and now - last_reply > 75:
                    raise ValueError("直播事件心跳超时")
                if authenticated and now >= heartbeat:
                    ws.send(packet(2, {}))
                    heartbeat = now + 30
                try:
                    raw = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                if not isinstance(raw, bytes):
                    continue
                for operation, data in messages(raw):
                    if operation == 8:
                        authenticated = self.handle(operation, data)
                        last_reply = now
                    elif authenticated:
                        self.handle(operation, data)
                        if operation == 3:
                            last_reply = now
                            if now - reported >= 60:
                                self.status("connected")
                                reported = now

    def run(self):
        attempts = 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.status("connecting")
                urls, auth = connection_info(self.source)
                self.listen(urls[attempts % len(urls)], auth)
                if self.stop.is_set():
                    break
                raise ValueError("直播事件连接已断开")
            except Exception as exc:
                if self.stop.is_set():
                    break
                if time.monotonic() - started >= 120:
                    attempts = 0
                delay = min(600, 30 * 2 ** min(attempts, 5)) + random.uniform(0, 5)
                attempts += 1
                # Never log cookies, signed query strings, WebSocket URLs or authentication payloads.
                error = str(exc) if isinstance(exc, ValueError) else f"直播事件连接暂不可用（{type(exc).__name__}）"
                LOG.warning("live events source=%s: %s", self.source["id"], error)
                self.status("retrying", error, time.time() + delay)
                if self.stop.wait(delay):
                    break
        self.status("stopped")


class Subscriptions:
    def __init__(self):
        self.listeners = {}

    def sync(self, sources):
        desired = {row["id"]: dict(row) for row in sources if row["enabled"]}
        for sid, listener in list(self.listeners.items()):
            source = desired.get(sid)
            if not listener.thread.is_alive():
                del self.listeners[sid]
                continue
            if source is None or any(listener.source[k] != source[k] for k in ("room_id", "owner")):
                listener.stop.set()
                # Wait for the old thread before starting its replacement; it may be inside an HTTP request.
                if not listener.thread.is_alive():
                    del self.listeners[sid]
        for sid, source in desired.items():
            if sid not in self.listeners:
                listener = self.listeners[sid] = Listener(source)
                listener.thread.start()

    def close(self):
        for listener in self.listeners.values():
            listener.stop.set()
