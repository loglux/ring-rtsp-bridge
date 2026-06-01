#!/bin/sh
# Fix ownership of config volumes so the non-root user can write to them.
# /media/frigate (clips) is intentionally skipped — it can contain many files.
chown -R appuser:appgroup /ring-mqtt-data /frigate-config 2>/dev/null || true

# Grant appuser access to the Docker socket regardless of host GID.
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if [ "$SOCK_GID" != "0" ]; then
        addgroup --gid "$SOCK_GID" dockersock 2>/dev/null || true
        adduser appuser dockersock 2>/dev/null || true
    else
        chmod 666 /var/run/docker.sock
    fi
fi

exec gosu appuser "$@"
