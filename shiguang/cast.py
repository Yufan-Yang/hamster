"""Small DLNA/UPnP control point used by the watch page.

The TV fetches the media URL from the Pi itself.  Discovery and SOAP controls
therefore belong on the Pi: browsers cannot send SSDP multicast packets.
"""
import hashlib
import html
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests


SSDP = ("239.255.255.250", 1900)
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
_devices = {}  # opaque id -> descriptor; a short cache also prevents forged control URLs


def _tag(node, name):
    return next((x.text or "" for x in node.iter() if x.tag.rsplit("}", 1)[-1] == name), "")


def discover(timeout=2.0):
    """Return DLNA MediaRenderers found by SSDP, refreshing the five-minute cache."""
    query = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
             "MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    found, end = set(), time.monotonic() + timeout
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
    finally:
        sock.close()
    now, fresh = time.time(), {}
    for location in found:
        try:
            root = ET.fromstring(requests.get(location, timeout=4).content)
            if _tag(root, "deviceType") != "urn:schemas-upnp-org:device:MediaRenderer:1":
                continue
            service = next((x for x in root.iter() if x.tag.rsplit("}", 1)[-1] == "service"
                            and _tag(x, "serviceType").startswith(AV_TRANSPORT)), None)
            control = _tag(service, "controlURL") if service is not None else ""
            if not control:
                continue
            control = urllib.parse.urljoin(location, control)
            # A Huawei screen advertises both its ordinary DLNA renderer and a
            # Bilibili-only AVTransport endpoint from the same address.  The
            # latter answers generic SetAVTransportURI with HTTP 500, so it
            # cannot play the Pi's local media and is deliberately omitted.
            if "/bilibili/" in control.lower():
                continue
            host = urllib.parse.urlsplit(location).hostname or location
            name = _tag(root, "friendlyName") or "DLNA 设备"
            did = hashlib.sha256(host.encode()).hexdigest()[:20]
            fresh[host] = {"id": did, "name": name, "control": control, "until": now + 300}
        except (requests.RequestException, ET.ParseError, OSError):
            continue
    _devices.clear()
    _devices.update({d["id"]: d for d in fresh.values()})
    return [{"id": d["id"], "name": d["name"]} for d in _devices.values() if d["until"] > now]


def _device(did):
    d = _devices.get(did)
    if not d or d["until"] <= time.time():
        raise ValueError("电视列表已过期，请重新搜索")
    return d


def _soap(did, action, **args):
    d = _device(did)
    fields = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args.items())
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} '
            f'xmlns:u="{AV_TRANSPORT}"><InstanceID>0</InstanceID>{fields}</u:{action}></s:Body></s:Envelope>')
    try:
        r = requests.post(d["control"], data=body.encode(), timeout=8, headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{AV_TRANSPORT}#{action}"'})
        if not r.ok:
            raise RuntimeError(f"电视返回 HTTP {r.status_code}")
        return r.content
    except requests.RequestException as e:
        raise RuntimeError(f"连不上 {d['name']}：{e}") from e


def start(did, url, title, thumb="", mime="video/mp4"):
    meta = ("<DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" "
            "xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\">"
            "<item id=\"0\" parentID=\"0\" restricted=\"1\"><dc:title>" + html.escape(title) + "</dc:title>"
            + (f"<upnp:albumArtURI>{html.escape(thumb)}</upnp:albumArtURI>" if thumb else "")
            + f"<res protocolInfo=\"http-get:*:{mime}:*\">{html.escape(url)}</res></item></DIDL-Lite>")
    _soap(did, "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=meta)
    _soap(did, "Play", Speed="1")
    return _device(did)["name"]


def control(did, action, position=None):
    if action == "play":
        _soap(did, "Play", Speed="1")
    elif action == "pause":
        _soap(did, "Pause")
    elif action == "stop":
        _soap(did, "Stop")
    elif action == "seek":
        seconds = max(0, int(float(position or 0)))
        h, seconds = divmod(seconds, 3600)
        m, s = divmod(seconds, 60)
        _soap(did, "Seek", Unit="REL_TIME", Target=f"{h:02}:{m:02}:{s:02}")
    else:
        raise ValueError("不认识这个投屏操作")


def _clock(value):
    try:
        h, m, s = (float(x) for x in value.split(":"))
        return int(h * 3600 + m * 60 + s)
    except (AttributeError, ValueError):
        return 0


def status(did):
    """The renderer's own clock, used for the seek bar rather than the phone's stopped player."""
    pos = ET.fromstring(_soap(did, "GetPositionInfo"))
    info = ET.fromstring(_soap(did, "GetTransportInfo"))
    return {"position": _clock(_tag(pos, "RelTime")), "duration": _clock(_tag(pos, "TrackDuration")),
            "state": _tag(info, "CurrentTransportState")}
