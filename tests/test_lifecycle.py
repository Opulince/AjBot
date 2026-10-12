"""Real signal handling: SIGTERM / SIGINT trigger the graceful shutdown path.

A child Python process runs the real ``run_bot`` entry point. Only the network
parts are replaced: ``start`` performs the setup hook without logging in, records
a buffered message, then sends the process a real signal. The test checks the
exit code and that the buffered count reached SQLite (the final flush ran).
"""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    sys.path.insert(0, {repo!r})
    from datetime import datetime, timezone
    from bot import main as m
    from bot.config import load_config

    async def fake_start(self, token, *, reconnect=True):
        async def no_sync(*a, **k):
            return []
        self.tree.sync = no_sync
        await self.setup_hook()
        self.services.buffer.record(guild_id=1, user_id=11, message_id=123456789012345678,
                                    created_at=datetime(2026, 3, 10, 12, tzinfo=timezone.utc),
                                    display_name="a", username="a")
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.{sig})
        await self._stop.wait()   # "connected" until shutdown begins

    m.LeaderboardBot.start = fake_start
    config = load_config({{"DISCORD_TOKEN": "fake.token.value", "DB_PATH": {db!r}, "LOG_DIR": {logs!r}}})
    m.setup_logging(config)
    sys.exit(asyncio.run(m.run_bot(config)))
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_signal_triggers_graceful_shutdown_with_final_flush(tmp_path: Path, sig: str) -> None:
    db = tmp_path / "lb.db"
    logs = tmp_path / "logs"
    script = CHILD.format(repo=str(REPO), sig=sig, db=str(db), logs=str(logs))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT user_id, count FROM daily_counts").fetchall()
    assert rows == [(11, 1)]
    log_text = (logs / "leaderboard.log").read_text()
    assert f"Received {signal.Signals[sig].name}" in log_text
    assert log_text.index("Shutting down") < log_text.index("Final flush complete") < log_text.index("Shutdown complete")
    assert "fake.token.value" not in log_text


STARTUP_RACE_CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    sys.path.insert(0, {repo!r})
    from bot import main as m
    from bot.config import load_config
    from bot.db import database

    original = database.Database._migrate

    async def slow_migrate(self, conn):
        await asyncio.sleep(1.0)   # e.g. a first-run migration on a slow volume
        await original(self, conn)

    database.Database._migrate = slow_migrate

    async def fake_start(self, token, *, reconnect=True):
        async def no_sync(*a, **k):
            return []
        self.tree.sync = no_sync
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
        await self.setup_hook()    # the signal lands while the database is opening
        await self._stop.wait()

    m.LeaderboardBot.start = fake_start
    config = load_config({{"DISCORD_TOKEN": "fake.token.value", "DB_PATH": {db!r}, "LOG_DIR": {logs!r}}})
    m.setup_logging(config)
    sys.exit(asyncio.run(m.run_bot(config)))
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_during_startup_exits_cleanly_without_hanging(tmp_path: Path) -> None:
    """Regression: a shutdown racing Database.connect() used to leave the connection open and hang."""
    script = STARTUP_RACE_CHILD.format(repo=str(REPO), db=str(tmp_path / "lb.db"), logs=str(tmp_path / "logs"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log_text = (tmp_path / "logs" / "leaderboard.log").read_text()
    assert "Startup interrupted by a shutdown request" in log_text
    assert "Fatal error" not in log_text


def test_unusable_storage_is_a_configuration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.config import ConfigError, load_config, prepare_storage
    from bot.main import EXIT_CONFIG_ERROR, main

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    cfg = load_config({"DISCORD_TOKEN": "fake.token", "DB_PATH": str(blocker / "lb.db"), "LOG_DIR": str(tmp_path / "logs")})
    with pytest.raises(ConfigError, match="DB_PATH directory"):
        prepare_storage(cfg)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "fake.token")
    monkeypatch.setenv("LOG_DIR", str(blocker / "logs"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db" / "lb.db"))
    assert main() == EXIT_CONFIG_ERROR  # clear message and no restart loop, not a traceback


def test_dotenv_is_loaded_from_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    from bot.main import EXIT_CONFIG_ERROR, main

    (tmp_path / ".env").write_text("DISCORD_TOKEN=fake.token.value\nBOARD_UPDATE_SECONDS=5\n")
    monkeypatch.chdir(tmp_path)
    for key in ("DISCORD_TOKEN", "BOARD_UPDATE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    try:
        assert main() == EXIT_CONFIG_ERROR
        assert "BOARD_UPDATE_SECONDS must be between" in capsys.readouterr().err  # came from the cwd .env
    finally:
        for key in ("DISCORD_TOKEN", "BOARD_UPDATE_SECONDS"):
            os.environ.pop(key, None)


def test_log_level_applies_to_discord_py_logs(tmp_path: Path) -> None:
    import logging

    from bot.config import load_config
    from bot.main import setup_logging

    cfg = load_config({"DISCORD_TOKEN": "fake.token", "LOG_DIR": str(tmp_path / "logs"), "LOG_LEVEL": "ERROR",
                       "DB_PATH": str(tmp_path / "lb.db")})
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(cfg)
        logging.getLogger("discord.client").info("gateway chatter")
        logging.getLogger("discord.client").error("real problem")
        for handler in root.handlers:
            handler.flush()
        content = (tmp_path / "logs" / "leaderboard.log").read_text()
        assert "real problem" in content and "gateway chatter" not in content
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved[0]:
            root.addHandler(handler)
        root.setLevel(saved[1])


FAILING_START_CHILD = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {repo!r})
    from bot import main as m
    from bot.config import load_config

    async def failing_start(self, token, *, reconnect=True):
        raise OSError("network unreachable")   # a genuine startup failure, no signal involved

    m.LeaderboardBot.start = failing_start
    config = load_config({{"DISCORD_TOKEN": "fake.token.value", "DB_PATH": {db!r}, "LOG_DIR": {logs!r}}})
    m.setup_logging(config)
    try:
        sys.exit(asyncio.run(m.run_bot(config)))
    except OSError:
        sys.exit(1)
    """
)


def test_genuine_startup_failure_still_exits_nonzero(tmp_path: Path) -> None:
    """Supervisors must restart after real failures; only an explicit stop request exits 0."""
    script = FAILING_START_CHILD.format(repo=str(REPO), db=str(tmp_path / "lb.db"), logs=str(tmp_path / "logs"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
