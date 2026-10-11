"""Slash-command wiring, permissions, validation and the shutdown lifecycle."""

from __future__ import annotations

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



async def test_history_scan_reads_board_markers_from_author_links_and_old_footers() -> None:
    """The production history scan must find markers in the new author link and in old-style footers."""
    from bot.discord_ops import DiscordPyOps
    from bot.ui.embeds import board_marker, build_board_embed

    new_board = build_board_embed(
        guild_id=GUILD, board_type="daily", generation=3, entries=[], previous=None,
        next_reset=datetime(2026, 3, 11, tzinfo=UTC), banner_url=None, now=datetime(2026, 3, 10, 12, tzinfo=UTC),
    )
    # Rebuild from the API-shaped dict, as discord.py does for fetched messages.
    fetched_new = discord.Embed.from_dict(new_board.to_dict())
    fetched_old = discord.Embed.from_dict({"footer": {"text": f"Last updated · board:{GUILD}:weekly:2"}})
    messages = [
        SimpleNamespace(id=1, author=SimpleNamespace(id=9), content="", embeds=[fetched_new], created_at=None),
        SimpleNamespace(id=2, author=SimpleNamespace(id=9), content="", embeds=[fetched_old], created_at=None),
        SimpleNamespace(id=3, author=SimpleNamespace(id=9), content="hi", embeds=[discord.Embed(title="x")], created_at=None),
    ]

    class History(discord.abc.Messageable):
        async def _get_channel(self) -> Any:
            return self

        async def history(self, **_kwargs: Any) -> Any:  # type: ignore[override]
            for message in messages:
                yield message

    channel = History()
    ops = DiscordPyOps(SimpleNamespace(get_channel=lambda _cid: channel))  # type: ignore[arg-type]
    found = {m.id: m for m in await ops.recent_history(5, after=datetime(2026, 3, 10, tzinfo=UTC), limit=10)}
    assert found[1].contains_marker(board_marker(GUILD, "daily", 3))
    assert not found[1].contains_marker(board_marker(GUILD, "daily", 30))
    assert found[2].contains_marker(board_marker(GUILD, "weekly", 2))
    assert found[3].links == () and not found[3].contains_marker(board_marker(GUILD, "daily", 3))

def test_exclusion_list_never_silently_drops_entries() -> None:
    from bot.commands.admin import format_exclusion_list

    lines = [f"• <#{100000000000000000 + i}> (`{100000000000000000 + i}`)" for i in range(120)]
    text = format_exclusion_list(lines, purged=2)
    assert len(text) < 2000
    shown = [line for line in text.splitlines() if line.startswith("•")]
    assert all(line in lines for line in shown)  # whole lines only, never cut mid-mention
    assert f"…and {120 - len(shown)} more not shown" in text
    assert "Removed 2 exclusion(s)" in text
    assert format_exclusion_list(lines[:3]) == "Excluded:\n" + "\n".join(lines[:3])
    assert format_exclusion_list([], purged=1).startswith("No channels or categories are excluded.")
