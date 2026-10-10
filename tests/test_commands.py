"""Slash-command wiring, permissions, validation and the shutdown lifecycle."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from bot.clock import UTC
from bot.commands.admin import (
    describe_forced_run,
    exclude_add,
    exclude_remove,
    reconcile_exclusions,
    register_admin_commands,
    set_banner,
    set_reward_role,
    validate_banner_url,
)
from bot.commands.stats import register_stats_command
from bot.db.database import Database
from bot.db.repo import Repo
from bot.main import LeaderboardBot, build_services
from tests.conftest import CHANNEL, GUILD, ROLE_DAILY, install
from tests.fakes import App, FakeDiscord, make_config


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    ["https://cdn.discordapp.com/attachments/1/2/banner.png", "https://i.imgur.com/abc.gif?x=1",
     "https://example.com:443/a.webp", "HTTPS://Example.com/x.png"],
)
def test_valid_banner_urls(url: str) -> None:
    clean, error = validate_banner_url(url)
    assert error is None and clean == url.strip()


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "empty"),
        ("http://example.com/a.png", "https"),
        ("javascript:alert(1)", "https"),
        ("ftp://example.com/a.png", "https"),
        ("https://user:pass@example.com/a.png", "username"),
        ("https://localhost/a.png", "public host"),
        ("https://127.0.0.1/a.png", "private"),
        ("https://10.0.0.5/a.png", "private"),
        ("https://[::1]/a.png", "private"),
        ("https://169.254.169.254/latest", "private"),
        ("https://printer.local/a.png", "public host"),
        ("https://intranet/a.png", "public host"),
        ("https://example.com/a b.png", "spaces"),
        ("https://example.com:8443/a.png", "port"),
        ("https://example.com:99999/a.png", "valid URL"),
        ("https://" + "a" * 2050 + ".com", "longer"),
    ],
)
def test_invalid_banner_urls(url: str, reason: str) -> None:
    clean, error = validate_banner_url(url)
    assert clean is None and error is not None and reason in error


async def _tree(services: Any) -> tuple[discord.Client, app_commands.CommandTree]:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_admin_commands(tree, services)
    register_stats_command(tree, services)
    return client, tree


async def test_command_structure_and_permissions(app: App) -> None:
    client, tree = await _tree(app.services)
    try:
        commands = {c.name: c for c in tree.get_commands()}
        assert set(commands) == {"setup", "exclude", "setrole", "setbanner", "forcereset", "stats"}
        manage = discord.Permissions(manage_guild=True)
        for name in ("setup", "exclude", "setrole", "setbanner", "forcereset"):
            assert commands[name].default_permissions == manage, name
            assert commands[name].guild_only, name
        assert commands["stats"].default_permissions is None and commands["stats"].guild_only
        exclude = commands["exclude"]
        assert isinstance(exclude, app_commands.Group)
        assert {c.name for c in exclude.commands} == {"add", "remove", "list"}
        banner = commands["setbanner"]
        assert isinstance(banner, app_commands.Group) and {c.name for c in banner.commands} == {"set", "clear"}
        # Every admin leaf command also checks Manage Server at runtime.
        leaves = [commands["setup"], commands["setrole"], commands["forcereset"], *exclude.commands, *banner.commands]
        assert all(leaf.checks for leaf in leaves)
        # /exclude accepts categories, forums and text channels.
        target = exclude.get_command("add").get_parameter("target")  # type: ignore[union-attr]
        types = set(target.channel_types)
        assert {discord.ChannelType.category, discord.ChannelType.text, discord.ChannelType.forum} <= types
        # Threads can't be picked, so exclusion IDs are always cached channels (never threads).
        assert not types & {
            discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread
        }
        period = commands["setrole"].get_parameter("period")  # type: ignore[union-attr]
        assert [c.value for c in period.choices] == ["daily", "weekly", "monthly"]
    finally:
        await client.close()


async def test_missing_permission_error_message(app: App) -> None:
    client, tree = await _tree(app.services)
    try:
        sent: list[str] = []
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=lambda message, ephemeral: _record(sent, message),
        )
        interaction = SimpleNamespace(response=response, command=None, guild_id=GUILD)
        await tree.on_error(interaction, app_commands.MissingPermissions(["manage_guild"]))  # type: ignore[arg-type]
        assert "Manage Server" in sent[0]
    finally:
        await client.close()


async def _record(sink: list[str], message: str) -> None:
    sink.append(message)


async def test_exclude_actions_update_db_and_cache(app: App) -> None:
    assert await exclude_add(app.services, GUILD, 77)
    assert not await exclude_add(app.services, GUILD, 77)
    assert await app.repo.list_exclusions(GUILD) == [77]
    assert 77 in app.services.exclusions.excluded_ids(GUILD)
    assert await exclude_remove(app.services, GUILD, 77)
    assert not await exclude_remove(app.services, GUILD, 77)
    assert 77 not in app.services.exclusions.excluded_ids(GUILD)


async def test_setrole_warns_when_role_unmanageable(app: App, fake: FakeDiscord) -> None:
    check = await set_reward_role(app.services, GUILD, "daily", ROLE_DAILY)
    assert check.ok
    fake.guilds[GUILD].roles[ROLE_DAILY].position = 500
    check = await set_reward_role(app.services, GUILD, "daily", ROLE_DAILY)
    assert not check.ok and "below" in (check.reason or "")
    cfg = await app.repo.get_guild_config(GUILD)
    assert cfg is not None and cfg.role_daily_id == ROLE_DAILY  # saved anyway, with a warning


async def test_setbanner_updates_boards(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await set_banner(app.services, GUILD, "https://cdn.example.com/x.png")
    assert all(m.embed["image"]["url"] == "https://cdn.example.com/x.png" for m in fake.board_messages(CHANNEL))
    await set_banner(app.services, GUILD, None)
    assert all("image" not in m.embed for m in fake.board_messages(CHANNEL))


async def test_forcereset_description_is_explicit(app: App, clock: Any) -> None:
    await install(app)
    clock.set(dt(2026, 3, 10, 12, 10))
    empty = await app.runner.force_reset(GUILD, "weekly")  # nobody has spoken yet
    assert "SKIPPED_EMPTY" in describe_forced_run(empty) and "Nobody eligible" in describe_forced_run(empty)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 12, 30), 2)
    clock.set(dt(2026, 3, 10, 13))
    run = await app.runner.force_reset(GUILD, "daily")
    text = describe_forced_run(run)
    assert "**test**" in text and "DONE" in text and "<@11>" in text


async def test_stats_command_ephemeral_and_zero_messages(app: App) -> None:
    client, tree = await _tree(app.services)
    try:
        await app.messages(GUILD, 11, dt(2026, 3, 10, 12, 30), 3)
        stats = tree.get_command("stats")
        sent: list[dict[str, Any]] = []

        async def send_message(**kwargs: Any) -> None:
            sent.append(kwargs)

        user = SimpleNamespace(id=11, name="user11", display_name="User Eleven")
        interaction = SimpleNamespace(guild_id=GUILD, user=user, response=SimpleNamespace(send_message=send_message))
        await stats.callback(interaction, None)  # type: ignore[union-attr]
        embed = sent[0]["embed"]
        assert sent[0]["ephemeral"] is True
        assert "**Daily:** 3 messages — rank #1" in embed.description
        assert "**All-Time:** 3 messages — rank #1" in embed.description
        other = SimpleNamespace(id=4242, name="ghost", display_name="Ghost")
        await stats.callback(interaction, other)  # type: ignore[union-attr]
        assert "**Weekly:** 0 messages — unranked" in sent[1]["embed"].description
    finally:
        await client.close()


async def test_graceful_shutdown_flushes_and_closes(tmp_path: Path, fake: FakeDiscord, clock: Any) -> None:
    config = make_config(tmp_path)
    bot = LeaderboardBot(config, clock=clock)
    await bot.db.connect()
    bot.services = build_services(config, bot.db, fake, clock)
    bot.services.buffer.record(guild_id=GUILD, user_id=11, message_id=123456789012345678,
                               created_at=dt(2026, 3, 10, 12), display_name="a", username="a")
    await bot.close()
    await bot.close()  # idempotent
    assert not bot.db.connected
    db = Database(config.db_path)
    await db.connect()
    try:
        rows = await Repo(db).aggregate_counts(GUILD)
        assert [(r.user_id, r.total) for r in rows] == [(11, 1)]
    finally:
        await db.close()


async def test_messages_ignored_while_shutting_down(tmp_path: Path, fake: FakeDiscord, clock: Any) -> None:
    config = make_config(tmp_path)
    bot = LeaderboardBot(config, clock=clock)
    await bot.db.connect()
    bot.services = build_services(config, bot.db, fake, clock)
    bot._closing = True
    await bot.on_message(SimpleNamespace(id=1))  # type: ignore[arg-type]
    assert bot.services.buffer.pending_messages == 0
    bot._closing = False
    await bot.close()


async def test_setup_hook_initialises_database_items_commands_and_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real startup path, minus the network: DB init, DynamicItems, commands, tasks."""
    config = make_config(tmp_path / "data")
    bot = LeaderboardBot(config)

    async def no_sync(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(bot.tree, "sync", no_sync)
    async with bot:  # what login() does before calling setup_hook
        await bot.setup_hook()
        assert config.db_path.exists()
        assert bot.services is not None and bot.db.connected
        patterns = {p.pattern for p in bot._connection._view_store._dynamic_items}
        assert any(p.startswith("lb:more:") for p in patterns) and any(p.startswith("lb:pg:") for p in patterns)
        assert {c.name for c in bot.tree.get_commands()} >= {"setup", "stats", "exclude", "forcereset"}
        tasks = (bot._flush_task, bot._scheduler_task, bot._board_task)
        assert all(t is not None and not t.done() for t in tasks)  # running (waiting for READY)
    assert not bot.db.connected
    assert all(t.done() for t in tasks)  # type: ignore[union-attr]
    assert all(t.cancelled() or t.exception() is None for t in tasks)  # type: ignore[union-attr]


async def test_messages_are_sent_with_an_enforced_nonce() -> None:
    """Discord dedupes discord.py's internal 5xx retries when a nonce is enforced."""
    from bot.discord_ops import DiscordPyOps

    class Capture(discord.abc.Messageable):
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def _get_channel(self) -> Any:
            return self

        async def send(self, **kwargs: Any) -> Any:  # type: ignore[override]
            self.calls.append(kwargs)
            return SimpleNamespace(id=42)

    channel = Capture()
    ops = DiscordPyOps(SimpleNamespace(get_channel=lambda _cid: channel))  # type: ignore[arg-type]
    assert await ops.send_message(5, content="a") == 42
    await ops.send_message(5, content="b")
    nonces = [c["nonce"] for c in channel.calls]
    assert all(isinstance(n, int) and 0 <= n < 2**63 and len(str(n)) <= 25 for n in nonces)
    assert nonces[0] != nonces[1]
    params = discord.http.handle_message_parameters(content="a", nonce=nonces[0])
    assert params.payload["enforce_nonce"] is True and params.payload["nonce"] == str(nonces[0])


def test_exclusion_list_never_silently_drops_entries() -> None:
    from bot.commands.admin import format_exclusion_list

    lines = [f"• <#{100000000000000000 + i}> (`{100000000000000000 + i}`)" for i in range(120)]
    text = format_exclusion_list(lines)
    assert len(text) < 2000
    shown = [line for line in text.splitlines() if line.startswith("•")]
    assert all(line in lines for line in shown)  # whole lines only, never cut mid-mention
    assert f"…and {120 - len(shown)} more not shown" in text
    assert format_exclusion_list(lines[:3]) == "Excluded:\n" + "\n".join(lines[:3])
    assert format_exclusion_list([]).startswith("No channels or categories are excluded.")


# ----------------------------------------------- /exclude list is read-only; deletes are event-driven


def _count_writes(app: App, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per write transaction opened from now on."""
    writes: list[int] = []
    db = app.services.db
    original = db.transaction

    def spy() -> Any:
        writes.append(1)
        return original()

    monkeypatch.setattr(db, "transaction", spy)
    return writes


def _guild(channels: dict[int, Any] | None = None, *, unavailable: bool = False, guild_id: int = GUILD) -> Any:
    known = channels or {}
    return SimpleNamespace(id=guild_id, unavailable=unavailable, get_channel=known.get)


def _category(name: str) -> discord.CategoryChannel:
    category = object.__new__(discord.CategoryChannel)
    category.name = name
    return category


def _deleted(channel_id: int, guild_id: int = GUILD) -> Any:
    return SimpleNamespace(id=channel_id, guild=SimpleNamespace(id=guild_id))


async def _exclude_list(app: App, guild: Any) -> str:
    client, tree = await _tree(app.services)
    try:
        sent: list[str] = []
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=lambda message, ephemeral: _record(sent, message),
        )
        interaction = SimpleNamespace(guild_id=GUILD, guild=guild, response=response)
        await tree.get_command("exclude").get_command("list").callback(interaction)  # type: ignore[union-attr]
        return sent[0]
    finally:
        await client.close()


@pytest.fixture
async def lb_bot(app: App, config: Any, clock: Any) -> Any:
    instance = LeaderboardBot(config, clock=clock)
    instance.services = app.services
    yield instance
    await instance.close()


async def test_exclude_list_shows_unresolvable_ids_without_deleting_them(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    for channel_id in (70, 71, 72, 73):
        await exclude_add(app.services, GUILD, channel_id)
    guild = _guild({72: SimpleNamespace(mention="<#72>"), 73: _category("Staff *only*")})
    writes = _count_writes(app, monkeypatch)
    text = await _exclude_list(app, guild)
    assert text.splitlines() == [
        "Excluded:",
        "• Unknown or deleted channel (`70`)",
        "• Unknown or deleted channel (`71`)",
        "• <#72> (`72`)",
        "• Category **Staff \\*only\\*** (`73`)",
    ]
    assert writes == []  # strictly read-only
    assert await app.repo.list_exclusions(GUILD) == [70, 71, 72, 73]
    assert set(app.services.exclusions.excluded_ids(GUILD)) == {70, 71, 72, 73}


@pytest.mark.parametrize("guild", [None, _guild(unavailable=True)], ids=["no-guild", "partial-uncached-guild"])
async def test_exclude_list_with_missing_or_empty_guild_lists_every_id(
    app: App, monkeypatch: pytest.MonkeyPatch, guild: Any
) -> None:
    for channel_id in (70, 71, 72):
        await exclude_add(app.services, GUILD, channel_id)
    writes = _count_writes(app, monkeypatch)
    text = await _exclude_list(app, guild)
    assert text.splitlines() == ["Excluded:", *(f"• Unknown or deleted channel (`{i}`)" for i in (70, 71, 72))]
    assert writes == []
    assert await app.repo.list_exclusions(GUILD) == [70, 71, 72]
    assert set(app.services.exclusions.excluded_ids(GUILD)) == {70, 71, 72}


async def test_channel_delete_removes_the_exclusion(app: App, lb_bot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    await exclude_add(app.services, GUILD, 77)
    await exclude_add(app.services, GUILD, 78)
    await exclude_add(app.services, 2, 79)
    await lb_bot.on_guild_channel_delete(_deleted(77))
    assert await app.repo.list_exclusions(GUILD) == [78]
    assert set(app.services.exclusions.excluded_ids(GUILD)) == {78}
    assert await app.repo.list_exclusions(2) == [79]  # other guilds are untouched
    writes = _count_writes(app, monkeypatch)
    await lb_bot.on_guild_channel_delete(_deleted(555))  # an ordinary channel: no database work at all
    assert writes == []


async def test_channel_delete_handler_is_guarded_and_never_raises(
    app: App, lb_bot: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await exclude_add(app.services, GUILD, 77)
    lb_bot._closing = True
    await lb_bot.on_guild_channel_delete(_deleted(77))
    lb_bot._closing = False
    lb_bot.services = None
    await lb_bot.on_guild_channel_delete(_deleted(77))  # before setup_hook finished
    lb_bot.services = app.services
    assert await app.repo.list_exclusions(GUILD) == [77]

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(app.repo, "remove_exclusion", broken)
    with caplog.at_level(logging.ERROR, logger="bot"):
        await lb_bot.on_guild_channel_delete(_deleted(77))
    assert "Failed to remove the exclusion of deleted channel 77" in caplog.text
    assert 77 in app.services.exclusions.excluded_ids(GUILD)  # cache still mirrors the database


async def test_events_reach_the_handlers_through_discord_py_dispatch(app: App, lb_bot: Any) -> None:
    await exclude_add(app.services, GUILD, 77)
    await exclude_add(app.services, GUILD, 78)

    async def wait_for(expected: list[int]) -> None:
        for _ in range(100):
            if await app.repo.list_exclusions(GUILD) == expected:
                return
            await asyncio.sleep(0.02)
        assert await app.repo.list_exclusions(GUILD) == expected

    async with lb_bot:  # what login() does; gives the client its event loop
        lb_bot.dispatch("guild_channel_delete", _deleted(77))
        await wait_for([78])
        lb_bot.dispatch("guild_available", _guild({}))
        await wait_for([])


async def test_reconcile_removes_only_missing_ids_in_one_batch(
    app: App, lb_bot: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for channel_id in (10, 11, 12, 13):
        await exclude_add(app.services, GUILD, channel_id)
    await exclude_add(app.services, 2, 10)  # same ID in another guild is not ours to touch
    guild = _guild({11: SimpleNamespace(), 13: SimpleNamespace()})
    batches: list[tuple[int, list[int]]] = []
    original = app.repo.remove_exclusions

    async def spy(guild_id: int, channel_ids: Any) -> int:
        batches.append((guild_id, sorted(channel_ids)))
        return await original(guild_id, channel_ids)

    async def single(*args: Any) -> bool:
        raise AssertionError("must not remove one exclusion at a time")

    monkeypatch.setattr(app.repo, "remove_exclusions", spy)
    monkeypatch.setattr(app.repo, "remove_exclusion", single)
    writes = _count_writes(app, monkeypatch)
    with caplog.at_level(logging.INFO, logger="bot.commands.admin"):
        await lb_bot.on_guild_available(guild)
    assert batches == [(GUILD, [10, 12])]
    assert len(writes) == 1  # a single transaction
    assert [r.getMessage() for r in caplog.records if "Removed" in r.getMessage()] == [
        f"Removed 2 exclusion(s) of deleted channels: guild={GUILD} channels=[10, 12]"
    ]
    assert await app.repo.list_exclusions(GUILD) == [11, 13]
    assert set(app.services.exclusions.excluded_ids(GUILD)) == {11, 13}
    assert await app.repo.list_exclusions(2) == [10]
    assert set(app.services.exclusions.excluded_ids(2)) == {10}


async def test_reconcile_ignores_an_unavailable_guild(app: App, lb_bot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for channel_id in (10, 11):
        await exclude_add(app.services, GUILD, channel_id)
    writes = _count_writes(app, monkeypatch)
    assert await reconcile_exclusions(app.services, _guild({}, unavailable=True)) == 0
    await lb_bot.on_guild_available(_guild({}, unavailable=True))
    assert writes == []  # an empty channel cache from an outage is not "everything was deleted"
    assert await app.repo.list_exclusions(GUILD) == [10, 11]
    assert set(app.services.exclusions.excluded_ids(GUILD)) == {10, 11}


async def test_reconcile_with_nothing_missing_does_no_writes(
    app: App, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await exclude_add(app.services, GUILD, 10)
    writes = _count_writes(app, monkeypatch)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bot.commands.admin"):
        assert await reconcile_exclusions(app.services, _guild({10: SimpleNamespace()})) == 0
        assert await reconcile_exclusions(app.services, _guild({}, guild_id=999)) == 0  # no exclusions at all
    assert writes == [] and caplog.records == []
    assert await app.repo.list_exclusions(GUILD) == [10]


async def test_guild_available_handler_is_guarded_and_never_raises(
    app: App, lb_bot: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await exclude_add(app.services, GUILD, 10)
    lb_bot._closing = True
    await lb_bot.on_guild_available(_guild({}))
    lb_bot._closing = False
    lb_bot.services = None
    await lb_bot.on_guild_available(_guild({}))
    lb_bot.services = app.services
    assert await app.repo.list_exclusions(GUILD) == [10]

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(app.repo, "remove_exclusions", broken)
    with caplog.at_level(logging.ERROR, logger="bot"):
        await lb_bot.on_guild_available(_guild({}))
    assert f"Failed to reconcile exclusions for guild {GUILD}" in caplog.text
    assert 10 in app.services.exclusions.excluded_ids(GUILD)
