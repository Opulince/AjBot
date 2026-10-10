# Small production image for the leaderboard bot.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/leaderboard.db \
    LOG_DIR=/data/logs

WORKDIR /app

# Unprivileged runtime user; /data is the persistent volume (database + logs).
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

USER bot
VOLUME ["/data"]

# The bot handles SIGTERM: it stops its tasks, flushes buffered counts and
# closes the database. Give it time with `docker run --stop-timeout 30`.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot.main"]
