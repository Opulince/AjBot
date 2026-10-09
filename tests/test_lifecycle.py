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
