# Ring RTSP Bridge

This repository uses a `ring-mqtt`-based Docker setup to expose Ring cameras as RTSP streams and local events.

Important limitation:

- this still does **not** make Ring local-only
- Ring live video still comes through Ring's cloud
- this only gives you a cleaner way to consume Ring streams from your own RTSP-compatible tools

## What you get

- RTSP live streams for Ring cameras
- optional RTSP authentication
- a local Dockerized MQTT broker for `ring-mqtt`
- persistent config/state under this project directory

## Goal

This project exists to make an existing Ring installation easier to use from a self-managed local stack.

Typical target integrations:

- `Frigate`
- `Home Assistant`
- `go2rtc`
- `ZoneMinder`
- `VLC` or `ffplay` for direct testing

This is a bridge project, not a full replacement for the Ring app or Ring cloud.

## How it works

At runtime the stack looks like this:

1. `ring-mqtt` authenticates against Ring using your Ring account and 2FA.
2. `ring-mqtt` discovers your Ring devices and exposes camera streams.
3. `ring-mqtt` publishes RTSP streams on port `8554` by default.
4. Your local tools connect to those RTSP URLs and handle viewing, recording, or automation.
5. A local Mosquitto broker is included because `ring-mqtt` expects MQTT as part of its runtime model.

Data flow in simple terms:

`Ring cloud -> ring-mqtt -> local RTSP consumer`

Important consequence:

- the stream becomes easier to consume locally
- the source is still Ring cloud, not direct camera-to-LAN streaming

## Project layout

- `docker-compose.yml`: recommended runtime
- `mosquitto.conf`: local MQTT broker config
- `ring-mqtt-config.example.json`: starter config for `ring-mqtt`
- `ring-mqtt-data/config.json`: local runtime config copied from the example
- `ring-mqtt-data/`: persistent runtime state, including Ring auth/session data
- `mosquitto-data/`: local MQTT persistence
- `mosquitto-log/`: local MQTT logs

## Prerequisites

- Docker
- Docker Compose
- a Ring account with 2FA enabled

## Quick start

### 1. Prepare the local files

Create the runtime directory and copy the example config:

```sh
mkdir -p ring-mqtt-data
cp ring-mqtt-config.example.json ring-mqtt-data/config.json
```

Optional: copy the env file if you want a non-default RTSP port.

```sh
cp .env.example .env
```

### 2. Start the local MQTT broker

```sh
docker compose up -d mosquitto
```

### 3. Start the bridge

```sh
docker compose up -d ring-mqtt
```

### 4. Complete one-time Ring authentication

On first run, `ring-mqtt` exposes a local authentication UI on port `55123`.

Open it from the host machine or your LAN and complete Ring login plus 2FA there. After successful login, `ring-mqtt` writes runtime state into `ring-mqtt-data/ring-state.json`.

If you do not need the auth UI after bootstrap, you can later remove the `55123:55123` port mapping from `docker-compose.yml`.

### 5. Read the logs

```sh
docker compose logs -f ring-mqtt
```


## Standard console workflow

These are the main commands you are likely to use:

Start MQTT only:

```sh
docker compose up -d mosquitto
```

Start the bridge:

```sh
docker compose up -d ring-mqtt
```

Complete first-run auth:

Open `http://<host>:55123/`

Restart the bridge after config changes:

```sh
docker compose restart ring-mqtt
```

Stop the stack:

```sh
docker compose down
```

Inspect logs:

```sh
docker compose logs -f ring-mqtt
docker compose logs -f mosquitto
```

## RTSP URLs

`ring-mqtt` creates RTSP paths in this format:

- live stream: `rtsp://<host>:8554/<camera_id>_live`
- recorded event stream: `rtsp://<host>:8554/<camera_id>_event`

If you set `livestream_user` and `livestream_pass` in `ring-mqtt-data/config.json`, most players will prompt for credentials, or you can embed them in the URL:

```text
rtsp://stream_user:stream_pass@<host>:8554/<camera_id>_live
```

## Recommended config changes

Edit `ring-mqtt-data/config.json` after the first init if needed:

- set `livestream_user` and `livestream_pass`
- keep `enable_cameras` enabled
- leave `mqtt_url` as `mqtt://mosquitto:1883` for this compose setup

Suggested first changes:

- replace the example RTSP username/password
- keep `enable_modes` and `enable_panic` disabled unless you explicitly need alarm features
- keep `location_ids` empty unless you want to limit the bridge to selected Ring locations

## Motion recording with Frigate

Frigate is an NVR container that connects to the ring-mqtt RTSP stream, records clips triggered by motion, and provides a web UI for browsing footage. It also runs AI object detection to label clips by type (person, car, dog, cat, bicycle, motorcycle).

### How recording works

- Motion detected → recording starts immediately
- AI identifies an object (person/car/dog...) → event marked as **alert**, kept 30 days
- Motion only, no identified object → event marked as **detection**, kept 14 days
- Snapshots saved alongside every event

### Volume layout

All persistent data lives in Docker named volumes (not bind mounts). This works correctly regardless of where docker compose is run from.

| Volume | Contents |
|---|---|
| `ring-rtsp-bridge_ring-mqtt-data` | Ring auth token, go2rtc config, runtime state |
| `ring-rtsp-bridge_frigate-config` | Frigate config.yaml, database, model cache |
| `ring-rtsp-bridge_frigate-clips` | Recorded video clips |
| `ring-rtsp-bridge_mosquitto-data` | MQTT persistence |

### Fresh deployment

1. Copy and edit `.env`:

   ```sh
   cp .env.example .env
   # set RING_RTSP_USER, RING_RTSP_PASS, RING_CAMERA_ID
   ```

2. Start everything and push configs into the volumes:

   ```sh
   make init
   ```

   `make init` starts all containers then uses `docker cp` to load ring-mqtt auth state and Frigate config into their volumes.

3. Complete Ring auth if needed (first-ever run):

   Open `http://<host>:55123/` and log in.

4. Open Frigate:

   ```
   http://<host>:5000/
   ```

### Finding your camera ID

After ring-mqtt connects, run:

```sh
docker exec ring-rtsp-bridge cat /data/go2rtc.yaml
```

The camera ID is the part before `_live` in the stream key. Set it as `RING_CAMERA_ID` in `.env`.

### Pushing an updated Frigate config

Edit `frigate-config/config.yaml` then:

```sh
docker cp frigate-config/config.yaml ring-frigate:/config/config.yaml
docker restart ring-frigate
```

### Object detection

The default model runs on CPU and detects: `person`, `car`, `dog`, `cat`, `bicycle`, `motorcycle`.

Detection runs at 640×360 px, 5 fps — appropriate for 1–2 cameras on Intel hardware without a Coral TPU. To add a Coral USB Accelerator later, change `detectors.cpu1.type` to `edgetpu` in `frigate-config/config.yaml` and add the USB device passthrough to the frigate service in `docker-compose.yml`.

### Event retention

| Event type | Kept for |
|---|---|
| Alert (object detected) | 30 days |
| Detection (motion only) | 14 days |

Settings are in `frigate-config/config.yaml` under `record.alerts` and `record.detections`.

### Camera name

The camera is named `ring_front` in `frigate-config/config.yaml`. Rename it if needed — the name appears in the UI and affects how Frigate labels recordings.

## Notes

- Continuous viewing is possible, but Ring cameras are cloud devices and long-running streams may increase battery use, heat, and side effects on Ring behavior.
- This repository does not expose MQTT outside Docker by default. If you later want Home Assistant or another tool to consume MQTT directly, add a published port for Mosquitto and secure it properly.

## Current design choice

This repository is intentionally a self-managed integration project around `ring-mqtt`, not a custom implementation of the Ring streaming protocol.

That tradeoff is deliberate:

- less custom code to maintain
- faster path to a usable RTSP bridge
- better practical reliability than the old prototype
- still flexible enough to add our own scripts, docs, wrappers, health checks, or deployment logic later

## Future directions

Reasonable next steps for this repository:

- add a small healthcheck wrapper
- add a helper script for first-time auth and startup
- add a hardened Mosquitto config if external MQTT access is needed
- add a backup/restore note for `ring-mqtt-data/`
- add Coral TPU passthrough to Frigate for faster AI detection
- add Frigate zones to limit detection to specific areas of the frame

## Sources

- ring-mqtt overview: https://github.com/tsightler/ring-mqtt/wiki
- Docker install: https://github.com/tsightler/ring-mqtt/wiki/Installation-%28Docker%29
- video streaming details: https://github.com/tsightler/ring-mqtt/wiki/Video-Streaming
