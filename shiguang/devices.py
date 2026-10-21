"""Home TV status routes. ADB is restricted to one server-configured device."""
from flask import Response, g, jsonify, request, send_from_directory

from .core import HERE, app, kv_get, kv_set
import time
import json

from . import cast, television


def _home_only():
    if g.external:
        return jsonify(error="设备管理只能在家庭网络中使用"), 403


def _admin_only():
    denied = _home_only()
    if denied:
        return denied
    if not g.admin:
        return jsonify(error="请先在拾光首页登录管理员账号"), 403


@app.get("/devices")
def device_page():
    denied = _home_only()
    return denied if denied else send_from_directory(HERE / "static", "devices.html")


@app.get("/api/tv/status")
def tv_status():
    denied = _home_only()
    if denied:
        return denied
    result = television.status(force=request.args.get("refresh") == "1")
    descriptor = cast.renderer_at(television.HOST) if result.get("adb") else None
    result["projection"] = {"available": bool(descriptor)}
    if descriptor:
        result["projection"].update(id=descriptor["id"], name=descriptor["name"])
        try:
            result["projection"].update(cast.status(descriptor["id"]))
        except (ValueError, RuntimeError) as exc:
            result["projection"]["error"] = str(exc)
    result["admin"] = bool(g.admin)
    if result.get("ads"):
        result["ads"]["can_restore"] = bool(kv_get("tv_ad_backup:" + television.HOST))
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/tv/screenshot")
def tv_screenshot():
    denied = _admin_only()
    if denied:
        return denied
    try:
        return Response(television.screenshot(), mimetype="image/png", headers={"Cache-Control": "no-store"})
    except (OSError, ValueError, television.TVError) as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/tv/receiver/start")
def tv_receiver_start():
    denied = _admin_only()
    if denied:
        return denied
    try:
        television.restore_receiver()
        end = time.monotonic() + 45
        while time.monotonic() < end:
            descriptor = cast.renderer_at(television.HOST)
            if descriptor:
                return jsonify(ok=True, name=descriptor["name"])
            time.sleep(.5)
        return jsonify(error="已开启电视投屏开关，接收服务仍未就绪，请稍后刷新"), 502
    except (OSError, ValueError, television.TVError) as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/tv/ads")
def tv_ads():
    denied = _admin_only()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or data.get("action") not in ("block", "restore"):
        return jsonify(error="广告操作需要是屏蔽或恢复"), 400
    key = "tv_ad_backup:" + television.HOST

    def load_backup():
        raw = kv_get(key)
        return json.loads(raw) if raw else None

    def save_backup(value):
        kv_set(key, json.dumps(value, ensure_ascii=False) if value is not None else "")

    try:
        result = television.change_ads(data["action"], load_backup, save_backup)
        kv_set("tv_ad_last_action:" + television.HOST,
               json.dumps({"action": data["action"], "user": g.user, "at": time.time(), "changed": result["changed"]}))
        return jsonify(ok=True, ads=result["ads"], changed=result["changed"])
    except television.TVPermissionError as exc:
        return jsonify(error=str(exc)), 403
    except (OSError, ValueError, television.TVError) as exc:
        return jsonify(error=str(exc)), 502
