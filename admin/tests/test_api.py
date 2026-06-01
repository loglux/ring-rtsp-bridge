"""
Admin API smoke tests.
Run: pytest admin/tests/ -v
Requires: pip install pytest httpx
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Patch Docker and file I/O before importing app
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def client(tmp_path):
    # Create minimal config files in temp dir
    ring_cfg = tmp_path / "config.json"
    ring_cfg.write_text(json.dumps({
        "mqtt_url": "mqtt://mosquitto:1883",
        "livestream_user": "stream_user",
        "livestream_pass": "test_pass",
        "enable_cameras": True,
    }))
    frigate_cfg = tmp_path / "frigate_config.yaml"
    frigate_cfg.write_text(
        "version: 0.17-0\ncameras:\n  test_cam:\n    ffmpeg:\n      inputs: []\n"
    )
    go2rtc = tmp_path / "go2rtc.yaml"
    go2rtc.write_text("streams:\n  abc123_live: exec://test\n")

    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.logs.return_value = b"Successfully established connection to Ring API"

    mock_docker = MagicMock()
    mock_docker.containers.get.return_value = mock_container

    with patch("main.RING_CONFIG_PATH", ring_cfg), \
         patch("main.FRIGATE_CONFIG_PATH", frigate_cfg), \
         patch("main.GO2RTC_PATH", go2rtc), \
         patch("main.docker_client", return_value=mock_docker), \
         patch("main.frigate_stats", return_value={}), \
         patch("main.restart_container"):
        from main import app
        yield TestClient(app)


def test_root_redirects_to_setup_when_no_cameras(client, tmp_path):
    # Overwrite frigate config with no cameras
    from main import FRIGATE_CONFIG_PATH
    FRIGATE_CONFIG_PATH.write_text("version: 0.17-0\ncameras: {}\n")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_root_returns_dashboard_with_cameras(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ring-tab" in r.text or "Dashboard" in r.text


def test_setup_page_loads(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert "Setup" in r.text


def test_status_endpoint(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "services" in data
    assert "frigate_stats" in data


def test_cameras_endpoint(client):
    r = client.get("/api/cameras")
    assert r.status_code == 200
    data = r.json()
    assert "discovered" in data
    assert "active" in data
    assert "abc123" in data["discovered"]


def test_add_rtsp_camera(client):
    r = client.post("/api/cameras/add-rtsp", json={
        "name": "backyard",
        "rtsp_url": "rtsp://user:pass@192.168.1.100/stream",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_add_rtsp_camera_missing_fields(client):
    r = client.post("/api/cameras/add-rtsp", json={"name": "only_name"})
    assert r.status_code == 400


def test_rename_camera(client):
    r = client.post("/api/cameras/rename", json={
        "old_name": "test_cam",
        "new_name": "front_door",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_rename_nonexistent_camera(client):
    r = client.post("/api/cameras/rename", json={
        "old_name": "ghost",
        "new_name": "something",
    })
    assert r.status_code == 404


def test_detection_save(client):
    r = client.post("/api/detection", json={
        "global_objects": ["person", "car"],
        "per_camera": {},
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_retention_save(client):
    r = client.post("/api/retention", json={
        "alerts_days": 30,
        "detections_days": 14,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_credentials_save(client):
    r = client.post("/api/credentials", json={
        "user": "new_user",
        "pass": "new_pass",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_logs_endpoint(client):
    with patch("main.container_logs", return_value="log line 1\nlog line 2"):
        r = client.get("/api/logs/frigate?lines=10")
    assert r.status_code == 200
    assert "logs" in r.json()


def test_unknown_service_restart(client):
    r = client.post("/api/restart/nonexistent")
    assert r.status_code == 400
