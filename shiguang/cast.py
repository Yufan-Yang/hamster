"""Small DLNA/UPnP control point used by the watch page.

The TV fetches the media URL from the Pi itself.  Discovery and SOAP controls
therefore belong on the Pi: browsers cannot send SSDP multicast packets.
"""
import hashlib
import html
import math
import os
import socket
import threading
import time
import urllib.parse
import weakref
import xml.etree.ElementTree as ET

import requests


SSDP = ("239.255.255.250", 1900)
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
_devices = {}  # opaque id -> descriptor; a short cache also prevents forged control URLs
_refresh_lock = threading.Lock()
_last_refresh = -float("inf")
_pending = {}  # renderer id -> requested media, seek and pause until the TV has opened it
_start_errors = {}
_control_locks = weakref.WeakValueDictionary()
_control_locks_guard = threading.Lock()
_known_locations = set()
_http = requests.Session()
_http.trust_env = False  # private TV traffic must never take the internet proxy


def _tag(node, name):
    return next((x.text or "" for x in node.iter() if x.tag.rsplit("}", 1)[-1] == name), "")


def _control_lock(did):
    # A strong reference in each caller keeps one lock alive while requests or
    # the startup worker use it; idle renderers do not accumulate locks.
    with _control_locks_guard:
        lock = _control_locks.get(did)
        if lock is None:
            lock = threading.RLock()
            _control_locks[did] = lock
        return lock


def _cancel_pending(did):
    pending = _pending.pop(did, None)
    if pending:
        pending["cancel"].set()


def _description(location):
    response = _http.get(location, timeout=2)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    renderer = next((x for x in root.iter() if x.tag.rsplit("}", 1)[-1] == "device"
                     and _tag(x, "deviceType").startswith("urn:schemas-upnp-org:device:MediaRenderer:")), None)
    if renderer is None:
        return None
    services = [x for x in renderer.iter() if x.tag.rsplit("}", 1)[-1] == "service"]
    service = next((x for x in services if _tag(x, "serviceType").startswith(AV_TRANSPORT.rsplit(":", 1)[0] + ":")), None)
    if service is None or not _tag(service, "controlURL"):
        return None
    base = _tag(root, "URLBase") or location
    control = urllib.parse.urljoin(base, _tag(service, "controlURL"))
    if "/bilibili/" in control.lower():
        return None
    host = urllib.parse.urlsplit(location).hostname
    rendering = next((x for x in services if _tag(x, "serviceType").startswith(RENDERING_CONTROL.rsplit(":", 1)[0] + ":")), None)
    return {"id": hashlib.sha256(host.encode()).hexdigest()[:20], "host": host,
            "name": _tag(renderer, "friendlyName") or "DLNA 设备", "control": control,
            "transport_type": _tag(service, "serviceType"),
            "rendering": urllib.parse.urljoin(base, _tag(rendering, "controlURL")) if rendering is not None else "",
            "rendering_type": _tag(rendering, "serviceType") if rendering is not None else RENDERING_CONTROL,
            "location": location, "until": time.time() + 300}


def _direct_locations():
    configured = os.environ.get("CAST_DEVICE_URLS", "").split(",")
    host = os.environ.get("TV_ADB_HOST", "192.168.3.214")
    return {x.strip() for x in configured if x.strip()} | _known_locations | {f"http://{host}:49152/description.xml"}


def renderer_at(host):
    """Read-only probe of this home's known LeTV descriptor, also works without multicast."""
    for location in sorted(_direct_locations()):
        if urllib.parse.urlsplit(location).hostname != host:
            continue
        try:
            descriptor = _description(location)
            if descriptor:
                _devices[descriptor["id"]] = descriptor
                _known_locations.add(location)
                return descriptor
        except (requests.RequestException, ET.ParseError, OSError, ValueError):
            continue
    return None


def discover(timeout=2.0):
    """Return DLNA MediaRenderers found by SSDP, refreshing the five-minute cache."""
    query = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
             "MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    found, end = _direct_locations(), time.monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.settimeout(.2)
        sock.sendto(query, SSDP)
        while time.monotonic() < end:
            try:
                packet, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            headers = {}
            for line in packet.decode("utf-8", "ignore").split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            if headers.get("location"):
                found.add(headers["location"])
    except OSError:
        pass  # a configured descriptor still works when multicast is unavailable
    finally:
        sock.close()
    now, fresh = time.time(), {}
    for location in sorted(found):
        try:
            descriptor = _description(location)
            if descriptor:
                fresh.setdefault(descriptor["host"], descriptor)
                _known_locations.add(location)
        except (requests.RequestException, ET.ParseError, OSError, ValueError):
            continue
    for did in list(_devices):
        if _devices[did]["until"] <= now:
            _devices.pop(did, None)
    _devices.update({d["id"]: d for d in fresh.values()})
    return [{"id": d["id"], "name": d["name"]} for d in _devices.values() if d["until"] > now]


def _device(did):
    global _last_refresh
    d = _devices.get(did)
    if not d or d["until"] <= time.time():
        # Keep a selected TV usable across long playback / suspended browser tabs.
        # Only rediscovered LAN descriptors are accepted, never client-supplied URLs.
        with _refresh_lock:
            d = _devices.get(did)
            if (not d or d["until"] <= time.time()) and time.monotonic() - _last_refresh >= 10:
                _last_refresh = time.monotonic()
                discover()
            d = _devices.get(did)
        if not d or d["until"] <= time.time():
            raise ValueError("找不到投屏电视，请确认电视在线或重新搜索")
    return d


def _soap(did, action, service=AV_TRANSPORT, **args):
    d = _device(did)
    control = d["control"] if service == AV_TRANSPORT else d.get("rendering")
    if not control:
        raise ValueError(f"{d['name']} 不支持这个投屏控制")
    service = d.get("transport_type", AV_TRANSPORT) if service == AV_TRANSPORT else d.get("rendering_type", RENDERING_CONTROL)
    fields = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} '
            f'xmlns:u="{service}"><InstanceID>0</InstanceID>{fields}</u:{action}></s:Body></s:Envelope>')
    try:
        r = _http.post(control, data=body.encode(), timeout=8, headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
        if not r.ok:
            try:
                fault = ET.fromstring(r.content)
                detail = _tag(fault, "errorDescription") or _tag(fault, "errorCode")
            except ET.ParseError:
                detail = ""
            raise RuntimeError(f"电视返回 HTTP {r.status_code}" + (f"：{detail}" if detail else ""))
        d["until"] = time.time() + 300
        return r.content
    except requests.RequestException as e:
        raise RuntimeError(f"连不上 {d['name']}：{e}") from e


def start(did, url, title, thumb="", mime="video/mp4", position=0):
    position = float(position or 0)
    if not math.isfinite(position) or position < 0:
        raise ValueError("播放位置不对")
    meta = ("<DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" "
            "xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\">"
            "<item id=\"0\" parentID=\"0\" restricted=\"1\"><dc:title>" + html.escape(title) + "</dc:title>"
            + ("<upnp:class>object.item.audioItem.musicTrack</upnp:class>" if mime.startswith("audio/")
               else "<upnp:class>object.item.videoItem</upnp:class>")
            + (f"<upnp:albumArtURI>{html.escape(thumb)}</upnp:albumArtURI>" if thumb else "")
            + f"<res protocolInfo=\"http-get:*:{mime}:*\">{html.escape(url)}</res></item></DIDL-Lite>")
    with _control_lock(did):
        _cancel_pending(did)
        _start_errors.pop(did, None)
        _soap(did, "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=meta)
        _soap(did, "Play", Speed="1")
        # This LeTV takes 15–20 seconds to open its first stream. Complete the
        # delayed controls on the server even after the phone closes or locks.
        pending = {"url": url, "position": position, "paused": False,
                   "until": time.monotonic() + 60, "cancel": threading.Event()}
        _pending[did] = pending
        threading.Thread(target=_wait_for_start, args=(did, pending), daemon=True).start()
        return _device(did)["name"]


def control(did, action, position=None, volume=None):
    with _control_lock(did):
        _control(did, action, position, volume)


def _control(did, action, position=None, volume=None):
    pending = _pending.get(did)
    if pending and action in ("seek", "pause", "play"):
        if action == "seek":
            value = float(position or 0)
            if not math.isfinite(value):
                raise ValueError("播放位置不对")
            pending["position"] = max(0, value)
        else:
            pending["paused"] = action == "pause"
        return
    if action == "play":
        _soap(did, "Play", Speed="1")
    elif action == "pause":
        _soap(did, "Pause")
    elif action == "stop":
        _cancel_pending(did)
        _start_errors.pop(did, None)
        _soap(did, "Stop")
    elif action == "seek":
        seconds = max(0, int(float(position or 0)))
        h, seconds = divmod(seconds, 3600)
        m, s = divmod(seconds, 60)
        _soap(did, "Seek", Unit="REL_TIME", Target=f"{h:02}:{m:02}:{s:02}")
    elif action == "volume":
        _soap(did, "SetVolume", service=RENDERING_CONTROL, Channel="Master",
              DesiredVolume=max(0, min(100, int(float(volume if volume is not None else 0)))))
    else:
        raise ValueError("不认识这个投屏操作")


def _clock(value):
    try:
        h, m, s = (float(x) for x in value.split(":"))
        return int(h * 3600 + m * 60 + s)
    except (AttributeError, ValueError):
        return 0


def _transport_status(did):
    pos = ET.fromstring(_soap(did, "GetPositionInfo"))
    info = ET.fromstring(_soap(did, "GetTransportInfo"))
    out = {"position": _clock(_tag(pos, "RelTime")), "duration": _clock(_tag(pos, "TrackDuration")),
           "state": _tag(info, "CurrentTransportState"), "volume": None}
    return out, _tag(pos, "TrackURI")


def _advance_start(did, pending):
    """One bounded startup check. Only this worker applies the deferred controls."""
    with _control_lock(did):
        if _pending.get(did) is not pending:
            return False
        if time.monotonic() >= pending["until"]:
            _cancel_pending(did)
            _start_errors[did] = "电视已收到视频，但尚未开始播放，请检查电视画面后重试"
            return False
        try:
            out, track = _transport_status(did)
        except (RuntimeError, ValueError, ET.ParseError):
            return True  # Some TVs refuse status queries while opening a stream.
        if (out["duration"] > 0 and out["position"] >= 1 and out["state"] == "PLAYING"
                and (not track or track == pending["url"])):
            # Hold the renderer lock through the controls: a replacement or Stop
            # cannot slip between the matching-track check and an old Seek.
            _cancel_pending(did)
            try:
                if pending["position"] >= 1:
                    _control(did, "seek", min(pending["position"], max(0, out["duration"] - 1)))
                if pending["paused"]:
                    _control(did, "pause")
            except (RuntimeError, ValueError) as exc:
                _start_errors[did] = str(exc)
            return False
        return True


def _wait_for_start(did, pending):
    while _advance_start(did, pending):
        if pending["cancel"].wait(.5):
            break


def status(did):
    """Read the renderer's clock; deferred controls do not depend on this request."""
    with _control_lock(did):
        error = _start_errors.pop(did, None)
        if error:
            raise RuntimeError(error)
        out, _ = _transport_status(did)
        pending = _pending.get(did)
        if pending:
            out.update(state="TRANSITIONING", position=int(pending["position"]), duration=0)
        try:
            volume = ET.fromstring(_soap(did, "GetVolume", service=RENDERING_CONTROL, Channel="Master"))
            out["volume"] = max(0, min(100, int(_tag(volume, "CurrentVolume"))))
        except (RuntimeError, ValueError, ET.ParseError):
            pass  # Volume is optional in DLNA; playback controls still work.
        return out
