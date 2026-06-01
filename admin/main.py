import json
import logging
import re
import threading
import time
from pathlib import Path

import docker
import httpx
import paho.mqtt.client as mqtt
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="/app/templates")

RING_CONFIG_PATH    = Path("/ring-mqtt-data/config.json")
GO2RTC_PATH         = Path("/ring-mqtt-data/go2rtc.yaml")
FRIGATE_CONFIG_PATH = Path("/frigate-config/config.yaml")
CAMERA_META_PATH    = Path("/frigate-config/camera_meta.json")
FRIGATE_API         = "http://ring-frigate:5000"

SERVICES = {
    "mosquitto": "ring-rtsp-mosquitto",
    "ring-mqtt": "ring-rtsp-bridge",
    "frigate":   "ring-frigate",
}

ALL_OBJECTS = ["person", "car", "dog", "cat", "bicycle", "motorcycle", "truck", "bird"]

def _go2rtc_rtsp_credentials() -> tuple[str, str]:
    """Read RTSP username/password from go2rtc.yaml."""
    try:
        cfg = yaml.safe_load(GO2RTC_PATH.read_text()) or {}
        rtsp = cfg.get("rtsp", {})
        return rtsp.get("username", "stream_user"), rtsp.get("password", "")
    except Exception:
        return "stream_user", ""

# Frigate config template for a Ring camera.
# battery=True  → _live stream added/removed on motion events (saves battery between events)
# battery=False → _live stream always present (wired/transformer-powered cameras)
def ring_camera_config(camera_id: str, battery: bool = True) -> dict:
    user, password = _go2rtc_rtsp_credentials()
    cfg = {
        "ffmpeg": {"inputs": [{
            "path": f"rtsp://{user}:{password}@ring-mqtt:8554/{camera_id}_live",
            "roles": ["detect", "record"],
        }]},
        "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
        "motion": {"threshold": 25, "contour_area": 100},
    }
    if battery:
        cfg["enabled"] = False  # disabled by default; toggled on motion
    return cfg

def rtsp_camera_config(rtsp_url: str) -> dict:
    return {
        "ffmpeg": {"inputs": [{"path": rtsp_url, "roles": ["detect", "record"]}]},
        "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
        "motion": {"threshold": 25, "contour_area": 100},
    }


# ── file helpers ──────────────────────────────────────────────────────────────

def docker_client():
    return docker.from_env()

def container_status(name: str) -> dict:
    try:
        c = docker_client().containers.get(name)
        return {"status": c.status, "name": name}
    except Exception:
        return {"status": "missing", "name": name}

def read_ring_config() -> dict:
    try:
        return json.loads(RING_CONFIG_PATH.read_text())
    except Exception:
        return {}

def write_ring_config(data: dict):
    RING_CONFIG_PATH.write_text(json.dumps(data, indent=2))

def read_frigate_config() -> dict:
    try:
        return yaml.safe_load(FRIGATE_CONFIG_PATH.read_text()) or {}
    except Exception:
        return {}

def write_frigate_config(data: dict):
    FRIGATE_CONFIG_PATH.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True)
    )

def read_camera_meta() -> dict:
    """
    {cam_name: {battery: bool, active: bool, camera_id?: str, config: dict}}
    Stored in the shared frigate-config volume alongside config.yaml.
    """
    try:
        return json.loads(CAMERA_META_PATH.read_text())
    except Exception:
        return {}

def write_camera_meta(data: dict):
    CAMERA_META_PATH.write_text(json.dumps(data, indent=2))

def discovered_cameras() -> list[str]:
    try:
        cfg = yaml.safe_load(GO2RTC_PATH.read_text()) or {}
        ids: set[str] = set()
        for key in cfg.get("streams", {}):
            m = re.match(r"^(.+?)_(live|event)$", key)
            if m:
                ids.add(m.group(1))
        return sorted(ids)
    except Exception:
        return []

def frigate_stats() -> dict:
    try:
        r = httpx.get(f"{FRIGATE_API}/api/stats", timeout=3)
        return r.json()
    except Exception:
        return {}

def container_logs(service_key: str, lines: int = 60) -> str:
    name = SERVICES.get(service_key, "")
    try:
        c = docker_client().containers.get(name)
        raw = c.logs(tail=lines, timestamps=False).decode("utf-8", errors="replace")
        return re.sub(r"\x1b\[[0-9;]*m", "", raw)
    except Exception as e:
        return f"Could not fetch logs: {e}"

def restart_container(service_key: str):
    name = SERVICES.get(service_key, "")
    docker_client().containers.get(name).restart()


# ── camera helpers ────────────────────────────────────────────────────────────

def all_known_cameras() -> dict:
    """
    Returns all cameras: active ones from Frigate config + inactive battery cameras from meta.
    {name: {battery, active, config}}
    """
    fcfg   = read_frigate_config()
    meta   = read_camera_meta()
    active = fcfg.get("cameras", {})
    result = {}

    # Cameras in Frigate config (enabled or disabled)
    for name, cfg in active.items():
        m = meta.get(name, {})
        # battery cameras: active = camera-level enabled flag in Frigate config
        # wired cameras:   active = True (always on)
        is_battery = m.get("battery", False)
        if is_battery:
            is_active = cfg.get("enabled", True)  # default True if not set
        else:
            is_active = True
        result[name] = {
            "battery":        is_battery,
            "active":         is_active,
            "config":         cfg,
            "camera_id":      m.get("camera_id"),
            "battery_level":  m.get("battery_level"),
            "last_motion":    m.get("last_motion"),
            "events":         m.get("events", {}),
            "record_seconds": m.get("record_seconds", 60),
        }

    # Inactive battery cameras (in meta but not in Frigate)
    for name, m in meta.items():
        if name not in result and m.get("battery"):
            result[name] = {
                "battery":        True,
                "active":         False,
                "config":         m.get("config", {}),
                "camera_id":      m.get("camera_id"),
                "battery_level":  m.get("battery_level"),
                "last_motion":    m.get("last_motion"),
                "events":         m.get("events", {}),
                "record_seconds": m.get("record_seconds", 60),
            }

    return result


# ── pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    fcfg = read_frigate_config()
    cameras = all_known_cameras()
    if not cameras:
        return RedirectResponse("/setup", status_code=302)
    statuses = {k: container_status(v) for k, v in SERVICES.items()}
    stats    = frigate_stats()
    ring_cfg = read_ring_config()
    disc     = discovered_cameras()
    return templates.TemplateResponse(request, "index.html", {
        "statuses":   statuses,
        "stats":      stats,
        "fcfg":       fcfg,
        "cameras":    cameras,
        "ring_cfg":   ring_cfg,
        "disc":       disc,
        "all_objects": ALL_OBJECTS,
    })

@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    ring_cfg = read_ring_config()
    disc     = discovered_cameras()
    return templates.TemplateResponse(request, "setup.html", {
        "ring_cfg": ring_cfg,
        "disc":     disc,
    })


# ── API: status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    statuses = {k: container_status(v) for k, v in SERVICES.items()}
    stats    = frigate_stats()
    cameras  = all_known_cameras()
    return {"services": statuses, "frigate_stats": stats, "cameras": cameras}


# ── API: ring connection ──────────────────────────────────────────────────────

@app.get("/api/ring-status")
async def api_ring_status():
    svc = container_status(SERVICES["ring-mqtt"])
    if svc["status"] != "running":
        return {"connected": False, "cameras": 0, "status": svc["status"]}
    cams = discovered_cameras()
    connected = len(cams) > 0
    if not connected:
        try:
            c = docker_client().containers.get(SERVICES["ring-mqtt"])
            logs = c.logs(tail=50).decode("utf-8", errors="replace")
            connected = "Successfully established connection to Ring API" in logs
        except Exception:
            pass
    return {"connected": connected, "cameras": len(cams), "status": svc["status"]}


# ── API: restart ──────────────────────────────────────────────────────────────

@app.post("/api/restart/{service}")
async def api_restart(service: str):
    if service not in SERVICES:
        return JSONResponse({"error": "unknown service"}, status_code=400)
    try:
        restart_container(service)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: logs ─────────────────────────────────────────────────────────────────

@app.get("/api/logs/{service}")
async def api_logs(service: str, lines: int = 60):
    return {"logs": container_logs(service, lines)}


# ── API: cameras ──────────────────────────────────────────────────────────────

@app.get("/api/cameras")
async def api_cameras():
    return {
        "discovered": discovered_cameras(),
        "active":     list(read_frigate_config().get("cameras", {}).keys()),
        "all":        all_known_cameras(),
    }


@app.post("/api/cameras/add")
async def api_add_ring_camera(
    camera_id: str = Form(...),
    camera_name: str = Form(...),
    wired: str = Form("false"),
):
    """Add a Ring camera. wired=true → _live stream (powered); default → _event (battery)."""
    is_battery = wired.lower() not in ("true", "1", "yes")
    cfg = ring_camera_config(camera_id, battery=is_battery)
    meta = read_camera_meta()
    meta[camera_name] = {"battery": is_battery, "active": True, "camera_id": camera_id, "config": cfg}
    write_camera_meta(meta)
    fcfg = read_frigate_config()
    fcfg.setdefault("cameras", {})[camera_name] = cfg
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/add-rtsp")
async def api_add_rtsp_camera(request: Request):
    """Add a wired IP camera by RTSP URL."""
    data = await request.json()
    name     = data.get("name", "").strip().replace(" ", "_")
    rtsp_url = data.get("rtsp_url", "").strip()
    if not name or not rtsp_url:
        return JSONResponse({"error": "name and rtsp_url required"}, status_code=400)

    cfg = rtsp_camera_config(rtsp_url)

    # Save to meta (battery=False)
    meta = read_camera_meta()
    meta[name] = {"battery": False, "active": True, "config": cfg}
    write_camera_meta(meta)

    # Add to Frigate config
    fcfg = read_frigate_config()
    if name in fcfg.get("cameras", {}):
        return JSONResponse({"error": f"Camera '{name}' already exists"}, status_code=400)
    fcfg.setdefault("cameras", {})[name] = cfg
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/rename")
async def api_rename_camera(request: Request):
    data     = await request.json()
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip().replace(" ", "_")
    if not old_name or not new_name:
        return JSONResponse({"error": "old_name and new_name required"}, status_code=400)

    # Update meta
    meta = read_camera_meta()
    if old_name in meta:
        meta[new_name] = meta.pop(old_name)
        write_camera_meta(meta)

    # Update Frigate config (only if camera is currently active)
    fcfg    = read_frigate_config()
    cameras = fcfg.get("cameras", {})
    if old_name not in cameras:
        return JSONResponse({"error": f"Camera '{old_name}' not found in Frigate"}, status_code=404)
    if new_name in cameras:
        return JSONResponse({"error": f"Camera '{new_name}' already exists"}, status_code=400)
    cameras[new_name] = cameras.pop(old_name)
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/remove")
async def api_remove_camera(camera_name: str = Form(...)):
    # Remove from meta entirely
    meta = read_camera_meta()
    meta.pop(camera_name, None)
    write_camera_meta(meta)

    # Remove from Frigate config
    fcfg = read_frigate_config()
    fcfg.get("cameras", {}).pop(camera_name, None)
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: live toggle ──────────────────────────────────────────────────────────

@app.post("/api/camera/{camera}/live")
async def api_camera_live(camera: str, request: Request):
    data     = await request.json()
    enabled  = bool(data.get("enabled", True))
    meta     = read_camera_meta()
    cam_meta = meta.get(camera, {})

    # Cancel any pending auto-disable timer
    if not enabled:
        with _timer_lock:
            t = _motion_timers.pop(camera, None)
            if t:
                t.cancel()

    # Toggle detect + record via Frigate MQTT commands — no restart needed
    _frigate_set_camera_enabled(camera, enabled)

    return {"ok": True, "camera": camera, "enabled": enabled}


# ── API: detection ────────────────────────────────────────────────────────────

@app.post("/api/detection")
async def api_save_detection(request: Request):
    data           = await request.json()
    global_objects = data.get("global_objects", [])
    per_camera     = data.get("per_camera", {})

    fcfg = read_frigate_config()
    fcfg["objects"] = {"track": global_objects}

    for cam_name, cam_cfg in fcfg.get("cameras", {}).items():
        overrides = per_camera.get(cam_name)
        if overrides is not None and sorted(overrides) != sorted(global_objects):
            cam_cfg.setdefault("objects", {})["track"] = overrides
        else:
            cam_cfg.pop("objects", None)

    # Also update templates in meta so they stay in sync
    meta = read_camera_meta()
    for cam_name, m in meta.items():
        if cam_name not in fcfg.get("cameras", {}):
            continue
        overrides = per_camera.get(cam_name)
        cfg = m.get("config", {})
        if overrides is not None and sorted(overrides) != sorted(global_objects):
            cfg.setdefault("objects", {})["track"] = overrides
        else:
            cfg.pop("objects", None)
    write_camera_meta(meta)

    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: retention ────────────────────────────────────────────────────────────

@app.post("/api/retention")
async def api_save_retention(request: Request):
    data = await request.json()
    fcfg = read_frigate_config()
    record = fcfg.setdefault("record", {})
    record.pop("retain", None)  # not supported in Frigate 0.17
    record.setdefault("alerts", {})["retain"]      = {"days": int(data.get("alerts_days", 30))}
    record.setdefault("detections", {})["retain"]  = {"days": int(data.get("detections_days", 14))}
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: camera settings ─────────────────────────────────────────────────────

@app.post("/api/camera/{camera}/record-seconds")
async def api_set_record_seconds(camera: str, request: Request):
    data = await request.json()
    seconds = max(30, min(600, int(data.get("seconds", 120))))
    meta = read_camera_meta()
    if camera not in meta:
        return JSONResponse({"error": "camera not found"}, status_code=404)
    meta[camera]["record_seconds"] = seconds
    write_camera_meta(meta)
    return {"ok": True, "camera": camera, "record_seconds": seconds}


# ── API: credentials ──────────────────────────────────────────────────────────

@app.post("/api/credentials")
async def api_save_credentials(request: Request):
    data     = await request.json()
    ring_cfg = read_ring_config()
    ring_cfg["livestream_user"] = data.get("user", ring_cfg.get("livestream_user", ""))
    ring_cfg["livestream_pass"] = data.get("pass", ring_cfg.get("livestream_pass", ""))
    write_ring_config(ring_cfg)
    restart_container("ring-mqtt")
    return {"ok": True, "note": "ring-mqtt restarted. Update RING_RTSP_PASS in .env and restart Frigate."}


# ── API: storage cleanup ─────────────────────────────────────────────────────

FRIGATE_DB_PATH = Path("/config/frigate.db")
FRIGATE_CLIPS_PATH = Path("/media/frigate/clips")

@app.get("/api/cleanup/preview")
async def api_cleanup_preview():
    """Return count of events that have no video clip on disk."""
    import sqlite3, glob as _glob
    try:
        conn = sqlite3.connect(str(FRIGATE_DB_PATH))
        c = conn.cursor()
        c.execute("SELECT camera, COUNT(*) FROM event WHERE has_clip=1 GROUP BY camera")
        rows = c.fetchall()
        conn.close()
        result = {}
        for camera, total in rows:
            orphaned = sum(
                1 for row_id in _get_clip_ids(camera)
                if not (FRIGATE_CLIPS_PATH / f"{row_id}.mp4").exists()
            )
            if orphaned:
                result[camera] = orphaned
        return {"orphaned": result}
    except Exception as e:
        return {"error": str(e)}

def _get_clip_ids(camera: str) -> list[str]:
    import sqlite3
    try:
        conn = sqlite3.connect(str(FRIGATE_DB_PATH))
        c = conn.cursor()
        c.execute("SELECT id FROM event WHERE camera=? AND has_clip=1", (camera,))
        ids = [r[0] for r in c.fetchall()]
        conn.close()
        return ids
    except Exception:
        return []

@app.post("/api/cleanup/orphaned")
async def api_cleanup_orphaned(request: Request):
    """Delete snapshot files for events that have no video clip."""
    import sqlite3, glob as _glob
    data   = await request.json()
    camera = data.get("camera")  # optional: clean only this camera
    try:
        conn = sqlite3.connect(str(FRIGATE_DB_PATH))
        c    = conn.cursor()
        query = "SELECT id FROM event WHERE has_clip=1"
        params: tuple = ()
        if camera:
            query += " AND camera=?"
            params = (camera,)
        c.execute(query, params)
        event_ids = [r[0] for r in c.fetchall()]

        deleted_files = 0
        cleaned_events = 0
        for eid in event_ids:
            if not (FRIGATE_CLIPS_PATH / f"{eid}.mp4").exists():
                for f in _glob.glob(str(FRIGATE_CLIPS_PATH / f"{eid}*")):
                    Path(f).unlink(missing_ok=True)
                    deleted_files += 1
                c.execute("UPDATE event SET has_clip=0, has_snapshot=0 WHERE id=?", (eid,))
                cleaned_events += 1
        conn.commit()
        conn.close()
        return {"ok": True, "cleaned_events": cleaned_events, "deleted_files": deleted_files}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── MQTT listener ─────────────────────────────────────────────────────────────
# Battery cameras stay IN Frigate config permanently (removing them causes
# Frigate to delete their recordings). We toggle camera-level `enabled` flag:
#
#   motion ON  → set enabled:true  in Frigate config → restart Frigate → records
#   motion OFF → set enabled:false in Frigate config → restart Frigate → Ring sleeps
#
# `enabled:false` stops the ffmpeg process entirely (no RTSP connection = no
# battery drain) while keeping the camera in config so recordings are preserved.

logger = logging.getLogger("ring_admin")
logging.basicConfig(level=logging.INFO)

MQTT_HOST  = "mosquitto"
MQTT_PORT  = 1883
RING_TOPIC = "ring"

_motion_timers: dict[str, threading.Timer] = {}
_timer_lock = threading.Lock()


def _camera_name_for_id(camera_id: str) -> str | None:
    for name, m in read_camera_meta().items():
        if m.get("battery") and m.get("camera_id") == camera_id:
            return name
    return None


def _frigate_set_camera_enabled(cam_name: str, enabled: bool):
    """Set camera-level enabled flag in Frigate config and restart.

    enabled=True  → Frigate connects to RTSP, records
    enabled=False → Frigate stops the camera process entirely (no RTSP = no battery drain)
    Recordings are preserved because the camera stays in config.
    """
    try:
        fcfg = read_frigate_config()
        cam  = fcfg.get("cameras", {}).get(cam_name)
        if cam is None:
            logger.warning("_frigate_set_camera_enabled: %s not in Frigate config", cam_name)
            return
        cam["enabled"] = enabled
        write_frigate_config(fcfg)

        meta = read_camera_meta()
        if cam_name in meta:
            meta[cam_name]["active"] = enabled
            write_camera_meta(meta)

        restart_container("frigate")
        logger.info("Camera %s %s (Frigate restarted)", cam_name, "enabled" if enabled else "disabled")
    except Exception as e:
        logger.warning("Could not set camera enabled for %s: %s", cam_name, e)


def _schedule_disable(cam_name: str, seconds: int):
    with _timer_lock:
        old = _motion_timers.get(cam_name)
        if old:
            old.cancel()
        t = threading.Timer(seconds, _frigate_set_camera_enabled, args=[cam_name, False])
        t.daemon = True
        t.start()
        _motion_timers[cam_name] = t


def _record_motion_event(cam_name: str):
    """Persist motion event timestamp and increment today's counter."""
    import datetime
    meta = read_camera_meta()
    if cam_name not in meta:
        return
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    events = meta[cam_name].get("events", {})
    meta[cam_name]["last_motion"] = now.isoformat(timespec="seconds")
    if events.get("date") == today:
        events["count_today"] = events.get("count_today", 0) + 1
    else:
        events = {"date": today, "count_today": 1}
    meta[cam_name]["events"] = events
    write_camera_meta(meta)


def _on_mqtt_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()

    # motion/state  OR  ding/state — both trigger recording
    m = re.search(r"/camera/([^/]+)/(motion|ding)/state$", topic)
    if m:
        camera_id  = m.group(1)
        event_type = m.group(2)  # "motion" or "ding"
        cam_name   = _camera_name_for_id(camera_id)
        if not cam_name:
            return
        meta     = read_camera_meta()
        cam_meta = meta.get(cam_name, {})
        record_seconds = cam_meta.get("record_seconds", 60)
        if payload.upper() == "ON":
            logger.info("%s ON — %s, recording for %ds", event_type.capitalize(), cam_name, record_seconds)
            _record_motion_event(cam_name)
            # Only restart Frigate if camera is currently disabled — avoid unnecessary restarts
            meta2 = read_camera_meta()
            if not meta2.get(cam_name, {}).get("active"):
                _frigate_set_camera_enabled(cam_name, True)
            _schedule_disable(cam_name, record_seconds)
        elif payload.upper() == "OFF" and event_type == "motion":
            logger.info("Motion OFF — %s, stopping in %ds", cam_name, record_seconds)
            _schedule_disable(cam_name, record_seconds)
        return

    # info/state → battery level
    m = re.search(r"/camera/([^/]+)/info/state$", topic)
    if m:
        camera_id = m.group(1)
        cam_name  = _camera_name_for_id(camera_id)
        if not cam_name:
            return
        try:
            data  = json.loads(payload)
            level = int(data.get("batteryLevel", data.get("batteryLife", -1)))
        except (ValueError, KeyError, TypeError):
            return
        if level < 0:
            return
        meta = read_camera_meta()
        if cam_name in meta and meta[cam_name].get("battery_level") != level:
            meta[cam_name]["battery_level"] = level
            write_camera_meta(meta)
            logger.info("Battery level for %s: %d%%", cam_name, level)


def _start_mqtt_listener():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = _on_mqtt_message

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe(f"{RING_TOPIC}/#")
            logger.info("MQTT motion trigger connected, subscribed to %s/#", RING_TOPIC)
        else:
            logger.warning("MQTT motion trigger connect failed rc=%d", rc)

    client.on_connect = on_connect

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            logger.warning("MQTT motion trigger error: %s — retrying in 10s", e)
            time.sleep(10)


# Start MQTT listener in background thread at startup
_mqtt_thread = threading.Thread(target=_start_mqtt_listener, daemon=True)
_mqtt_thread.start()
