"""Configuration validation, logging setup and secret redaction."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from bot.config import ConfigError, load_config, prepare_storage
from bot.main import SecretRedactor, setup_logging

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xyz"


def test_defaults_with_only_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config({"DISCORD_TOKEN": TOKEN})
    assert cfg.token == TOKEN
    assert cfg.db_path == Path("data/leaderboard.db")
    assert cfg.flush_interval_seconds == 5.0
    assert cfg.reset_grace_seconds == 120
    assert cfg.board_update_seconds == 300
    assert cfg.log_dir == Path("logs")
    assert cfg.log_level == "INFO"


def test_explicit_values_and_persistent_volume_paths(tmp_path: Path) -> None:
    cfg = load_config({
        "DISCORD_TOKEN": TOKEN,
        "DB_PATH": str(tmp_path / "volume" / "lb.sqlite3"),
        "FLUSH_INTERVAL_SECONDS": "2.5",
        "RESET_GRACE_SECONDS": "0",
        "BOARD_UPDATE_SECONDS": "600",
        "LOG_DIR": str(tmp_path / "logs"),
        "LOG_LEVEL": "debug",
    })
    assert cfg.db_path == tmp_path / "volume" / "lb.sqlite3"
    assert (cfg.flush_interval_seconds, cfg.reset_grace_seconds, cfg.board_update_seconds) == (2.5, 0, 600)
    assert cfg.log_level == "DEBUG"


@pytest.mark.parametrize("token", ["", "   ", "your-bot-token-here", "abc def"])
def test_bad_tokens_fail_fast(token: str) -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config({"DISCORD_TOKEN": token})


def test_missing_token_fails_fast() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN is required"):
        load_config({})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FLUSH_INTERVAL_SECONDS", "abc"),
        ("FLUSH_INTERVAL_SECONDS", "0"),
        ("FLUSH_INTERVAL_SECONDS", "nan"),
        ("RESET_GRACE_SECONDS", "-1"),
        ("RESET_GRACE_SECONDS", "1.5"),
        ("BOARD_UPDATE_SECONDS", "10"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_invalid_values_fail_fast(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_config({"DISCORD_TOKEN": TOKEN, name: value})


def test_in_memory_and_directory_db_paths_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="in-memory"):
        load_config({"DISCORD_TOKEN": TOKEN, "DB_PATH": ":memory:"})
    with pytest.raises(ConfigError, match="directory"):
        load_config({"DISCORD_TOKEN": TOKEN, "DB_PATH": str(tmp_path)})
    log_file = tmp_path / "file.txt"
    log_file.write_text("x")
    with pytest.raises(ConfigError, match="LOG_DIR"):
        load_config({"DISCORD_TOKEN": TOKEN, "LOG_DIR": str(log_file)})


def test_all_errors_reported_together_without_leaking_token() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config({"DISCORD_TOKEN": "bad token", "FLUSH_INTERVAL_SECONDS": "x", "BOARD_UPDATE_SECONDS": "1"})
    text = str(excinfo.value)
    assert "DISCORD_TOKEN" in text and "FLUSH_INTERVAL_SECONDS" in text and "BOARD_UPDATE_SECONDS" in text
    assert "bad token" not in text


def test_token_not_in_repr() -> None:
    cfg = load_config({"DISCORD_TOKEN": TOKEN})
    assert TOKEN not in repr(cfg)


def test_secret_redactor_scrubs_formatted_messages() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "connecting with %s now", (TOKEN,), None)
    SecretRedactor([TOKEN]).filter(record)
    assert TOKEN not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_setup_logging_writes_rotating_file_without_secrets(tmp_path: Path) -> None:
    cfg = load_config({"DISCORD_TOKEN": TOKEN, "LOG_DIR": str(tmp_path / "logs"), "DB_PATH": str(tmp_path / "db")})
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(cfg)
        logging.getLogger("bot.test").info("token is %s", TOKEN)
        for handler in root.handlers:
            handler.flush()
        content = (tmp_path / "logs" / "leaderboard.log").read_text()
        assert "token is [REDACTED]" in content
        assert TOKEN not in content
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved[0]:
            root.addHandler(handler)
        root.setLevel(saved[1])


def _storage_config(tmp_path: Path):
    return load_config({"DISCORD_TOKEN": TOKEN, "DB_PATH": str(tmp_path / "lb.db"), "LOG_DIR": str(tmp_path / "logs")})


def _deny_access(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """Make os.access report ``denied`` as unusable (file modes are ignored when tests run as root)."""
    real_access = os.access
    blocked = {str(path) for path in denied}
    monkeypatch.setattr(os, "access", lambda path, mode, **kw: str(path) not in blocked and real_access(path, mode, **kw))


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_prepare_storage_reports_unwritable_sqlite_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    cfg = _storage_config(tmp_path)
    cfg.db_path.write_text("")
    sidecar = tmp_path / f"lb.db{suffix}"
    sidecar.write_text("")
    _deny_access(monkeypatch, sidecar)
    with pytest.raises(ConfigError, match=f"lb.db{suffix} is not readable and writable") as excinfo:
        prepare_storage(cfg)
    assert "lb.db is not readable" not in str(excinfo.value)  # only the offending file is reported


def test_prepare_storage_reports_every_unusable_database_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _storage_config(tmp_path)
    files = [tmp_path / name for name in ("lb.db", "lb.db-wal", "lb.db-shm")]
    for path in files:
        path.write_text("")
    _deny_access(monkeypatch, *files)
    with pytest.raises(ConfigError) as excinfo:
        prepare_storage(cfg)
    for path in files:
        assert f"{path} is not readable and writable" in str(excinfo.value)


def test_prepare_storage_accepts_missing_or_writable_sqlite_files(tmp_path: Path) -> None:
    cfg = _storage_config(tmp_path)
    prepare_storage(cfg)  # fresh install: no database and no sidecars yet
    for name in ("lb.db", "lb.db-wal", "lb.db-shm", "lb.db-journal"):
        (tmp_path / name).write_text("")
    prepare_storage(cfg)
