#!/bin/sh
# Fix ownership of config volumes so the non-root user can write to them.
# /media/frigate (clips) is intentionally skipped — it can contain many files.
chown -R appuser:appgroup /ring-mqtt-data /frigate-config 2>/dev/null || true
exec gosu appuser "$@"
