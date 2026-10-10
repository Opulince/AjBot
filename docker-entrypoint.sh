#!/bin/sh
# Container entrypoint. Many hosts mount persistent volumes owned by root; when
# started as root, take ownership of /data and then drop to the unprivileged
# "bot" user. When the platform already runs the container as a non-root user,
# start directly (an unwritable volume is then reported as a configuration error).
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    chown -R bot:bot /data || echo "warning: could not change the owner of /data" >&2
    exec setpriv --reuid=bot --regid=bot --init-groups "$@"
fi
exec "$@"
