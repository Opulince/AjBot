"""Shared fixtures. Tests never need a Discord token or network access."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.clock import UTC  # noqa: E402
from tests.fakes import App, FakeClock, FakeDiscord, make_config  # noqa: E402

GUILD = 1
CHANNEL = 100
ROLE_DAILY = 501
ROLE_WEEKLY = 502
ROLE_MONTHLY = 503
# Tuesday 2026-03-10 12:00 UTC (ISO week 2026-W11).
T0 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

os.environ.pop("DISCORD_TOKEN", None)


@pytest.fixture(autouse=True)
def instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-call Discord retries and board-edit spacing sleep for real in production; skip them in tests."""

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("bot.discord_ops._retry_sleep", no_sleep)
    monkeypatch.setattr("bot.boards._edit_pause", no_sleep)


@pytest.fixture(autouse=True)
async def close_leftover_apps() -> AsyncIterator[None]:
    """Close every App a test created, even if the test failed midway.

    An unclosed aiosqlite connection keeps a worker thread alive and would hang
    the interpreter at exit.
    """
    App.instances.clear()
    yield
    for instance in App.instances:
        if instance.services.db.connected:
            await instance.services.db.close()
    App.instances.clear()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def fake(clock: FakeClock) -> FakeDiscord:
    d = FakeDiscord(clock)
    d.add_guild(GUILD, bot_top_position=100)
    d.add_channel(CHANNEL)
    d.create_role(GUILD, ROLE_DAILY, position=10)
    d.create_role(GUILD, ROLE_WEEKLY, position=11)
    d.create_role(GUILD, ROLE_MONTHLY, position=12)
    for user_id in (11, 12, 13, 14, 15):
        d.add_member(GUILD, user_id)
    return d


@pytest.fixture
def config(tmp_path: Path):  # noqa: ANN201
    return make_config(tmp_path)


@pytest.fixture
async def app(config, fake: FakeDiscord, clock: FakeClock) -> AsyncIterator[App]:  # noqa: ANN001
    instance = await App.start(config, fake, clock)
    yield instance
    if instance.services.db.connected:
        await instance.services.db.close()


async def install(app: App, *, roles: bool = True) -> None:
    """Run /setup's logic and configure the three reward roles."""
    from bot.commands.admin import setup_guild

    await setup_guild(app.services, GUILD, CHANNEL)
    if roles:
        now = app.services.clock.now()
        await app.repo.set_reward_role(GUILD, "daily", ROLE_DAILY, now)
        await app.repo.set_reward_role(GUILD, "weekly", ROLE_WEEKLY, now)
        await app.repo.set_reward_role(GUILD, "monthly", ROLE_MONTHLY, now)
