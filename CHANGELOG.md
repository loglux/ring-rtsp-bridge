# Changelog

## Unreleased

### Added
- Setup wizard (`/setup`) — 4-step guided first-run configuration
  - RTSP credentials → Cameras → Detection → Retention → Apply
  - Auto-redirects from `/` when no cameras are configured
- Admin API tests (`admin/tests/test_api.py`)

---

## 2026-05-31

### Added
- Admin panel (`admin/`) — FastAPI + Tailwind web UI on port 8085
  - Dashboard: live camera stats, service status, restart buttons
  - Cameras: add by RTSP URL or Ring auto-discovery, rename, remove
  - Detection: global and per-camera object overrides
  - Retention: sliders for alerts and detections
  - Credentials: RTSP username/password
  - Logs: tail any service
  - Live toggle: pause/resume Ring camera stream without restart
- Frigate NVR integration
  - Motion recording with AI object detection (CPU, MobileNet)
  - Tracks: person, car, dog, cat, bicycle, motorcycle
  - Alerts retained 30 days, detections 14 days
  - Web UI at port 5000
- Two-camera setup: Ring doorbell (ring_front) + IP camera (Frontside)
- Named Docker volumes for all runtime state (ASUSTOR-compatible)
- Makefile with `init`, `admin-deploy`, `frigate-config` and other targets

### Changed
- Mosquitto config embedded in container command (avoids bind mount issues on ASUSTOR)
- All volumes changed from bind mounts to named Docker volumes
- `ring-mqtt` image tag left unversioned (tsightler/ring-mqtt, no :tag)

### Security
- `frigate-config/` removed from git tracking (contains runtime camera credentials)
- `frigate-config.example.yaml` added as clean template
- Git history rewritten to remove previously committed credentials

### Fixed
- Mosquitto failed to start after container recreation (bind mount path issue)
- Frigate config schema updated for 0.17-0 (`record.retain` → `record.alerts`/`record.detections`)

---

## 2026-03-xx — Initial commit

- `ring-mqtt` + Mosquitto Docker Compose stack
- RTSP streams exposed on port 8554
- Basic README and configuration examples
