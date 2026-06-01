"""
Admin API tests.
Run: pytest admin/tests/ -v
"""
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402 — import after sys.path tweak


# ── fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    """Write minimal config files and return their paths."""
    ring = tmp_path / "config.json"
    ring.write_text(json.dumps({
        "mqtt_url": "mqtt://mosquitto:1883",
        "livestream_user": "stream_user",
        "livestream_pass": "test_pass",
        "enable_cameras": True,
    }))

    frigate = tmp_path / "frigate_config.yaml"
    frigate.write_text(
        "cameras:\n"
        "  test_cam:\n"
        "    ffmpeg:\n"
        "      inputs: [{path: 'rtsp://x:y@host/s', roles: [record]}]\n"
        "record:\n"
        "  alerts: {retain: {days: 30}}\n"
        "  detections: {retain: {days: 14}}\n"
    )

    go2rtc = tmp_path / "go2rtc.yaml"
    go2rtc.write_text(
        "streams:\n"
        "  abc123_live: exec://test\n"
        "rtsp:\n"
        "  username: stream_user\n"
        "  password: test_pass\n"
    )

    meta = tmp_path / "camera_meta.json"
    meta.write_text(json.dumps({
        "test_cam": {"battery": True, "active": False, "camera_id": "abc123", "record_seconds": 120},
    }))

    return {"ring": ring, "frigate": frigate, "go2rtc": go2rtc, "meta": meta}


@pytest.fixture
def client(env):
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.logs.return_value = b"log line"
    mock_docker = MagicMock()
    mock_docker.containers.get.return_value = mock_container

    templates_dir = str(Path(__file__).parent.parent / "templates")

    with patch.object(main, "RING_CONFIG_PATH",    env["ring"]), \
         patch.object(main, "FRIGATE_CONFIG_PATH", env["frigate"]), \
         patch.object(main, "GO2RTC_PATH",         env["go2rtc"]), \
         patch.object(main, "CAMERA_META_PATH",    env["meta"]), \
         patch.object(main, "docker_client",       return_value=mock_docker), \
         patch.object(main, "frigate_stats",       return_value={}), \
         patch.object(main, "restart_container") as mock_restart, \
         patch.object(main, "templates", main.Jinja2Templates(directory=templates_dir)):
        yield TestClient(main.app), mock_restart


# ── dashboard ─────────────────────────────────────────────────────────────────

def test_root_returns_dashboard(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_root_redirects_to_setup_when_no_cameras(client, env):
    c, _ = client
    env["frigate"].write_text("cameras: {}\n")
    env["meta"].write_text("{}")  # also clear meta — all_known_cameras includes battery cams from meta
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_setup_page_loads(client):
    c, _ = client
    r = c.get("/setup")
    assert r.status_code == 200
    assert "Setup" in r.text


# ── status ────────────────────────────────────────────────────────────────────

def test_status_endpoint(client):
    c, _ = client
    r = c.get("/api/status")
    assert r.status_code == 200
    d = r.json()
    assert "services" in d
    assert "frigate_stats" in d
    assert "cameras" in d


def test_cameras_list_endpoint(client):
    c, _ = client
    r = c.get("/api/cameras")
    assert r.status_code == 200
    d = r.json()
    assert "discovered" in d
    assert "abc123" in d["discovered"]


# ── camera add ────────────────────────────────────────────────────────────────

def test_add_ring_camera(client, env):
    c, mock_restart = client
    r = c.post("/api/cameras/add", data={
        "camera_id": "def456",
        "camera_name": "back_door",
        "wired": "false",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    mock_restart.assert_called_once_with("frigate")
    meta = json.loads(env["meta"].read_text())
    assert "back_door" in meta
    assert meta["back_door"]["camera_id"] == "def456"
    assert meta["back_door"]["battery"] is True


def test_add_ring_camera_invalid_id(client):
    c, _ = client
    r = c.post("/api/cameras/add", data={
        "camera_id": "bad/id",
        "camera_name": "cam",
    })
    assert r.status_code == 400


def test_add_ring_camera_invalid_name(client):
    c, _ = client
    r = c.post("/api/cameras/add", data={
        "camera_id": "abc123",
        "camera_name": "bad name!",
    })
    assert r.status_code == 400


def test_add_rtsp_camera(client, env):
    c, mock_restart = client
    r = c.post("/api/cameras/add-rtsp", json={
        "name": "backyard",
        "rtsp_url": "rtsp://user:pass@192.168.1.100/stream",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    fcfg = __import__("yaml").safe_load(env["frigate"].read_text())
    assert "backyard" in fcfg["cameras"]


def test_add_rtsp_camera_missing_url(client):
    c, _ = client
    r = c.post("/api/cameras/add-rtsp", json={"name": "only_name"})
    assert r.status_code == 400


# ── camera rename ─────────────────────────────────────────────────────────────

def test_rename_camera(client, env):
    c, _ = client
    r = c.post("/api/cameras/rename", json={"old_name": "test_cam", "new_name": "front_door"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    fcfg = __import__("yaml").safe_load(env["frigate"].read_text())
    assert "front_door" in fcfg["cameras"]
    assert "test_cam" not in fcfg["cameras"]


def test_rename_nonexistent(client):
    c, _ = client
    r = c.post("/api/cameras/rename", json={"old_name": "ghost", "new_name": "x"})
    assert r.status_code == 404


def test_rename_duplicate_leaves_meta_intact(client, env):
    c, _ = client
    # Add a second camera to Frigate config so new_name already exists
    import yaml
    fcfg = yaml.safe_load(env["frigate"].read_text())
    fcfg["cameras"]["other_cam"] = fcfg["cameras"]["test_cam"].copy()
    env["frigate"].write_text(yaml.dump(fcfg))

    meta_before = env["meta"].read_text()
    r = c.post("/api/cameras/rename", json={"old_name": "test_cam", "new_name": "other_cam"})
    assert r.status_code == 400
    # meta must not have been modified
    assert env["meta"].read_text() == meta_before


# ── camera remove ─────────────────────────────────────────────────────────────

def test_remove_camera(client, env):
    c, mock_restart = client
    r = c.post("/api/cameras/remove", data={"camera_name": "test_cam"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    meta = json.loads(env["meta"].read_text())
    assert "test_cam" not in meta
    fcfg = __import__("yaml").safe_load(env["frigate"].read_text())
    assert "test_cam" not in fcfg.get("cameras", {})
    mock_restart.assert_called_once_with("frigate")


# ── live toggle ───────────────────────────────────────────────────────────────

def test_camera_live_enable(client):
    c, _ = client
    with patch.object(main, "_frigate_set_camera_enabled") as mock_set:
        r = c.post("/api/camera/test_cam/live", json={"enabled": True})
    assert r.status_code == 200
    mock_set.assert_called_once_with("test_cam", True)


def test_camera_live_disable_cancels_timer(client):
    c, _ = client
    mock_timer = MagicMock()
    with patch.dict(main._motion_timers, {"test_cam": mock_timer}), \
         patch.object(main, "_frigate_set_camera_enabled"):
        r = c.post("/api/camera/test_cam/live", json={"enabled": False})
    assert r.status_code == 200
    mock_timer.cancel.assert_called_once()


# ── record seconds ────────────────────────────────────────────────────────────

def test_record_seconds_saves_and_clamps(client, env):
    c, _ = client
    r = c.post("/api/camera/test_cam/record-seconds", json={"seconds": 60})
    assert r.status_code == 200
    assert r.json()["record_seconds"] == 60
    meta = json.loads(env["meta"].read_text())
    assert meta["test_cam"]["record_seconds"] == 60


def test_record_seconds_clamps_below_minimum(client, env):
    c, _ = client
    r = c.post("/api/camera/test_cam/record-seconds", json={"seconds": 5})
    assert r.status_code == 200
    assert r.json()["record_seconds"] == 30


# ── detection / retention / credentials ──────────────────────────────────────

def test_detection_save(client):
    c, _ = client
    r = c.post("/api/detection", json={"global_objects": ["person"], "per_camera": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_retention_save_all_fields(client, env):
    c, _ = client
    r = c.post("/api/retention", json={
        "continuous_days": 3,
        "motion_days": 7,
        "alerts_days": 30,
        "detections_days": 14,
    })
    assert r.status_code == 200
    import yaml
    fcfg = yaml.safe_load(env["frigate"].read_text())
    assert fcfg["record"]["continuous"]["days"] == 3
    assert fcfg["record"]["motion"]["days"] == 7


def test_retention_continuous_zero_removes_key(client, env):
    c, _ = client
    c.post("/api/retention", json={
        "continuous_days": 0,
        "motion_days": 7,
        "alerts_days": 30,
        "detections_days": 14,
    })
    import yaml
    fcfg = yaml.safe_load(env["frigate"].read_text())
    assert "continuous" not in fcfg.get("record", {})


def test_credentials_save(client, env):
    c, _ = client
    r = c.post("/api/credentials", json={"user": "new_user", "pass": "new_pass"})
    assert r.status_code == 200
    cfg = json.loads(env["ring"].read_text())
    assert cfg["livestream_user"] == "new_user"
    assert cfg["livestream_pass"] == "new_pass"


# ── restart ───────────────────────────────────────────────────────────────────

def test_restart_valid_service(client):
    c, mock_restart = client
    r = c.post("/api/restart/frigate")
    assert r.status_code == 200
    mock_restart.assert_called_once_with("frigate")


def test_restart_unknown_service(client):
    c, _ = client
    r = c.post("/api/restart/nonexistent")
    assert r.status_code == 400


# ── logs ──────────────────────────────────────────────────────────────────────

def test_logs_returns_content(client):
    c, _ = client
    with patch.object(main, "container_logs", return_value="SENTINEL_LOG"):
        r = c.get("/api/logs/frigate?lines=10")
    assert r.status_code == 200
    assert r.json()["logs"] == "SENTINEL_LOG"


def test_logs_unknown_service_returns_400(client):
    c, _ = client
    r = c.get("/api/logs/nonexistent")
    assert r.status_code == 400


# ── MQTT handler ──────────────────────────────────────────────────────────────

class _FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


def test_mqtt_motion_on_enables_camera_and_schedules_disable(env):
    with patch.object(main, "CAMERA_META_PATH", env["meta"]), \
         patch.object(main, "_frigate_set_camera_enabled") as mock_enable, \
         patch.object(main, "_schedule_disable") as mock_sched, \
         patch.object(main, "_record_motion_event"):
        main._on_mqtt_message(None, None,
            _FakeMsg("ring/loc1/camera/abc123/motion/state", "ON"))

    mock_enable.assert_called_once_with("test_cam", True)
    mock_sched.assert_called_once_with("test_cam", 120)


def test_mqtt_motion_off_schedules_disable(env):
    with patch.object(main, "CAMERA_META_PATH", env["meta"]), \
         patch.object(main, "_frigate_set_camera_enabled"), \
         patch.object(main, "_schedule_disable") as mock_sched, \
         patch.object(main, "_record_motion_event"):
        main._on_mqtt_message(None, None,
            _FakeMsg("ring/loc1/camera/abc123/motion/state", "OFF"))

    mock_sched.assert_called_once_with("test_cam", 120)


def test_mqtt_ding_triggers_record(env):
    with patch.object(main, "CAMERA_META_PATH", env["meta"]), \
         patch.object(main, "_frigate_set_camera_enabled") as mock_enable, \
         patch.object(main, "_schedule_disable"), \
         patch.object(main, "_record_motion_event") as mock_record:
        main._on_mqtt_message(None, None,
            _FakeMsg("ring/loc1/camera/abc123/ding/state", "ON"))

    mock_enable.assert_called_once_with("test_cam", True)
    mock_record.assert_called_once_with("test_cam")


def test_mqtt_unknown_camera_id_ignored(env):
    with patch.object(main, "CAMERA_META_PATH", env["meta"]), \
         patch.object(main, "_frigate_set_camera_enabled") as mock_enable:
        main._on_mqtt_message(None, None,
            _FakeMsg("ring/loc1/camera/unknown_id/motion/state", "ON"))

    mock_enable.assert_not_called()


def test_mqtt_battery_level_saved(env):
    with patch.object(main, "CAMERA_META_PATH", env["meta"]):
        main._on_mqtt_message(None, None,
            _FakeMsg("ring/loc1/camera/abc123/info/state",
                     json.dumps({"batteryLevel": 72})))

    meta = json.loads(env["meta"].read_text())
    assert meta["test_cam"]["battery_level"] == 72
