# Ring RTSP Bridge

A self-hosted Docker stack that gives you **local motion-triggered recordings for Ring cameras — no Ring Protect subscription required.**

Ring's cloud subscription charges ~$100/year per account for video history. This stack records everything locally with AI object detection, retains clips as long as you want, and works alongside any wired IP camera.

> **Note:** Ring cameras are cloud devices. Video still routes through Ring's servers before reaching this stack — this is a Ring hardware limitation, not something we can change.

---

## What you get

- **Motion-triggered recordings** stored locally — no cloud subscription
- **AI object detection** — alerts tagged by person, car, dog, etc. (CPU-based, no GPU needed)
- **Battery camera support** — Ring battery cameras activate only on motion events; no continuous stream drain
- **Battery level monitoring** — charge % shown in the admin dashboard, updated automatically via MQTT
- **Any RTSP camera** alongside Ring — add Hikvision, Reolink, Dahua, Axis, etc.
- **Web admin panel** — manage cameras, detection objects, retention, credentials (port 8085)
- **Frigate NVR UI** — browse recorded clips, timeline, live streams (port 5000)

---

## Stack

| Container | Image | Purpose | Port |
|---|---|---|---|
| `ring-rtsp-mosquitto` | `eclipse-mosquitto:2` | MQTT broker (Ring events + admin) | — |
| `ring-rtsp-bridge` | `tsightler/ring-mqtt` | Ring → RTSP/MQTT bridge | 8554 (RTSP), 55123 (auth UI) |
| `ring-frigate` | `ghcr.io/blakeblackshear/frigate:stable` | NVR, AI detection, clip storage | 5000 (UI) |
| `ring-admin` | local build (`admin/`) | Admin panel | 8085 |

All persistent data lives in Docker named volumes — no bind mounts for runtime state.

| Volume | Contents |
|---|---|
| `ring-rtsp-bridge_ring-mqtt-data` | Ring auth token, go2rtc RTSP config |
| `ring-rtsp-bridge_frigate-config` | Frigate config, camera metadata, SQLite DB, model cache |
| `ring-rtsp-bridge_frigate-clips` | Recorded video clips |
| `ring-rtsp-bridge_mosquitto-data` | MQTT persistence |

---

## Quick start

### 1. Configure `.env`

```sh
cp .env.example .env
```

Edit `.env`:

```sh
RING_RTSP_PORT=8554
RING_RTSP_USER=stream_user       # RTSP username for ring-mqtt streams
RING_RTSP_PASS=your_password     # Change this from the default
```

### 2. Start the stack

```sh
make init
```

`make init` starts all containers in order and pushes ring-mqtt and Frigate configs into their volumes via `docker cp`.

### 3. Authenticate with Ring (first run only)

```
http://<host>:55123/
```

Log in with your Ring account and 2FA code. ring-mqtt stores a refresh token and reconnects automatically on future restarts.

### 4. Add cameras via the admin panel

```
http://<host>:8085/
```

- **Ring cameras** are auto-discovered under **Cameras → Ring cameras**. Enter a name and click "Add to Frigate".
- **IP cameras** (any RTSP source) can be added under **Cameras → Add camera — RTSP URL**.

### 5. Browse recordings

```
http://<host>:5000/
```

Frigate UI shows recorded clips, alerts timeline, and live streams.

---

## How recording works

### Wired IP cameras (Hikvision, Reolink, Dahua, Axis, etc.)

These cameras work **100% locally** — no cloud, no subscription, no external dependency of any kind. Frigate connects directly to the camera's RTSP stream on your LAN.

Any pixel motion → recording segment written to disk. AI model runs at 5 fps (640×360, CPU MobileNet):

- Detected object (person, car, etc.) → **alert** (default 30-day retention)
- Motion without identified object → **detection** (default 14-day retention)

In many ways this **outperforms paid cloud recording**: no compression artifacts from re-encoding, no cloud upload lag, configurable retention up to 90 days, and AI object detection that cloud plans often charge extra for.

### Ring battery cameras (event-driven, MQTT-triggered)

Battery cameras use an event-driven flow to avoid draining the battery with a continuous RTSP stream:

1. Frigate starts with the camera **disabled** — no ffmpeg, no RTSP pull
2. Ring's PIR sensor fires → ring-mqtt publishes `motion/state ON` or `ding/state ON` to MQTT
3. The admin service receives the event → publishes `frigate/<cam>/enabled/set ON` to Frigate via MQTT
4. Frigate starts ffmpeg and connects to the camera's `_live` RTSP stream → records the clip
5. After the configured record window → admin publishes `frigate/<cam>/enabled/set OFF` → Frigate stops ffmpeg → Ring goes back to sleep

**Why this matters:** Ring stops sending MQTT motion/ding events while actively streaming. Keeping the camera always-on in Frigate would mean Ring never reports events. The camera must be off between events for the trigger to work.

> `_event` (ring-mqtt's pre-recorded cloud clip stream) requires a Ring Protect subscription and is not used here. We use `_live` only, triggered on demand.

#### Important: Frigate config vs runtime state

Battery cameras must have **`enabled: true`** (or no `enabled` field) in Frigate's `config.yaml`. Frigate 0.17 blocks MQTT enable/disable commands if a camera has `enabled: false` in its config — the command silently fails. The "disabled between events" state is achieved entirely at runtime via MQTT:

- On admin startup → `frigate/<cam>/enabled/set OFF` (retained) is published to MQTT. Frigate receives it and stops ffmpeg even if it restarts later.
- On motion event → `frigate/<cam>/enabled/set ON` (not retained) — ephemeral, only for the recording window.
- After recording → `frigate/<cam>/enabled/set OFF` (retained) — resets the retained state back to idle.

This means after any Frigate restart, cameras automatically return to the disabled/idle state when admin reconnects to MQTT.

The admin panel shows each battery camera's state:
- **🟡 Ready** — camera disabled in Frigate, Ring is idle and reporting events
- **🟢 Recording** — live stream active, Frigate is recording right now
- **⚫ Paused** — manually disabled

Battery charge level is read from MQTT (`ring/.../info/state`) and shown in the dashboard, updated every ~5 minutes.

### Wired Ring cameras (transformer-powered)

If your Ring doorbell is wired to a doorbell transformer, it can sustain a continuous live stream. When adding via the admin panel, check **"Wired / powered"** — the camera will use the `_live` stream and behave like a standard IP camera.

---

## Admin panel

| Tab | What you can do |
|---|---|
| **Dashboard** | Camera cards with live fps stats, battery level, Ready/Recording/Paused state; service status; restart buttons |
| **Cameras** | Add Ring cameras (with wired/battery choice) or any RTSP camera; rename; remove |
| **Detection** | Choose which objects Frigate tracks globally and per-camera |
| **Retention** | Sliders for all four retention categories: Continuous, Motion clips, Alerts, Detections |
| **Credentials** | Change RTSP username and password for ring-mqtt streams |
| **Logs** | Live log tail for any service |

---

## RTSP streams (ring-mqtt)

```
rtsp://<user>:<pass>@<host>:8554/<camera_id>_live    # live on-demand stream (all cameras)
```

`<camera_id>` is the Ring device ID shown in the admin panel.
Streams auto-start when a client connects and auto-stop ~5–10 s after the last client disconnects.

> `_event` streams require a Ring Protect subscription and are not used by this stack.

Credentials are set in Admin → Credentials (or in `ring-mqtt-data/config.json`).

---

## Deployment

### Standard Linux

```sh
make init   # first run
make up     # subsequent starts
```

### ASUSTOR NAS

ASUSTOR enforces a hard file descriptor limit of 1024 per process. The `docker-socket-proxy` service (HAProxy) requires ~8034 FDs and crashes immediately on ASUSTOR. Use the provided override which replaces the proxy with a no-op container and mounts the Docker socket directly instead:

```sh
make asustor-init   # first run
make asustor-up     # subsequent starts
```

The override file is `docker-compose.asustor.yml`. It:
- Replaces `docker-socket-proxy` with a `busybox sleep infinity` container (satisfies `depends_on` without proxying)
- Adds `/var/run/docker.sock` directly to the admin container
- Sets `DOCKER_HOST=""` so the admin uses the socket, not the proxy

This is acceptable because port 8085 is LAN-only on a home NAS and the admin process runs as a non-root user (uid 1001).

---

## Makefile reference

```sh
make init            # First-run: start stack + push configs (standard Linux)
make up              # Start all containers
make down            # Stop all containers
make status          # Show container status
make pull            # Pull latest images

make asustor-init    # First-run on ASUSTOR NAS (no socket proxy)
make asustor-up      # Start all containers on ASUSTOR

make admin-deploy    # Rebuild admin image and recreate container
make frigate-config  # Push frigate-config/config.yaml and restart Frigate

make logs            # Tail all logs
make frigate-logs    # Tail Frigate logs
make bridge-logs     # Tail ring-mqtt logs

make lint            # Validate docker-compose.yml
make check-env       # Check .env for unchanged placeholder values
make auth-ui         # Print Ring auth URL
make frigate-ui      # Print Frigate UI URL
```

> `docker restart` does not apply a newly built image. Use `make admin-deploy` after editing admin code.

---

## Notes

- Ring cameras route video through Ring's cloud — this is a hardware/firmware constraint.
- MQTT is not exposed outside Docker by default. To connect Home Assistant, expose Mosquitto's port and add authentication.
- Frigate config is delivered via `docker cp` because named volumes are used (bind mounts are unreliable in this deployment environment).
- Battery level updates arrive every ~5 minutes from ring-mqtt. The dashboard refreshes automatically.

## Future directions

- Frigate zones — limit detection to specific areas of the frame
- Coral TPU passthrough for faster AI inference
- Backup and restore guide for named volumes
- Auto-generate secure RTSP password during setup wizard

## Sources

- ring-mqtt: https://github.com/tsightler/ring-mqtt/wiki
- Frigate: https://docs.frigate.video
- go2rtc: https://github.com/AlexxIT/go2rtc
