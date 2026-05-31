# Ring RTSP Bridge

A self-managed Docker stack that exposes Ring cameras as RTSP streams, records motion clips with AI object detection, and provides a web admin panel.

**Important limitation:** Ring cameras are cloud devices. Live video still routes through Ring's cloud — this stack gives you a cleaner local way to consume and record those streams.

## What you get

- RTSP live streams for Ring cameras (via `ring-mqtt` + `go2rtc`)
- Any IP camera with an RTSP URL can be added alongside Ring cameras
- Motion recording with AI object detection (Frigate NVR)
- Web UI for browsing recorded clips and live streams (Frigate, port 5000)
- Web admin panel for managing cameras, detection settings, retention, credentials (port 8085)
- Local Mosquitto MQTT broker

## Stack

| Container | Image | Purpose | Port |
|---|---|---|---|
| `ring-rtsp-mosquitto` | `eclipse-mosquitto:2` | MQTT broker | — |
| `ring-rtsp-bridge` | `tsightler/ring-mqtt` | Ring → RTSP bridge | 8554 (RTSP), 55123 (auth UI) |
| `ring-frigate` | `ghcr.io/blakeblackshear/frigate:stable` | NVR, AI detection, clip storage | 5000 (UI) |
| `ring-admin` | local build (`admin/`) | Admin panel | 8085 |

All persistent data lives in Docker named volumes — no bind mounts for runtime state.

| Volume | Contents |
|---|---|
| `ring-rtsp-bridge_ring-mqtt-data` | Ring auth token, RTSP config |
| `ring-rtsp-bridge_frigate-config` | Frigate config, database, model cache |
| `ring-rtsp-bridge_frigate-clips` | Recorded video clips |
| `ring-rtsp-bridge_mosquitto-data` | MQTT persistence |

## Prerequisites

- Docker + Docker Compose
- `make`
- A Ring account with 2FA enabled (for Ring cameras)

## Quick start

### 1. Configure `.env`

```sh
cp .env.example .env
```

Edit `.env`:

```sh
RING_RTSP_PORT=8554
RING_RTSP_USER=stream_user       # RTSP username for ring-mqtt streams
RING_RTSP_PASS=your_password     # RTSP password — change from default
RING_CAMERA_ID=your_camera_id    # find after first ring-mqtt run (see below)
```

### 2. Start the stack

```sh
make init
```

`make init` starts all containers in order and pushes ring-mqtt and Frigate configs into their named volumes via `docker cp`.

### 3. Complete Ring authentication (first run only)

Open the ring-mqtt auth UI and log in with your Ring account + 2FA:

```
http://<host>:55123/
```

After login, ring-mqtt writes a refresh token to its volume and reconnects automatically on future restarts.

### 4. Find your Ring camera ID

```sh
docker exec ring-rtsp-bridge cat /data/go2rtc.yaml
```

The camera ID is the part before `_live` in each stream key (e.g. `5c475e578873`). Set it as `RING_CAMERA_ID` in `.env`, then run `make init` again or add the camera via the admin panel.

### 5. Open the UIs

| UI | URL | Purpose |
|---|---|---|
| Admin panel | `http://<host>:8085/` | Manage cameras, detection, settings |
| Frigate | `http://<host>:5000/` | View clips, live streams, timeline |
| Ring auth | `http://<host>:55123/` | One-time Ring login |

## Admin panel

The admin panel (`admin/`) is a FastAPI + Tailwind web app for managing the stack without editing config files.

**Dashboard** — live camera stats (fps, detection fps), service status, restart buttons, per-camera Live/Paused toggle

**Cameras** — add cameras by RTSP URL (any IP camera) or from Ring auto-discovery; rename and remove cameras

**Detection** — select which objects Frigate should identify (person, car, dog, cat, bicycle, motorcycle, truck, bird); per-camera overrides

**Retention** — sliders for how long to keep alerts (default 30 days) and detections (default 14 days)

**Credentials** — change ring-mqtt RTSP username and password

**Logs** — live log tail for any service

### Live toggle (Ring cameras)

Ring cameras stream through the cloud — continuous connections increase battery use. The **Live / Paused** button on the Dashboard stops detection and recording for a camera without restarting Frigate.

## Adding cameras

### Any IP camera (Hikvision, Reolink, Dahua, etc.)

Admin panel → **Cameras** → "Add camera — RTSP URL":

- Enter a name (e.g. `backyard`)
- Enter the RTSP URL (e.g. `rtsp://user:pass@192.168.1.x/stream`)
- Click **Add camera** — Frigate restarts automatically

### Ring cameras

Ring cameras are auto-discovered by ring-mqtt when it connects to your Ring account. They appear in Admin → **Cameras** → "Ring cameras — auto-discovered". Enter a name and click **Add to Frigate**.

If a new camera doesn't appear: restart ring-mqtt (Admin → Dashboard → Restart ring-mqtt) and wait ~30 seconds.

## Makefile reference

```sh
make init            # First-run: start stack + push configs via docker cp
make up              # Start all containers
make down            # Stop all containers
make status          # Show container status
make pull            # Pull latest images

make admin-deploy    # Rebuild admin image and recreate container
make frigate-config  # Push frigate-config/config.yaml and restart Frigate

make logs            # Tail all logs
make frigate-logs    # Tail Frigate logs
make bridge-logs     # Tail ring-mqtt logs

make lint            # Validate docker-compose.yml and check required files
make check-env       # Check .env for unchanged placeholder values

make auth-ui         # Print Ring auth URL
make frigate-ui      # Print Frigate UI URL
```

> **Note:** `docker restart` does not apply a newly built image. Always use `make admin-deploy` (or `docker compose up -d --build admin`) after editing admin code.

## Updating Frigate config

Edit `frigate-config/config.yaml` then:

```sh
make frigate-config
```

This pushes the file into the Frigate volume and restarts the container.

## RTSP URLs (ring-mqtt streams)

Ring camera streams served by ring-mqtt:

```
rtsp://<user>:<pass>@<host>:8554/<camera_id>_live
rtsp://<user>:<pass>@<host>:8554/<camera_id>_event
```

Credentials (`user`/`pass`) are set in Admin → Credentials (or directly in `ring-mqtt-data/config.json`).

## How recording works

1. Frigate connects to camera RTSP streams
2. Any pixel motion → recording segment written to `frigate-clips` volume
3. AI model (CPU, MobileNet) runs at 5 fps on 640×360 frames
4. Detected object → event marked as **alert** (default 30-day retention)
5. Motion without identified object → **detection** (default 14-day retention)

All settings adjustable from Admin → Detection and Admin → Retention.

## Notes

- Ring cameras are cloud devices — continuous streaming may increase battery use and heat.
- MQTT is not exposed outside Docker by default. To connect Home Assistant directly, add a Mosquitto port and secure it.
- Frigate config is delivered via `docker cp` because named volumes are used (bind mounts from the host are unreliable in this deployment environment).

## Future directions

- Frigate zones — limit detection to specific areas of the frame
- Coral TPU passthrough for faster AI inference
- Backup and restore guide for named volumes
- Healthcheck endpoints for all services

## Sources

- ring-mqtt: https://github.com/tsightler/ring-mqtt/wiki
- Frigate: https://docs.frigate.video
- go2rtc: https://github.com/AlexxIT/go2rtc
