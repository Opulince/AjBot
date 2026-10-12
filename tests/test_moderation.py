"""Moderation: storage, Discord operations, safety rules, commands and the mod log."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.clock import UTC
from bot.discord_ops import DiscordPyOps, MemberRank, OpForbidden, PurgeResult, rank_of, role_rank
from tests.conftest import CHANNEL, GUILD, T0
from tests.fakes import App, FakeDiscord, snowflake_at

MOD = 20
TARGET = 11
OTHER_GUILD = 2


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ================================================================ storage


async def test_case_numbers_are_per_guild_and_sequential(app: App) -> None:
    repo = app.repo
    a = await repo.create_case(GUILD, "kick", target_id=TARGET, moderator_id=MOD, reason="spam", now=T0)
    b = await repo.create_case(GUILD, "ban", target_id=12, moderator_id=MOD, reason=None, now=T0,
                               duration_seconds=3600, extra={"delete_message_days": 1})
    c = await repo.create_case(OTHER_GUILD, "warn", target_id=TARGET, moderator_id=MOD, reason="x", now=T0)
    assert (a.case_id, b.case_id, c.case_id) == (1, 2, 1)
    got = await repo.get_case(GUILD, 2)
    assert got is not None and got.action == "ban" and got.duration_seconds == 3600
    assert got.extra == {"delete_message_days": 1} and got.active and got.created_at == T0
    assert await repo.get_case(GUILD, 99) is None


async def test_concurrent_cases_get_unique_numbers(app: App) -> None:
    cases = await asyncio.gather(*(
        app.repo.create_case(GUILD, "warn", target_id=TARGET, moderator_id=MOD, reason=str(i), now=T0)
        for i in range(30)
    ))
    assert sorted(c.case_id for c in cases) == list(range(1, 31))


async def test_invalid_case_action_is_rejected_in_code_and_schema(app: App) -> None:
    with pytest.raises(ValueError):
        await app.repo.create_case(GUILD, "nuke", target_id=1, moderator_id=MOD, reason=None, now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        async with app.services.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO mod_cases (guild_id, case_id, action, moderator_id, created_at) VALUES (1, 1, 'nuke', 1, 'x')"
            )


async def test_warnings_count_clear_and_never_delete(app: App) -> None:
    repo = app.repo
    w1, n1 = await repo.create_warning(GUILD, TARGET, moderator_id=MOD, reason="one", now=T0)
    await repo.create_case(GUILD, "kick", target_id=TARGET, moderator_id=MOD, reason=None, now=T0)
    w2, n2 = await repo.create_warning(GUILD, TARGET, moderator_id=MOD, reason="two", now=T0)
    _, other = await repo.create_warning(GUILD, 12, moderator_id=MOD, reason="else", now=T0)
    assert (n1, n2, other) == (1, 2, 1)
    assert [c.case_id for c in await repo.active_warnings(GUILD, TARGET)] == [w1.case_id, w2.case_id]
    cleared = await repo.clear_warning(GUILD, w1.case_id)
    assert cleared is not None and not cleared.active
    assert await repo.clear_warning(GUILD, w1.case_id) is None  # already cleared
    assert await repo.clear_warning(GUILD, 2) is None  # case 2 is a kick, not a warning
    _, n3 = await repo.create_warning(GUILD, TARGET, moderator_id=MOD, reason="three", now=T0)
    assert n3 == 2  # cleared warnings don't count
    assert await repo.clear_warnings(GUILD, TARGET) == 2
    assert await repo.active_warnings(GUILD, TARGET) == []
    assert (await repo.get_case(GUILD, w1.case_id)) is not None  # history kept


async def test_case_reason_and_modlog_reference(app: App) -> None:
    case = await app.repo.create_case(GUILD, "kick", target_id=TARGET, moderator_id=MOD, reason="old", now=T0)
    await app.repo.set_case_modlog(GUILD, case.case_id, 300, 4444)
    updated = await app.repo.update_case_reason(GUILD, case.case_id, "new")
    assert updated is not None and updated.reason == "new"
    assert (updated.modlog_channel_id, updated.modlog_message_id) == (300, 4444)
    assert await app.repo.update_case_reason(GUILD, 77, "x") is None


async def test_leaderboard_bans_storage(app: App) -> None:
    repo = app.repo
    assert await repo.add_lb_ban(GUILD, TARGET, moderator_id=MOD, reason="farming", now=T0)
    assert not await repo.add_lb_ban(GUILD, TARGET, moderator_id=MOD, reason="again", now=T0)
    await repo.add_lb_ban(OTHER_GUILD, 12, moderator_id=MOD, reason=None, now=T0)
    [ban] = await repo.list_lb_bans(GUILD)
    assert (ban.user_id, ban.reason, ban.moderator_id) == (TARGET, "farming", MOD)
    assert await repo.load_all_lb_bans() == {GUILD: {TARGET}, OTHER_GUILD: {12}}
    assert await repo.remove_lb_ban(GUILD, TARGET)
    assert not await repo.remove_lb_ban(GUILD, TARGET)


async def test_delete_counts_range_and_all(app: App) -> None:
    await app.messages(GUILD, TARGET, dt(2026, 3, 1, 12), 2)
    await app.messages(GUILD, TARGET, dt(2026, 3, 10, 12), 3)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 12), 1)
    assert await app.repo.delete_counts(GUILD, TARGET, "2026-03-09", "2026-03-16") == 1
    totals = {r.user_id: r.total for r in await app.repo.aggregate_counts(GUILD)}
    assert totals == {TARGET: 2, 12: 1}
    assert await app.repo.delete_counts(GUILD, TARGET, None, None) == 1
    assert {r.user_id for r in await app.repo.aggregate_counts(GUILD)} == {12}
    with pytest.raises(ValueError):
        await app.repo.delete_counts(GUILD, TARGET, "2026-03-01", None)


async def test_warn_thresholds_storage(app: App) -> None:
    repo = app.repo
    await repo.set_warn_threshold(GUILD, 3, 600)
    await repo.set_warn_threshold(GUILD, 5, 3600)
    await repo.set_warn_threshold(GUILD, 3, 900)  # replaced
    assert [(t.warn_count, t.timeout_seconds) for t in await repo.list_warn_thresholds(GUILD)] == [(3, 900), (5, 3600)]
    assert await repo.get_warn_threshold(GUILD, 3) == 900 and await repo.get_warn_threshold(GUILD, 4) is None
    assert await repo.remove_warn_threshold(GUILD, 3) and not await repo.remove_warn_threshold(GUILD, 3)
    with pytest.raises(sqlite3.IntegrityError):  # longer than Discord's 28-day maximum
        await repo.set_warn_threshold(GUILD, 7, 29 * 86400)
    with pytest.raises(sqlite3.IntegrityError):
        await repo.set_warn_threshold(GUILD, 0, 60)


async def test_scheduled_unbans_storage(app: App) -> None:
    repo = app.repo
    await repo.schedule_unban(GUILD, TARGET, T0 + timedelta(hours=1), case_id=4)
    await repo.schedule_unban(GUILD, 12, T0 + timedelta(hours=2), case_id=5)
    assert await repo.due_unbans(T0) == []
    [due] = await repo.due_unbans(T0 + timedelta(hours=1))
    assert (due.user_id, due.case_id, due.attempts) == (TARGET, 4, 0)
    failed = await repo.record_unban_error(GUILD, TARGET, "forbidden: x", T0 + timedelta(hours=3))
    assert failed is not None and failed.attempts == 1 and failed.last_error == "forbidden: x"
    assert [u.user_id for u in await repo.due_unbans(T0 + timedelta(hours=2))] == [12]  # TARGET backs off
    assert {u.user_id for u in await repo.due_unbans(T0 + timedelta(hours=3))} == {TARGET, 12}
    # Re-banning restarts the clock and the retry state.
    await repo.schedule_unban(GUILD, TARGET, T0 + timedelta(days=1), case_id=9)
    again = await repo.get_scheduled_unban(GUILD, TARGET)
    assert again is not None and (again.case_id, again.attempts, again.next_attempt_at) == (9, 0, None)
    # Completing a stale snapshot (the user was re-banned meanwhile) does nothing.
    assert await repo.complete_scheduled_unban(due, moderator_id=999, reason="expired", now=T0) is None
    assert await repo.get_scheduled_unban(GUILD, TARGET) is not None
    current = await repo.get_scheduled_unban(GUILD, 12)
    assert current is not None
    case = await repo.complete_scheduled_unban(current, moderator_id=999, reason="Tempban expired", now=T0)
    assert case is not None and case.action == "unban" and case.target_id == 12 and case.extra == {"ban_case": 5}
    assert await repo.get_scheduled_unban(GUILD, 12) is None
    assert await repo.delete_scheduled_unban(GUILD, TARGET) and not await repo.delete_scheduled_unban(GUILD, TARGET)


async def test_guild_settings(app: App) -> None:
    repo = app.repo
    await repo.set_modlog_channel(GUILD, 300, T0)
    await repo.set_count_cooldown(GUILD, 5, T0)
    await repo.set_winner_dm(GUILD, True, T0)
    cfg = await repo.get_guild_config(GUILD)
    assert cfg is not None and (cfg.modlog_channel_id, cfg.count_cooldown_seconds, cfg.winner_dm) == (300, 5, True)
    assert not cfg.installed  # settings alone never install the leaderboard
    assert await repo.load_all_cooldowns() == {GUILD: 5}
    await repo.set_modlog_channel(GUILD, None, T0)
    await repo.set_count_cooldown(GUILD, 0, T0)
    cfg = await repo.get_guild_config(GUILD)
    assert cfg is not None and cfg.modlog_channel_id is None and cfg.count_cooldown_seconds == 0
    assert await repo.load_all_cooldowns() == {}


# ====================================================== discord operations


async def test_fake_member_rank_follows_discord_hierarchy(fake: FakeDiscord) -> None:
    fake.create_role(GUILD, 70, position=20)
    fake.create_role(GUILD, 60, position=20)  # same position, lower ID: ranks higher
    fake.add_member(GUILD, 30, roles=(70,))
    fake.add_member(GUILD, 31, roles=(60,))
    fake.guilds[GUILD].owner_id = 32
    fake.add_member(GUILD, 32)
    a, b = await fake.member_rank(GUILD, 30), await fake.member_rank(GUILD, 31)
    assert b.top_role > a.top_role
    owner = await fake.member_rank(GUILD, 32)
    assert owner.owner and owner.administrator and owner.top_role == (0, -GUILD)
    assert (await fake.member_rank(GUILD, fake.bot_user_id)).top_role > b.top_role


async def test_fake_enforces_discord_rules(fake: FakeDiscord, clock: Any) -> None:
    fake.add_member(GUILD, 40, administrator=True)
    with pytest.raises(OpForbidden):
        await fake.timeout_member(GUILD, 40, clock.now() + timedelta(minutes=5), reason="r")
    fake.create_role(GUILD, 80, position=150)  # above the bot
    fake.add_member(GUILD, 41, roles=(80,))
    with pytest.raises(OpForbidden):
        await fake.kick_member(GUILD, 41, reason="r")
    await fake.ban_user(GUILD, 4242, delete_message_seconds=0, reason="not a member")  # bans need no membership
    assert 4242 in fake.guilds[GUILD].bans


async def test_fake_purge_skips_old_and_pinned(fake: FakeDiscord, clock: Any) -> None:
    old = fake.add_user_message(CHANNEL, TARGET, at=clock.now() - timedelta(days=20))
    pinned = fake.add_user_message(CHANNEL, TARGET, pinned=True)
    other = fake.add_user_message(CHANNEL, 12)
    mine = [fake.add_user_message(CHANNEL, TARGET) for _ in range(3)]
    result = await fake.purge(CHANNEL, limit=100, author_id=TARGET, not_before=clock.now() - timedelta(days=14),
                              reason="r")
    assert result == PurgeResult(deleted=3, skipped_old=1, skipped_pinned=1)
    remaining = {m.id for m in fake.messages(CHANNEL)}
    assert {old, pinned, other} <= remaining and not (set(mine) & remaining)


# --------------------------------------------- the production implementation


class _World:
    """A real discord.py guild with roles and members built from gateway-shaped payloads."""

    def __init__(self) -> None:
        self.client = discord.Client(intents=discord.Intents(guilds=True))
        self.state = self.client._connection
        roles = [
            {"id": str(GUILD), "name": "@everyone", "position": 0, "permissions": "0"},
            {"id": "70", "name": "mods", "position": 5, "permissions": "0"},
            {"id": "60", "name": "same-pos-lower-id", "position": 5, "permissions": "0"},
            {"id": "90", "name": "admins", "position": 8, "permissions": str(discord.Permissions(administrator=True).value)},
        ]
        self.guild = discord.Guild(
            data={"id": str(GUILD), "name": "g", "owner_id": "500", "roles": roles, "channels": [], "members": [],
                  "threads": []},
            state=self.state,
        )

    def member(self, user_id: int, roles: list[int], *, timed_out: str | None = None) -> discord.Member:
        data = {
            "user": {"id": str(user_id), "username": f"u{user_id}", "discriminator": "0", "avatar": None},
            "roles": [str(r) for r in roles],
            "joined_at": "2026-01-01T00:00:00+00:00",
            "deaf": False,
            "mute": False,
            "flags": 0,
            "communication_disabled_until": timed_out,
        }
        return discord.Member(data=data, guild=self.guild, state=self.state)


def test_rank_of_real_members_matches_discord_ordering() -> None:
    world = _World()
    plain = rank_of(world.member(1, []))
    mod = rank_of(world.member(2, [70]))
    tie = rank_of(world.member(3, [60]))
    admin = rank_of(world.member(4, [70, 90], timed_out="2026-03-10T13:00:00+00:00"))
    owner = rank_of(world.member(500, []))
    assert plain.top_role == (0, -GUILD) and not plain.administrator and not plain.owner
    assert tie.top_role > mod.top_role  # same position: lower role ID ranks higher, like discord.Role.__lt__
    assert (world.guild.get_role(60) > world.guild.get_role(70)) is True
    assert role_rank(world.guild.get_role(60)) > role_rank(world.guild.get_role(70))
    assert admin.administrator and admin.top_role > tie.top_role and admin.timed_out_until == dt(2026, 3, 10, 13)
    assert owner.owner and owner.administrator


class _Http:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def edit_member(self, guild_id: int, user_id: int, *, reason: str | None = None, **fields: Any) -> None:
        self.calls.append(("edit_member", guild_id, user_id, reason, fields))

    async def kick(self, user_id: int, guild_id: int, reason: str | None = None) -> None:
        self.calls.append(("kick", user_id, guild_id, reason))

    async def ban(self, user_id: int, guild_id: int, delete_message_seconds: int = 86400, reason: str | None = None) -> None:
        self.calls.append(("ban", user_id, guild_id, delete_message_seconds, reason))

    async def unban(self, user_id: int, guild_id: int, *, reason: str | None = None) -> None:
        self.calls.append(("unban", user_id, guild_id, reason))

    async def delete_message(self, channel_id: int, message_id: int, *, reason: str | None = None) -> None:
        self.calls.append(("delete_message", channel_id, message_id, reason))

    async def delete_messages(self, channel_id: int, ids: list[int], *, reason: str | None = None) -> None:
        self.calls.append(("delete_messages", channel_id, list(ids), reason))


async def test_production_ops_pass_reasons_and_formats_to_discord() -> None:
    http = _Http()
    ops = DiscordPyOps(SimpleNamespace(http=http))  # type: ignore[arg-type]
    until = dt(2026, 3, 10, 14)
    await ops.timeout_member(GUILD, TARGET, until, reason="spam")
    await ops.timeout_member(GUILD, TARGET, None, reason="lifted")
    await ops.kick_member(GUILD, TARGET, reason="bye")
    await ops.ban_user(GUILD, TARGET, delete_message_seconds=3600, reason="ban")
    await ops.unban_user(GUILD, TARGET, reason="unban")
    assert http.calls == [
        ("edit_member", GUILD, TARGET, "spam", {"communication_disabled_until": "2026-03-10T14:00:00+00:00"}),
        ("edit_member", GUILD, TARGET, "lifted", {"communication_disabled_until": None}),
        ("kick", TARGET, GUILD, "bye"),
        ("ban", TARGET, GUILD, 3600, "ban"),
        ("unban", TARGET, GUILD, "unban"),
    ]


async def test_production_purge_filters_then_bulk_deletes() -> None:
    now = dt(2026, 3, 10, 12)

    def msg(author: int, at: datetime, *, pinned: bool = False, mtype: discord.MessageType = discord.MessageType.default,
            seq: int = 0) -> Any:
        return SimpleNamespace(id=snowflake_at(at, seq), author=SimpleNamespace(id=author), pinned=pinned, type=mtype)

    history = [
        msg(TARGET, now, seq=1),
        msg(12, now, seq=2),
        msg(TARGET, now - timedelta(minutes=1), pinned=True, seq=3),
        msg(TARGET, now - timedelta(minutes=2), mtype=discord.MessageType.thread_starter_message, seq=4),
        msg(TARGET, now - timedelta(days=3), seq=5),
        msg(TARGET, now - timedelta(days=15), seq=6),
    ]

    class Channel(discord.abc.Messageable):
        async def _get_channel(self) -> Any:
            return self

        async def history(self, *, limit: int) -> Any:  # type: ignore[override]
            for m in history[:limit]:
                yield m

    http = _Http()
    ops = DiscordPyOps(SimpleNamespace(http=http, get_channel=lambda _cid: Channel()))  # type: ignore[arg-type]
    result = await ops.purge(CHANNEL, limit=100, author_id=TARGET, not_before=now - timedelta(days=14), reason="clean")
    assert result == PurgeResult(deleted=2, skipped_old=1, skipped_pinned=1)
    assert http.calls == [("delete_messages", CHANNEL, [history[0].id, history[4].id], "clean")]
    http.calls.clear()
    single = await ops.purge(CHANNEL, limit=1, author_id=None, not_before=now - timedelta(days=14), reason="one")
    assert single.deleted == 1 and http.calls == [("delete_message", CHANNEL, history[0].id, "one")]


async def test_production_dm_returns_false_for_closed_dms() -> None:
    sent: list[dict[str, Any]] = []

    class DM:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def send(self, **kwargs: Any) -> None:
            if self.fail:
                response = SimpleNamespace(status=403, reason="Forbidden")
                raise discord.Forbidden(response, {"code": 50007, "message": "Cannot send messages to this user"})
            sent.append(kwargs)

    async def create_dm(user: Any) -> DM:
        return DM(fail=user.id == 13)

    ops = DiscordPyOps(SimpleNamespace(create_dm=create_dm))  # type: ignore[arg-type]
    assert await ops.dm_user(12, content="hi") is True
    assert sent[0]["content"] == "hi" and sent[0]["allowed_mentions"].to_dict()["parse"] == []
    assert await ops.dm_user(13, content="hi") is False


def test_member_rank_is_frozen_and_comparable() -> None:
    a = MemberRank(1, (5, -70), owner=False, administrator=False)
    assert a.top_role > (5, -71) and not (a.top_role > (5, -70))
