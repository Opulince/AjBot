"""Real signal handling: SIGTERM / SIGINT trigger the graceful shutdown path.

A child Python process runs the real ``run_bot`` entry point. Only the network
parts are replaced: ``start`` performs the setup hook without logging in, records
a buffered message, then sends the process a real signal. The test checks the
exit code and that the buffered count reached SQLite (the final flush ran).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import aiohttp
import discord
import pytest

from bot import main as m
from bot.config import load_config
from bot.db.database import DatabaseClosedError, DatabaseError

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


def test_dotenv_is_not_searched_in_parent_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from bot.main import load_env_file

    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / ".env").write_text("LB_TEST_PARENT_ONLY=parent\nLB_TEST_BOTH=parent\n")  # another app's .env
    (work / ".env").write_text("LB_TEST_CWD_ONLY=cwd\nLB_TEST_BOTH=cwd\nLB_TEST_PRESET=file\n")
    monkeypatch.chdir(work)
    keys = ("LB_TEST_PARENT_ONLY", "LB_TEST_CWD_ONLY", "LB_TEST_BOTH")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LB_TEST_PRESET", "environment")
    try:
        load_env_file()
        assert os.environ.get("LB_TEST_CWD_ONLY") == "cwd"
        assert os.environ.get("LB_TEST_BOTH") == "cwd"
        assert "LB_TEST_PARENT_ONLY" not in os.environ
        assert os.environ["LB_TEST_PRESET"] == "environment"  # real environment variables take precedence
    finally:
        for key in keys:
            os.environ.pop(key, None)


def test_missing_dotenv_in_the_working_directory_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import os

    from bot.main import load_env_file

    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / ".env").write_text("LB_TEST_PARENT_ONLY=parent\n")
    monkeypatch.chdir(work)
    monkeypatch.delenv("LB_TEST_PARENT_ONLY", raising=False)
    try:
        with caplog.at_level("DEBUG"):
            load_env_file()  # must neither raise nor warn
        assert "LB_TEST_PARENT_ONLY" not in os.environ
        assert not caplog.records
    finally:
        os.environ.pop("LB_TEST_PARENT_ONLY", None)


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


# ------------------------------------------------- shutdown failures and stop-caused errors


def _config(tmp_path: Path):
    return load_config({"DISCORD_TOKEN": "fake.token.value", "DB_PATH": str(tmp_path / "lb.db"), "LOG_DIR": str(tmp_path / "logs")})


async def _run_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start, bots: list) -> int:
    """The real run_bot with only the network part (``start``) replaced; ``bots`` receives the bot."""

    async def recording_start(self, token, *, reconnect=True):
        bots.append(self)
        await start(self)

    monkeypatch.setattr(m.LeaderboardBot, "start", recording_start)
    return await m.run_bot(_config(tmp_path))


def _stop_then_fail(exc: BaseException):
    """A ``start`` that connects the database, gets a stop request (as request_stop() does) and then fails with ``exc``."""

    async def start(self) -> None:
        await self.db.connect()
        self._stop_requested = True
        await self.close()  # the shutdown the request triggers
        raise exc

    return start


async def test_backstop_closes_database_when_the_shutdown_task_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a raising bot.close() used to skip the backstop and leave aiosqlite's thread alive (hang at exit)."""

    async def start(self) -> None:
        await self.db.connect()

    async def failing_shutdown(self) -> None:  # fails before it could reach its own db.close()
        raise RuntimeError("shutdown exploded")

    monkeypatch.setattr(m.LeaderboardBot, "_shutdown", failing_shutdown)
    bots: list = []
    with pytest.raises(RuntimeError, match="shutdown exploded"):
        await _run_bot(tmp_path, monkeypatch, start, bots)
    assert not bots[0].db.connected


async def test_shutdown_error_does_not_replace_the_startup_failure_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing_shutdown(self) -> None:
        self._closing = True
        await discord.Client.close(self)  # the gateway part finished, so `async with bot` exits quietly...
        raise RuntimeError("final flush exploded")  # ...but the rest of the shutdown fails

    monkeypatch.setattr(m.LeaderboardBot, "_shutdown", failing_shutdown)

    async def start(self) -> None:
        await self.db.connect()
        shutdown = asyncio.ensure_future(self.close())  # e.g. a signal handler's request_stop()
        await asyncio.sleep(0.05)
        assert isinstance(shutdown.exception(), RuntimeError)  # also marks it retrieved
        raise OSError("disk went away")  # the more important failure

    bots: list = []
    with caplog.at_level("ERROR", logger="bot"):
        with pytest.raises(OSError, match="disk went away"):
            await _run_bot(tmp_path, monkeypatch, start, bots)
    assert not bots[0].db.connected  # the backstop still ran
    assert any("final flush exploded" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)  # reported, not lost


@pytest.mark.parametrize("failing_step", ["buffer_flush", "final_flush"])
async def test_shutdown_closes_database_when_the_final_flush_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_step: str
) -> None:
    bot = m.LeaderboardBot(_config(tmp_path))
    await bot.db.connect()

    async def exploding_flush() -> bool:
        raise RuntimeError("flush exploded")

    bot.services = types.SimpleNamespace(buffer=types.SimpleNamespace(flush=exploding_flush, pending_messages=0))  # type: ignore[assignment]
    if failing_step == "final_flush":
        monkeypatch.setattr(bot, "_final_flush", exploding_flush)
    with pytest.raises(RuntimeError, match="flush exploded"):
        await bot.close()
    assert not bot.db.connected  # an open connection's worker thread would keep the process alive
    with pytest.raises(RuntimeError, match="flush exploded"):  # every caller sees the same outcome
        await bot.close()


def _attribute_error(obj: object, name: str) -> AttributeError:
    try:
        getattr(obj, name)
    except AttributeError as exc:
        return exc
    raise AssertionError(f"{obj!r} unexpectedly has {name!r}")


# What a stop request is known to cause in a half-started bot. The first entry is ours; the rest are
# discord.py 2.7.1 / aiohttp internals (see _caused_by_stop and test_sigterm_while_real_discord_py_...).
STOP_CAUSED_ERRORS = {
    "database-closed": lambda: DatabaseClosedError("Database is closed (shutdown in progress)"),
    "connector-closed": lambda: aiohttp.ClientConnectionError("Connector is closed."),
    "session-closed": lambda: RuntimeError("Session is closed"),
    # Client.connect() reads self.ws.sequence while self.ws is still None (the first handshake was cut off)
    "gateway-handshake-cut-off": lambda: _attribute_error(None, "sequence"),
}


@pytest.mark.parametrize("make_error", STOP_CAUSED_ERRORS.values(), ids=STOP_CAUSED_ERRORS.keys())
async def test_errors_caused_by_the_stop_request_exit_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, make_error
) -> None:
    bots: list = []
    with caplog.at_level("INFO", logger="bot"):
        assert await _run_bot(tmp_path, monkeypatch, _stop_then_fail(make_error()), bots) == 0
    assert "Startup interrupted by a shutdown request" in caplog.text
    assert not bots[0].db.connected


UNRELATED_ERRORS = {
    "schema-newer-than-supported": lambda: DatabaseError("Database schema version 9 is newer than this bot supports (1)"),
    "corrupt-database": lambda: sqlite3.DatabaseError("database disk image is malformed"),
    "oserror": lambda: OSError("network unreachable"),
    "server-disconnected": lambda: aiohttp.ServerDisconnectedError("Server disconnected"),
    "connection-lost": lambda: aiohttp.ClientConnectionError("Connection lost: Connection reset by peer"),
    "database-never-connected":lambda: DatabaseError("Database is not connected"),
    "other-runtime-error": lambda: RuntimeError("Database lock is not re-entrant; nested transaction detected"),
    "other-attribute-error": lambda: _attribute_error(None, "flush"),
    "sequence-attribute-of-something-else": lambda: _attribute_error(object(), "sequence"),
}


@pytest.mark.parametrize("make_error", UNRELATED_ERRORS.values(), ids=UNRELATED_ERRORS.keys())
async def test_unrelated_errors_propagate_even_after_a_stop_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_error
) -> None:
    """A SIGTERM must not turn a genuine failure into a clean exit (and a missing restart)."""
    bots: list = []
    with pytest.raises(Exception) as excinfo:
        await _run_bot(tmp_path, monkeypatch, _stop_then_fail(make_error()), bots)
    assert type(excinfo.value) is type(make_error())
    assert not bots[0].db.connected  # still shut down cleanly


@pytest.mark.parametrize("make_error", STOP_CAUSED_ERRORS.values(), ids=STOP_CAUSED_ERRORS.keys())
async def test_stop_caused_errors_without_a_stop_request_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_error
) -> None:
    async def start(self) -> None:
        raise make_error()  # nobody asked the bot to stop, so this is a real failure

    with pytest.raises(Exception) as excinfo:
        await _run_bot(tmp_path, monkeypatch, start, [])
    assert type(excinfo.value) is type(make_error())


UNRELATED_AFTER_STOP_CHILD = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {repo!r})
    from bot import main as m
    from bot.db.database import DatabaseError

    async def start(self, token, *, reconnect=True):
        await self.db.connect()
        self._stop_requested = True   # what the SIGTERM handler does...
        await self.close()
        raise DatabaseError("Database schema version 9 is newer than this bot supports (1)")  # ...then a real failure

    m.LeaderboardBot.start = start
    sys.exit(m.main())
    """
)


def test_unrelated_failure_after_a_stop_request_exits_nonzero_with_a_traceback(tmp_path: Path) -> None:
    env = {**os.environ, "DISCORD_TOKEN": "fake.token.value", "DB_PATH": str(tmp_path / "lb.db"), "LOG_DIR": str(tmp_path / "logs")}
    script = UNRELATED_AFTER_STOP_CHILD.format(repo=str(REPO))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=env, cwd=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    log_text = (tmp_path / "logs" / "leaderboard.log").read_text()
    assert "Fatal error; the bot stopped" in log_text
    assert "newer than this bot supports" in log_text and "Traceback" in log_text
    assert "Startup interrupted by a shutdown request" not in log_text


REAL_DISCORD_PY_CHILD = textwrap.dedent(
    """
    import asyncio, json, os, signal, sys
    sys.path.insert(0, {repo!r})
    import yarl
    from aiohttp import web
    from discord.gateway import DiscordWebSocket
    from discord.http import Route
    from bot import main as m
    from bot.config import load_config

    USER = {{"id": "1", "username": "b", "discriminator": "0", "avatar": None, "bot": True, "global_name": None}}
    APP = {{"id": "1", "name": "n", "description": "", "icon": None, "bot_public": True, "bot_require_code_grant": False,
           "owner": USER, "verify_key": "x", "flags": 0, "team": None}}

    def stop_soon(delay):  # SIGTERM tied to a startup stage, not a wall-clock guess (slow CI machines)
        asyncio.get_running_loop().call_later(delay, os.kill, os.getpid(), signal.SIGTERM)

    def reply(obj):  # discord.py only decodes a body whose content type is exactly application/json
        return web.Response(body=json.dumps(obj).encode(), content_type="application/json")

    async def login(request):
        if {mode!r} == "login":
            stop_soon(0.1)              # the request is in flight: the stop lands while login() waits
            await asyncio.sleep(3600)   # Discord never answers
        return reply(USER)

    async def application(request):
        return reply(APP)

    async def commands(request):
        return reply([])

    async def gateway(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if {mode!r} == "gateway":
            stop_soon(0.2)              # the client is waiting for HELLO: the stop lands mid-handshake
        await asyncio.sleep(3600)       # accepts the websocket but never sends HELLO
        return ws

    async def main():
        app = web.Application()
        app.router.add_get("/api/v10/users/@me", login)
        app.router.add_get("/api/v10/oauth2/applications/@me", application)
        app.router.add_put("/api/v10/applications/{{app_id}}/commands", commands)
        app.router.add_get("/gateway", gateway)
        runner = web.AppRunner(app, shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        Route.BASE = "http://127.0.0.1:%d/api/v10" % port
        DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL("ws://127.0.0.1:%d/gateway" % port)
        config = load_config({{"DISCORD_TOKEN": "fake.token.value", "DB_PATH": {db!r}, "LOG_DIR": {logs!r}}})
        m.setup_logging(config)
        stop_soon(20)  # fallback so a run that never reaches its stage fails on the log, not the timeout
        try:
            return await m.run_bot(config)
        finally:
            await runner.cleanup()

    sys.exit(asyncio.run(main()))
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize(
    ("mode", "expected_error"),
    [
        ("login", "ClientConnectionError: Connector is closed."),  # SIGTERM while GET /users/@me is in flight
        ("gateway", "AttributeError"),  # SIGTERM during the gateway handshake (discord.py reads self.ws.sequence of None)
    ],
)
def test_sigterm_while_real_discord_py_is_starting_exits_cleanly(tmp_path: Path, mode: str, expected_error: str) -> None:
    """Guards the list of stop-caused errors against the real discord.py / aiohttp (pinned) behaviour."""
    script = REAL_DISCORD_PY_CHILD.format(repo=str(REPO), mode=mode, db=str(tmp_path / "lb.db"), logs=str(tmp_path / "logs"))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log_text = (tmp_path / "logs" / "leaderboard.log").read_text()
    assert "Received SIGTERM" in log_text
    assert f"Startup interrupted by a shutdown request ({expected_error}" in log_text
    assert "Fatal error" not in log_text
    assert log_text.index("Shutting down") < log_text.index("Shutdown complete")
