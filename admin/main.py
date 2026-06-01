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
FRIGATE_API         = "http://ring-frigate:5001"

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

# Default Frigate camera config template for a Ring camera
def ring_camera_config(camera_id: str) -> dict:
    user, password = _go2rtc_rtsp_credentials()
    return {
        "ffmpeg": {"inputs": [{
            "path": f"rtsp://{user}:{password}@ring-mqtt:8554/{camera_id}_live",
            "roles": ["detect", "record"],
        }]},
        "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
        "motion": {"threshold": 25, "contour_area": 100},
    }

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

    # Active cameras in Frigate
    for name, cfg in active.items():
        m = meta.get(name, {})
        result[name] = {
            "battery": m.get("battery", False),
            "active":  True,
            "config":  cfg,
            "camera_id": m.get("camera_id"),
        }

    # Inactive battery cameras (in meta but not in Frigate)
    for name, m in meta.items():
        if name not in result and m.get("battery"):
            result[name] = {
                "battery":   True,
                "active":    False,
                "config":    m.get("config", {}),
                "camera_id": m.get("camera_id"),
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
async def api_add_ring_camera(camera_id: str = Form(...), camera_name: str = Form(...)):
    """Add a Ring camera — marked as battery, stored in meta, added to Frigate."""
    cfg = ring_camera_config(camera_id)

    # Save to meta (battery=True, template for future toggling)
    meta = read_camera_meta()
    meta[camera_name] = {"battery": True, "active": True, "camera_id": camera_id, "config": cfg}
    write_camera_meta(meta)

    # Add to Frigate config
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
    data    = await request.json()
    enabled = bool(data.get("enabled", True))
    meta    = read_camera_meta()
    cam_meta = meta.get(camera, {})

    if cam_meta.get("battery"):
        # Battery camera: add/remove from Frigate config entirely
        fcfg = read_frigate_config()
        if enabled:
            # Restore from saved template
            cam_cfg = cam_meta.get("config")
            if not cam_cfg:
                return JSONResponse({"error": "no saved config template"}, status_code=400)
            fcfg.setdefault("cameras", {})[camera] = cam_cfg
        else:
            # Remove from Frigate — saves battery
            fcfg.get("cameras", {}).pop(camera, None)

        # Update active flag in meta
        cam_meta["active"] = enabled
        meta[camera] = cam_meta
        write_camera_meta(meta)
        write_frigate_config(fcfg)
        restart_container("frigate")
    else:
        # Wired camera: toggle detect + record via Frigate API (no restart needed)
        async with httpx.AsyncClient() as client:
            await client.post(f"{FRIGATE_API}/api/{camera}/detect",
                              json={"enabled": enabled}, timeout=5)
            await client.post(f"{FRIGATE_API}/api/{camera}/recordings",
                              json={"enabled": enabled}, timeout=5)

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
    record.setdefault("alerts", {})["retain"]     = {"days": int(data.get("alerts_days", 30))}
    record.setdefault("detections", {})["retain"]  = {"days": int(data.get("detections_days", 14))}
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


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


# ── API: motion trigger settings ─────────────────────────────────────────────

@app.get("/api/motion-settings")
async def api_motion_settings():
    meta = read_camera_meta()
    result = {}
    for name, m in meta.items():
        if m.get("battery"):
            result[name] = {
                "motion_trigger": m.get("motion_trigger", True),
                "record_seconds": m.get("record_seconds", 60),
            }
    return result

@app.post("/api/motion-settings")
async def api_save_motion_settings(request: Request):
    data = await request.json()
    meta = read_camera_meta()
    for cam_name, settings in data.items():
        if cam_name in meta and meta[cam_name].get("battery"):
            meta[cam_name]["motion_trigger"]  = bool(settings.get("motion_trigger", True))
            meta[cam_name]["record_seconds"]  = int(settings.get("record_seconds", 60))
    write_camera_meta(meta)
    return {"ok": True}


# ── MQTT motion trigger ───────────────────────────────────────────────────────

logger = logging.getLogger("motion_trigger")
logging.basicConfig(level=logging.INFO)

# Per-camera timer: camera_name -> threading.Timer
_motion_timers: dict[str, threading.Timer] = {}
_timer_lock = threading.Lock()

MQTT_HOST   = "mosquitto"
MQTT_PORT   = 1883
RING_TOPIC  = "ring"  # base topic from ring-mqtt config


def _camera_name_for_id(camera_id: str) -> str | None:
    """Find the camera name in meta that matches the given Ring camera_id."""
    for name, m in read_camera_meta().items():
        if m.get("battery") and m.get("camera_id") == camera_id:
            return name
    return None


def _enable_battery_camera(cam_name: str):
    """Add battery camera to Frigate and restart."""
    meta = read_camera_meta()
    m    = meta.get(cam_name, {})
    if not m.get("battery") or m.get("active"):
        return
    cfg  = m.get("config")
    if not cfg:
        return
    fcfg = read_frigate_config()
    fcfg.setdefault("cameras", {})[cam_name] = cfg
    m["active"] = True
    meta[cam_name] = m
    write_camera_meta(meta)
    write_frigate_config(fcfg)
    try:
        restart_container("frigate")
        logger.info("Motion trigger: enabled %s in Frigate", cam_name)
    except Exception as e:
        logger.warning("Motion trigger: could not restart Frigate: %s", e)


def _disable_battery_camera(cam_name: str):
    """Remove battery camera from Frigate and restart."""
    meta = read_camera_meta()
    m    = meta.get(cam_name, {})
    if not m.get("battery") or not m.get("active"):
        return
    fcfg = read_frigate_config()
    fcfg.get("cameras", {}).pop(cam_name, None)
    m["active"] = False
    meta[cam_name] = m
    write_camera_meta(meta)
    write_frigate_config(fcfg)
    try:
        restart_container("frigate")
        logger.info("Motion trigger: disabled %s in Frigate", cam_name)
    except Exception as e:
        logger.warning("Motion trigger: could not restart Frigate: %s", e)


def _schedule_disable(cam_name: str, seconds: int):
    """Cancel any existing timer and schedule a new disable."""
    with _timer_lock:
        old = _motion_timers.get(cam_name)
        if old:
            old.cancel()
        t = threading.Timer(seconds, _disable_battery_camera, args=[cam_name])
        t.daemon = True
        t.start()
        _motion_timers[cam_name] = t


def _on_mqtt_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()

    # ring/<location>/<...>/camera/<camera_id>/motion/state
    m = re.search(r"/camera/([^/]+)/motion/state$", topic)
    if m:
        camera_id = m.group(1)
        cam_name  = _camera_name_for_id(camera_id)
        if not cam_name:
            return
        meta     = read_camera_meta()
        cam_meta = meta.get(cam_name, {})
        if not cam_meta.get("motion_trigger", True):
            return
        record_seconds = cam_meta.get("record_seconds", 60)
        if payload.upper() == "ON":
            logger.info("Motion ON for %s — enabling stream", cam_name)
            _enable_battery_camera(cam_name)
            _schedule_disable(cam_name, record_seconds)
        elif payload.upper() == "OFF":
            logger.info("Motion OFF for %s — will disable in %ds", cam_name, record_seconds)
            _schedule_disable(cam_name, record_seconds)
        return

    # ring/<location>/<...>/camera/<camera_id>/info/state  {"batteryLevel": N, ...}
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
