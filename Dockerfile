# Small production image for the leaderboard bot.
# PYTHON_IMAGE can point at a registry mirror of the same official image, e.g.
#   docker build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim .
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/leaderboard.db \
    LOG_DIR=/data/logs

WORKDIR /app

# Unprivileged runtime user (the entrypoint drops to it); /data is the
# persistent volume holding the database and logs.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]

# The entrypoint fixes /data ownership (root-owned provider volumes) and then
# runs the bot as the unprivileged "bot" user via setpriv. The bot handles
# SIGTERM: it stops its tasks, flushes buffered counts and closes the database.
# Give it time with `docker run --stop-timeout 30`.
STOPSIGNAL SIGTERM
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
