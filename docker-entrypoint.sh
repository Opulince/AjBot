#!/bin/sh
# Container entrypoint. Many hosts mount persistent volumes owned by root; when
# started as root, take ownership of /data and then drop to the unprivileged
# "bot" user. When the platform already runs the container as a non-root user,
# start directly (an unwritable volume is then reported as a configuration error).
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data
    # Only walk the volume when its top-level owner is wrong. A recursive chown on
    # every start is slow on large or network volumes, makes restart loops worse
    # and silently re-owns host files on bind mounts. Files with other owners
    # inside an already-owned /data are not fixed here: the bot's startup storage
    # check (prepare_storage in bot/config.py) reports an unusable database or log
    # directory and exits with code 2.
    if [ "$(stat -c %u /data)" != "$(id -u bot)" ]; then
        chown -R bot:bot /data || echo "warning: could not change the owner of /data" >&2
    fi
    # setpriv keeps the root environment, so HOME would stay /root. Point HOME,
    # USER and LOGNAME at the bot user so "~" in DB_PATH or LOG_DIR (and anything
    # else under $HOME) resolves to its home directory. Not setpriv --reset-env:
    # it clears the whole environment, including DISCORD_TOKEN and the other config.
    HOME=$(getent passwd bot | cut -d: -f6)
    USER=bot
    LOGNAME=bot
    export HOME USER LOGNAME
    exec setpriv --reuid=bot --regid=bot --init-groups "$@"
fi
exec "$@"
