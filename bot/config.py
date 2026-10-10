"""Environment configuration loading and validation.

Configuration is read once at startup. Any missing or invalid value raises
:class:`ConfigError` with a message listing every problem so the operator can fix
them in one pass. The bot token is never included in ``repr`` output or errors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_DB_PATH = "data/leaderboard.db"
DEFAULT_LOG_DIR = "logs"
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Values that clearly come from an un-edited .env.example.
_PLACEHOLDER_TOKENS = {
    "your-bot-token-here",
    "your_bot_token_here",
    "changeme",
    "token",
    "<token>",
}


class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    db_path: Path
    flush_interval_seconds: float
    reset_grace_seconds: int
    board_update_seconds: int
    log_dir: Path
    log_level: str = "INFO"


def prepare_storage(config: Config) -> None:
    """Create the database and log directories and verify they are writable.

    Raises :class:`ConfigError` so an unusable volume is reported like any other
    configuration problem (exit code 2, no restart loop) instead of a traceback.
    """
    problems: list[str] = []
    uid = getattr(os, "getuid", lambda: "?")()
    for label, directory in (("DB_PATH directory", config.db_path.parent), ("LOG_DIR", config.log_dir)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{label} {directory} cannot be created: {exc.strerror or exc}")
            continue
        if not os.access(directory, os.W_OK | os.X_OK):
            problems.append(f"{label} {directory} is not writable by this user (uid {uid})")
    if config.db_path.exists() and not os.access(config.db_path, os.R_OK | os.W_OK):
        problems.append(f"DB_PATH {config.db_path} is not readable and writable by this user (uid {uid})")
    if problems:
        raise ConfigError(
            "Storage is not usable:\n  - "
            + "\n  - ".join(problems)
            + "\nIf this is a mounted volume, make it writable by the bot's user (UID 10001 in the Docker image)."
        )


def _expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw.strip())))


def _parse_number(
    env: Mapping[str, str],
    name: str,
    default: str,
    *,
    kind: type,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float | int:
    raw = env.get(name, "").strip() or default
    try:
        value = kind(raw)
    except ValueError:
        errors.append(f"{name} must be a {'whole number' if kind is int else 'number'}, got {raw!r}")
        return kind(default)
    if value != value or not (minimum <= value <= maximum):  # value != value catches NaN
        errors.append(f"{name} must be between {minimum:g} and {maximum:g}, got {raw!r}")
        return kind(default)
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a validated :class:`Config` from ``env`` (defaults to ``os.environ``)."""
    if env is None:
        env = os.environ
    errors: list[str] = []

    token = env.get("DISCORD_TOKEN", "").strip()
    if not token:
        errors.append("DISCORD_TOKEN is required but is missing or empty")
    elif token.lower() in _PLACEHOLDER_TOKENS:
        errors.append("DISCORD_TOKEN still contains the placeholder value from .env.example")
    elif any(ch.isspace() for ch in token):
        errors.append("DISCORD_TOKEN must not contain whitespace")

    raw_db = env.get("DB_PATH", "").strip() or DEFAULT_DB_PATH
    db_path = _expand_path(raw_db)
    if raw_db == ":memory:" or raw_db.startswith("file::memory:"):
        errors.append("DB_PATH must be a file path; an in-memory database would lose all data on restart")
    elif db_path.exists() and db_path.is_dir():
        errors.append(f"DB_PATH points to a directory, expected a file: {db_path}")

    flush = _parse_number(
        env, "FLUSH_INTERVAL_SECONDS", "5", kind=float, minimum=0.5, maximum=300, errors=errors
    )
    grace = _parse_number(
        env, "RESET_GRACE_SECONDS", "120", kind=int, minimum=0, maximum=3600, errors=errors
    )
    board = _parse_number(
        env, "BOARD_UPDATE_SECONDS", "300", kind=int, minimum=60, maximum=86400, errors=errors
    )

    raw_log_dir = env.get("LOG_DIR", "").strip() or DEFAULT_LOG_DIR
    log_dir = _expand_path(raw_log_dir)
    if log_dir.exists() and not log_dir.is_dir():
        errors.append(f"LOG_DIR points to a file, expected a directory: {log_dir}")

    log_level = (env.get("LOG_LEVEL", "").strip() or "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        errors.append(f"LOG_LEVEL must be one of {', '.join(VALID_LOG_LEVELS)}, got {log_level!r}")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    return Config(
        token=token,
        db_path=db_path,
        flush_interval_seconds=float(flush),
        reset_grace_seconds=int(grace),
        board_update_seconds=int(board),
        log_dir=log_dir,
        log_level=log_level,
    )
