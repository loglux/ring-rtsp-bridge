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
    objects = data.get("objects", [])
    fcfg = read_frigate_config()
    fcfg["objects"] = {"track": objects}
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
