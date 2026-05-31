import json
import os
import re
from pathlib import Path
from typing import Any

import docker
import httpx
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="/app/templates")

RING_CONFIG_PATH = Path("/ring-mqtt-data/config.json")
GO2RTC_PATH = Path("/ring-mqtt-data/go2rtc.yaml")
FRIGATE_CONFIG_PATH = Path("/frigate-config/config.yaml")
FRIGATE_API = "http://ring-frigate:5001"

SERVICES = {
    "mosquitto": "ring-rtsp-mosquitto",
    "ring-mqtt": "ring-rtsp-bridge",
    "frigate":   "ring-frigate",
}

ALL_OBJECTS = ["person", "car", "dog", "cat", "bicycle", "motorcycle", "truck", "bird"]


# ── helpers ───────────────────────────────────────────────────────────────────

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
    FRIGATE_CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


def discovered_cameras() -> list[str]:
    """Return camera IDs found in ring-mqtt's go2rtc.yaml."""
    try:
        cfg = yaml.safe_load(GO2RTC_PATH.read_text()) or {}
        streams = cfg.get("streams", {})
        ids: set[str] = set()
        for key in streams:
            m = re.match(r"^(.+?)_(live|event)$", key)
            if m:
                ids.add(m.group(1))
        return sorted(ids)
    except Exception:
        return []


def frigate_cameras(fcfg: dict) -> dict:
    return fcfg.get("cameras", {})


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
        # strip ANSI colour codes
        return re.sub(r"\x1b\[[0-9;]*m", "", raw)
    except Exception as e:
        return f"Could not fetch logs: {e}"


def restart_container(service_key: str):
    name = SERVICES.get(service_key, "")
    c = docker_client().containers.get(name)
    c.restart()


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    statuses = {k: container_status(v) for k, v in SERVICES.items()}
    stats = frigate_stats()
    fcfg = read_frigate_config()
    ring_cfg = read_ring_config()
    disc = discovered_cameras()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "statuses": statuses,
        "stats": stats,
        "fcfg": fcfg,
        "ring_cfg": ring_cfg,
        "disc": disc,
        "all_objects": ALL_OBJECTS,
    })


# ── API: status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    statuses = {k: container_status(v) for k, v in SERVICES.items()}
    stats = frigate_stats()
    return {"services": statuses, "frigate_stats": stats}


# ── API: camera live toggle ───────────────────────────────────────────────────

def frigate_camera_state(camera: str) -> dict:
    """Return {detect: bool, record: bool} for a camera from Frigate stats."""
    try:
        r = httpx.get(f"{FRIGATE_API}/api/config", timeout=3)
        cfg = r.json()
        cam_cfg = cfg.get("cameras", {}).get(camera, {})
        return {
            "detect": cam_cfg.get("detect", {}).get("enabled", True),
            "record": cam_cfg.get("record", {}).get("enabled", True),
        }
    except Exception:
        return {"detect": True, "record": True}


@app.get("/api/camera/{camera}/state")
async def api_camera_state(camera: str):
    return frigate_camera_state(camera)


@app.post("/api/camera/{camera}/live")
async def api_camera_live(camera: str, request: Request):
    data = await request.json()
    enabled = bool(data.get("enabled", True))
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FRIGATE_API}/api/{camera}/detect",
            json={"enabled": enabled},
            timeout=5,
        )
        await client.post(
            f"{FRIGATE_API}/api/{camera}/recordings",
            json={"enabled": enabled},
            timeout=5,
        )
    return {"ok": True, "camera": camera, "enabled": enabled}


# ── API: restart ──────────────────────────────────────────────────────────────

@app.post("/api/restart/{service}")
async def api_restart(service: str):
    if service not in SERVICES:
        return JSONResponse({"error": "unknown service"}, status_code=400)
    try:
        restart_container(service)
        return {"ok": True, "service": service}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: logs ─────────────────────────────────────────────────────────────────

@app.get("/api/logs/{service}")
async def api_logs(service: str, lines: int = 60):
    return {"logs": container_logs(service, lines)}


# ── API: cameras ──────────────────────────────────────────────────────────────

@app.get("/api/cameras")
async def api_cameras():
    fcfg = read_frigate_config()
    return {
        "discovered": discovered_cameras(),
        "active": list(frigate_cameras(fcfg).keys()),
    }


@app.post("/api/cameras/add")
async def api_add_camera(camera_id: str = Form(...), camera_name: str = Form(...)):
    fcfg = read_frigate_config()
    cameras = fcfg.setdefault("cameras", {})
    if camera_name not in cameras:
        cameras[camera_name] = {
            "ffmpeg": {
                "inputs": [{
                    "path": f"rtsp://{{FRIGATE_RTSP_USER}}:{{FRIGATE_RTSP_PASSWORD}}@ring-mqtt:8554/{camera_id}_live",
                    "roles": ["detect", "record"],
                }]
            },
            "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
            "motion": {"threshold": 25, "contour_area": 100},
        }
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/add-rtsp")
async def api_add_rtsp_camera(request: Request):
    data = await request.json()
    name = data.get("name", "").strip().replace(" ", "_")
    rtsp_url = data.get("rtsp_url", "").strip()
    if not name or not rtsp_url:
        return JSONResponse({"error": "name and rtsp_url required"}, status_code=400)
    fcfg = read_frigate_config()
    cameras = fcfg.setdefault("cameras", {})
    if name in cameras:
        return JSONResponse({"error": f"Camera '{name}' already exists"}, status_code=400)
    cameras[name] = {
        "ffmpeg": {
            "inputs": [{
                "path": rtsp_url,
                "roles": ["detect", "record"],
            }]
        },
        "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
        "motion": {"threshold": 25, "contour_area": 100},
    }
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/rename")
async def api_rename_camera(request: Request):
    data = await request.json()
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip().replace(" ", "_")
    if not old_name or not new_name:
        return JSONResponse({"error": "old_name and new_name required"}, status_code=400)
    fcfg = read_frigate_config()
    cameras = fcfg.get("cameras", {})
    if old_name not in cameras:
        return JSONResponse({"error": f"Camera '{old_name}' not found"}, status_code=404)
    if new_name in cameras:
        return JSONResponse({"error": f"Camera '{new_name}' already exists"}, status_code=400)
    cameras[new_name] = cameras.pop(old_name)
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


@app.post("/api/cameras/remove")
async def api_remove_camera(camera_name: str = Form(...)):
    fcfg = read_frigate_config()
    fcfg.get("cameras", {}).pop(camera_name, None)
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: detection ────────────────────────────────────────────────────────────

@app.post("/api/detection")
async def api_save_detection(request: Request):
    data = await request.json()
    global_objects = data.get("global_objects", [])
    per_camera = data.get("per_camera", {})  # {camera_name: [objects] or None}

    fcfg = read_frigate_config()
    fcfg["objects"] = {"track": global_objects}

    cameras = fcfg.get("cameras", {})
    for cam_name, cam_cfg in cameras.items():
        overrides = per_camera.get(cam_name)
        if overrides is not None:
            # explicit per-camera list — store only if different from global
            if sorted(overrides) != sorted(global_objects):
                cam_cfg.setdefault("objects", {})["track"] = overrides
            else:
                cam_cfg.pop("objects", None)   # same as global — no override needed
        else:
            cam_cfg.pop("objects", None)       # cleared

    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: retention ────────────────────────────────────────────────────────────

@app.post("/api/retention")
async def api_save_retention(request: Request):
    data = await request.json()
    fcfg = read_frigate_config()
    record = fcfg.setdefault("record", {})
    record.setdefault("alerts", {})["retain"] = {"days": int(data.get("alerts_days", 30))}
    record.setdefault("detections", {})["retain"] = {"days": int(data.get("detections_days", 14))}
    write_frigate_config(fcfg)
    restart_container("frigate")
    return {"ok": True}


# ── API: credentials ──────────────────────────────────────────────────────────

@app.post("/api/credentials")
async def api_save_credentials(request: Request):
    data = await request.json()
    ring_cfg = read_ring_config()
    ring_cfg["livestream_user"] = data.get("user", ring_cfg.get("livestream_user", ""))
    ring_cfg["livestream_pass"] = data.get("pass", ring_cfg.get("livestream_pass", ""))
    write_ring_config(ring_cfg)
    restart_container("ring-mqtt")
    return {"ok": True, "note": "ring-mqtt restarted. Update RING_RTSP_USER/RING_RTSP_PASS in .env and restart Frigate to apply to RTSP stream."}
