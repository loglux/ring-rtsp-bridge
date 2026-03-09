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
- add `go2rtc` or `Frigate` example configs
- add a helper script for first-time auth and startup
- add a hardened Mosquitto config if external MQTT access is needed
- add a backup/restore note for `ring-mqtt-data/`

## Sources

- ring-mqtt overview: https://github.com/tsightler/ring-mqtt/wiki
- Docker install: https://github.com/tsightler/ring-mqtt/wiki/Installation-%28Docker%29
- video streaming details: https://github.com/tsightler/ring-mqtt/wiki/Video-Streaming
