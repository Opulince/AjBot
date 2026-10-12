"""Moderation: storage, Discord operations, safety rules, commands and the mod log."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot import moderation as mod
from bot.clock import UTC
from bot.commands.admin import describe_command_error
from bot.discord_ops import DiscordPyOps, MemberRank, OpForbidden, OpNetwork, PurgeResult, rank_of, role_rank
from bot.moderation import Actor, ModError
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


# ================================================================ actions
#
# Hierarchy used below (the bot's top role is at position 100):
#   OWNER (no roles) · MOD (role at 50) · TARGET=11 (role at 20) · 12-15 (no roles)

OWNER = 30
MODLOG = 300
ROLE_MOD = 600
ROLE_MEMBER = 601
ROLE_HIGH = 602


@pytest.fixture
async def staff(app: App, fake: FakeDiscord, clock: Any) -> App:
    fake.create_role(GUILD, ROLE_MOD, position=50)
    fake.create_role(GUILD, ROLE_MEMBER, position=20)
    fake.add_member(GUILD, MOD, "Mod", roles=(ROLE_MOD,))
    fake.guilds[GUILD].members[TARGET].roles.add(ROLE_MEMBER)
    fake.guilds[GUILD].owner_id = OWNER
    fake.add_member(GUILD, OWNER, "Owner")
    fake.add_channel(MODLOG)
    await app.repo.set_modlog_channel(GUILD, MODLOG, clock.now())
    return app


ACTOR = Actor(MOD, "Mod")
MOD_ACTIONS = ("timeout", "untimeout", "warn", "kick", "ban")
DISCORD_OPS = ("timeout_member", "kick_member", "ban_user", "unban_user", "purge")


async def act(app: App, action: str, target: int, actor: Actor = ACTOR, reason: str | None = "spam") -> str:
    s = app.services
    if action == "timeout":
        return await mod.timeout(s, GUILD, actor, target, timedelta(minutes=10), reason)
    if action == "untimeout":
        return await mod.untimeout(s, GUILD, actor, target, reason)
    if action == "warn":
        return await mod.warn(s, GUILD, "Test Server", actor, target, reason or "r")
    if action == "kick":
        return await mod.kick(s, GUILD, actor, target, reason)
    if action == "ban":
        return await mod.ban(s, GUILD, actor, target, reason)
    raise AssertionError(action)


def mutations(fake: FakeDiscord) -> int:
    return sum(fake.count(op) for op in DISCORD_OPS)


async def cases(app: App) -> list[Any]:
    async with app.services.db.read() as conn:
        async with conn.execute("SELECT case_id, action, target_id FROM mod_cases ORDER BY case_id") as cur:
            return [tuple(r) for r in await cur.fetchall()]


def test_parse_duration() -> None:
    assert mod.parse_duration("30s") == timedelta(seconds=30)
    assert mod.parse_duration("10m") == timedelta(minutes=10)
    assert mod.parse_duration("2h") == timedelta(hours=2)
    assert mod.parse_duration("7D") == timedelta(days=7)
    assert mod.parse_duration("1h 30m") == timedelta(minutes=90)
    assert mod.parse_duration("1w") == timedelta(days=7)
    for bad in ("", "10", "m", "10x", "-5m", "0s", "1.5h", "ten minutes", "5m; drop"):
        with pytest.raises(ModError):
            mod.parse_duration(bad)


def test_parse_user_id() -> None:
    assert mod.parse_user_id(" 123456789012345678 ") == 123456789012345678
    assert mod.parse_user_id("<@123456789012345678>") == 123456789012345678
    assert mod.parse_user_id("<@!123456789012345678>") == 123456789012345678
    for bad in ("", "abc", "12", "123456789012345678x"):
        with pytest.raises(ModError):
            mod.parse_user_id(bad)


# -------------------------------------------------------------- safety


@pytest.mark.parametrize("action", MOD_ACTIONS)
async def test_cannot_target_self_bot_or_owner(staff: App, fake: FakeDiscord, action: str) -> None:
    for target, expected in ((MOD, "yourself"), (fake.bot_user_id, "myself"), (OWNER, "server owner")):
        with pytest.raises(ModError, match=expected):
            await act(staff, action, target)
    assert mutations(fake) == 0 and await cases(staff) == [] and fake.dms == {}


@pytest.mark.parametrize("action", MOD_ACTIONS)
async def test_moderator_must_strictly_outrank_target(staff: App, fake: FakeDiscord, action: str) -> None:
    fake.guilds[GUILD].members[TARGET].roles.add(ROLE_MOD)  # same top role as the moderator
    with pytest.raises(ModError, match="below yours"):
        await act(staff, action, TARGET)
    fake.create_role(GUILD, 603, position=60)  # now above the moderator
    fake.guilds[GUILD].members[TARGET].roles.add(603)
    with pytest.raises(ModError, match="below yours"):
        await act(staff, action, TARGET)
    assert mutations(fake) == 0 and await cases(staff) == []


async def test_owner_bypasses_the_moderator_hierarchy(staff: App, fake: FakeDiscord) -> None:
    # The owner has no roles at all, but may still kick someone with a role.
    message = await mod.kick(staff.services, GUILD, Actor(OWNER, "Owner"), TARGET, "bye")
    assert "Kicked" in message and TARGET not in fake.guilds[GUILD].members


@pytest.mark.parametrize("action", ["timeout", "untimeout", "kick", "ban"])
async def test_bot_must_outrank_target_with_a_clear_message(staff: App, fake: FakeDiscord, action: str) -> None:
    fake.guilds[GUILD].roles[ROLE_MOD].position = 160  # moderator above the bot
    fake.create_role(GUILD, ROLE_HIGH, position=120)  # target between the bot (100) and the moderator
    fake.guilds[GUILD].members[TARGET].roles.add(ROLE_HIGH)
    fake.guilds[GUILD].members[TARGET].timeout_until = staff.services.clock.now() + timedelta(hours=1)
    with pytest.raises(ModError, match="My highest role isn't above .* Move my role higher"):
        await act(staff, action, TARGET)
    assert mutations(fake) == 0 and await cases(staff) == []


async def test_warn_needs_no_bot_hierarchy(staff: App, fake: FakeDiscord) -> None:
    """A warning is only a record and a DM, so the bot's role position doesn't matter."""
    fake.guilds[GUILD].roles[ROLE_MOD].position = 160
    fake.create_role(GUILD, ROLE_HIGH, position=120)
    fake.guilds[GUILD].members[TARGET].roles.add(ROLE_HIGH)
    assert "Warned" in await act(staff, "warn", TARGET)


async def test_timing_out_an_administrator_is_refused_cleanly(staff: App, fake: FakeDiscord) -> None:
    fake.guilds[GUILD].members[TARGET].administrator = True
    with pytest.raises(ModError, match="Administrator"):
        await act(staff, "timeout", TARGET)
    assert fake.count("timeout_member") == 0 and await cases(staff) == []


async def test_non_members_cannot_be_kicked_but_can_be_banned(staff: App, fake: FakeDiscord) -> None:
    for action in ("timeout", "untimeout", "warn", "kick"):
        with pytest.raises(ModError, match="isn't a member"):
            await act(staff, action, 4242)
    message = await act(staff, "ban", 4242)
    assert "Banned" in message and 4242 in fake.guilds[GUILD].bans
    assert await cases(staff) == [(1, "ban", 4242)]


async def test_unknown_roles_fail_closed(staff: App, fake: FakeDiscord) -> None:
    fake.fail_next("member_rank", OpNetwork("timeout"), OpNetwork("timeout"), OpNetwork("timeout"))
    with pytest.raises(ModError, match="couldn't check"):
        await act(staff, "kick", TARGET)
    assert mutations(fake) == 0 and TARGET in fake.guilds[GUILD].members


async def test_raw_forbidden_from_discord_becomes_a_clear_message_and_no_case(staff: App, fake: FakeDiscord) -> None:
    fake.fail_next("kick_member", OpForbidden("403 Forbidden (error code: 50013): Missing Permissions", code=50013))
    with pytest.raises(ModError, match="Discord refused to kick"):
        await act(staff, "kick", TARGET)
    assert await cases(staff) == [] and fake.messages(MODLOG) == []


async def test_case_write_failure_after_success_still_reports_success(
    staff: App, fake: FakeDiscord, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(staff.repo, "create_case", broken)
    message = await act(staff, "kick", TARGET)
    assert "Kicked" in message and "couldn't be saved" in message
    assert TARGET not in fake.guilds[GUILD].members  # the kick happened
    assert any("could not be saved" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)
    [entry] = fake.messages(MODLOG)
    assert entry.embed["title"] == "Kick · case not saved"


# ------------------------------------------------- mod log and audit log


async def test_mod_log_entry_and_audit_reason(staff: App, fake: FakeDiscord, clock: Any) -> None:
    message = await act(staff, "timeout", TARGET, reason="spam in #general")
    assert "Case #1" in message
    [entry] = fake.messages(MODLOG)
    assert entry.mentions == ()  # pings nobody
    embed = entry.embed
    assert embed["title"] == "Timeout · Case #1"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["User"] == f"<@{TARGET}> (`{TARGET}`)" and fields["Moderator"] == f"<@{MOD}>"
    assert fields["Reason"] == "spam in #general"
    end = int((clock.now() + timedelta(minutes=10)).timestamp())
    assert fields["Duration"].startswith("10m") and f"<t:{end}:R>" in fields["Duration"]
    case = await staff.repo.get_case(GUILD, 1)
    assert case is not None and (case.modlog_channel_id, case.modlog_message_id) == (MODLOG, entry.id)
    call = next(c for c in fake.calls if c[0] == "timeout_member")
    assert call[4] == f"Mod ({MOD}): spam in #general"  # the Audit Log names the moderator


async def test_failed_mod_log_post_never_fails_the_action(staff: App, fake: FakeDiscord) -> None:
    fake.fail_next("send_message", OpForbidden("Missing Access", code=50001))
    assert "Kicked" in await act(staff, "kick", TARGET)
    case = await staff.repo.get_case(GUILD, 1)
    assert case is not None and case.modlog_message_id is None
    await staff.repo.set_modlog_channel(GUILD, 999, staff.services.clock.now())  # channel deleted
    assert "Banned" in await act(staff, "ban", 12)
    assert [c[1] for c in await cases(staff)] == ["kick", "ban"]


async def test_no_mod_log_configured(staff: App, fake: FakeDiscord) -> None:
    assert "Mod log turned off" in await mod.set_modlog(staff.services, GUILD, None)
    assert "Kicked" in await act(staff, "kick", TARGET)
    assert fake.messages(MODLOG) == [] and len(await cases(staff)) == 1
    assert "<#300>" in await mod.set_modlog(staff.services, GUILD, MODLOG)


# ------------------------------------------------------------ timeouts


async def test_timeout_and_untimeout(staff: App, fake: FakeDiscord, clock: Any) -> None:
    with pytest.raises(ModError, match="isn't timed out"):
        await act(staff, "untimeout", TARGET)
    with pytest.raises(ModError, match="28 days"):
        await mod.timeout(staff.services, GUILD, ACTOR, TARGET, timedelta(days=29), None)
    message = await mod.timeout(staff.services, GUILD, ACTOR, TARGET, timedelta(days=28), None)
    assert "timed out until" in message
    assert fake.guilds[GUILD].members[TARGET].timeout_until == clock.now() + timedelta(days=28)
    assert "Removed the timeout" in await act(staff, "untimeout", TARGET)
    assert fake.guilds[GUILD].members[TARGET].timeout_until is None
    assert [c[1] for c in await cases(staff)] == ["timeout", "untimeout"]
    case = await staff.repo.get_case(GUILD, 1)
    assert case is not None and case.duration_seconds == 28 * 86400
    # An expired timeout counts as "not timed out".
    fake.guilds[GUILD].members[TARGET].timeout_until = clock.now() - timedelta(seconds=1)
    with pytest.raises(ModError, match="isn't timed out"):
        await act(staff, "untimeout", TARGET)


# ------------------------------------------------------------ warnings


async def test_warn_dms_and_records(staff: App, fake: FakeDiscord) -> None:
    message = await mod.warn(staff.services, GUILD, "Cool *Server*", ACTOR, TARGET, "be nice")
    assert "case #1" in message and "Active warnings: **1**" in message and "notified by DM" in message
    [dm] = fake.dms[TARGET]
    assert "Cool \\*Server\\*" in dm["content"] and "be nice" in dm["content"]
    entry = fake.messages(MODLOG)[0]
    assert entry.embed["title"] == "Warn · Case #1"
    assert "Active warnings: **2**" in await mod.warn(staff.services, GUILD, "S", ACTOR, TARGET, "again")


@pytest.mark.parametrize("failure", ["closed", "error"])
async def test_warn_never_fails_on_undeliverable_dm(staff: App, fake: FakeDiscord, failure: str) -> None:
    if failure == "closed":
        fake.closed_dms.add(TARGET)
    else:
        fake.fail_next("dm_user", OpNetwork("reset"))
    message = await mod.warn(staff.services, GUILD, "S", ACTOR, TARGET, "be nice")
    assert "couldn't DM" in message and "case #1" in message
    fields = {f["name"]: f["value"] for f in fake.messages(MODLOG)[0].embed["fields"]}
    assert "DM not delivered" in fields["Note"]
    assert len(await staff.repo.active_warnings(GUILD, TARGET)) == 1


async def test_warnings_list_and_clearing(staff: App, fake: FakeDiscord) -> None:
    assert "no active warnings" in await mod.list_warnings(staff.services, GUILD, TARGET)
    for i in range(3):
        await mod.warn(staff.services, GUILD, "S", ACTOR, TARGET, f"reason {i} with **bold**")
    text = await mod.list_warnings(staff.services, GUILD, TARGET)
    assert "(3)" in text and "**#1**" in text and "**#3**" in text and "\\*\\*bold\\*\\*" in text
    assert "Cleared warning #2" in await mod.clear_warning(staff.services, GUILD, ACTOR, 2)
    with pytest.raises(ModError, match="isn't an active warning"):
        await mod.clear_warning(staff.services, GUILD, ACTOR, 2)
    assert "#2" not in await mod.list_warnings(staff.services, GUILD, TARGET)
    assert "Cleared 2 warning(s)" in await mod.clear_warnings(staff.services, GUILD, ACTOR, TARGET)
    with pytest.raises(ModError, match="no active warnings"):
        await mod.clear_warnings(staff.services, GUILD, ACTOR, TARGET)
    titles = [m.embed["title"] for m in fake.messages(MODLOG)]
    assert titles[-2:] == ["Warning cleared", "Warnings cleared"]


async def test_long_warning_lists_stay_under_discords_limit(staff: App) -> None:
    for i in range(60):
        await staff.repo.create_warning(GUILD, TARGET, moderator_id=MOD, reason="x" * 300, now=T0)
    text = await mod.list_warnings(staff.services, GUILD, TARGET)
    assert len(text) < 2000 and "more not shown" in text
    assert all(len(line) < 400 for line in text.splitlines())


# ---------------------------------------------------- kick, ban, unban


async def test_ban_with_message_deletion_and_unban(staff: App, fake: FakeDiscord, clock: Any) -> None:
    fake.add_user_message(CHANNEL, TARGET)
    message = await mod.ban(staff.services, GUILD, ACTOR, TARGET, "raid", delete_message_days=1)
    assert "Banned" in message and TARGET in fake.guilds[GUILD].bans
    assert not [m for m in fake.messages(CHANNEL) if m.author_id == TARGET]
    case = await staff.repo.get_case(GUILD, 1)
    assert case is not None and case.extra == {"delete_message_days": 1}
    fields = {f["name"]: f["value"] for f in fake.messages(MODLOG)[0].embed["fields"]}
    assert fields["Duration"] == "Permanent" and fields["Deleted messages"] == "Last 1 day(s)"
    with pytest.raises(ModError, match="0 to 7"):
        await mod.ban(staff.services, GUILD, ACTOR, 12, None, delete_message_days=8)
    assert "Unbanned" in await mod.unban(staff.services, GUILD, ACTOR, TARGET, "appeal")
    assert TARGET not in fake.guilds[GUILD].bans
    with pytest.raises(ModError, match="isn't banned"):
        await mod.unban(staff.services, GUILD, ACTOR, TARGET, None)
    assert [c[1] for c in await cases(staff)] == ["ban", "unban"]


# --------------------------------------------------------------- purge


async def test_purge(staff: App, fake: FakeDiscord, clock: Any) -> None:
    fake.add_user_message(CHANNEL, TARGET, at=clock.now() - timedelta(days=15))
    fake.add_user_message(CHANNEL, TARGET, pinned=True)
    for _ in range(3):
        fake.add_user_message(CHANNEL, TARGET)
        fake.add_user_message(CHANNEL, 12)
    message = await mod.purge(staff.services, GUILD, ACTOR, CHANNEL, 50, TARGET)
    assert "Deleted 3 message(s) from <@11>" in message
    assert "1 older than 14 days" in message and "1 pinned" in message
    case = await staff.repo.get_case(GUILD, 1)
    assert case is not None and case.action == "purge" and case.target_id == TARGET
    assert case.extra == {"channel_id": CHANNEL, "scanned": 50, "deleted": 3, "skipped_old": 1, "skipped_pinned": 1}
    assert "Nothing to delete" in await mod.purge(staff.services, GUILD, ACTOR, CHANNEL, 50, TARGET)
    assert len(await cases(staff)) == 1  # no case when nothing was deleted
    assert "Deleted 3 message(s)." in await mod.purge(staff.services, GUILD, ACTOR, CHANNEL, 3, None)
    with pytest.raises(ModError):
        await mod.purge(staff.services, GUILD, ACTOR, CHANNEL, 101, None)


# ------------------------------------------------------- error messages


def test_permission_errors_name_the_actual_permissions() -> None:
    from discord import app_commands

    one = describe_command_error(app_commands.MissingPermissions(["moderate_members"]))
    assert one == "You need the **Timeout Members** permission to use this command."
    two = describe_command_error(app_commands.MissingPermissions(["manage_messages", "read_message_history"]))
    assert two == "You need the **Manage Messages** and **Read Message History** permissions to use this command."
    bot = describe_command_error(app_commands.BotMissingPermissions(["ban_members"]))
    assert bot is not None and bot.startswith("I'm missing the **Ban Members** permission")
    assert "Manage Server" in (describe_command_error(app_commands.MissingPermissions(["manage_guild"])) or "")
    transformer = app_commands.transformers.MemberTransformer()
    error = app_commands.TransformerError(object(), discord.AppCommandOptionType.user, transformer)
    assert describe_command_error(error) == "That user isn't a member of this server."
    assert describe_command_error(app_commands.AppCommandError("boom")) is None


# ------------------------------------------------------------- commands


async def _mod_tree(services: Any) -> tuple[discord.Client, Any]:
    from discord import app_commands

    from bot.commands.admin import register_admin_commands
    from bot.commands.moderation import register_moderation_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    register_admin_commands(tree, services)
    register_moderation_commands(tree, services)
    return client, tree


EXPECTED_PERMS = {
    "timeout": ("moderate_members",), "untimeout": ("moderate_members",), "warn": ("moderate_members",),
    "warnings": ("moderate_members",), "clearwarn": ("moderate_members",), "clearwarns": ("moderate_members",),
    "kick": ("kick_members",), "ban": ("ban_members",), "unban": ("ban_members",),
    "purge": ("manage_messages", "read_message_history"), "modlog": ("manage_guild",),
}
BOT_CHECKED = {"timeout", "untimeout", "kick", "ban", "unban", "purge"}


async def test_moderation_commands_are_gated_twice(app: App) -> None:
    from discord import app_commands

    client, tree = await _mod_tree(app.services)
    try:
        commands = {c.name: c for c in tree.get_commands()}
        assert set(EXPECTED_PERMS) <= set(commands)
        for name, perms in EXPECTED_PERMS.items():
            command = commands[name]
            assert command.guild_only, name
            assert command.default_permissions == discord.Permissions(**{p: True for p in perms}), name
            leaves = command.commands if isinstance(command, app_commands.Group) else [command]
            for leaf in leaves:
                assert leaf.checks, f"{name} has no runtime permission check"
                expected = 2 if name in BOT_CHECKED else 1  # user check (+ bot check)
                assert len(leaf.checks) == expected, name
        modlog = commands["modlog"]
        assert isinstance(modlog, app_commands.Group) and {c.name for c in modlog.commands} == {"set", "clear"}
        duration = commands["timeout"].get_parameter("duration")  # type: ignore[union-attr]
        assert duration.required
        reason = commands["kick"].get_parameter("reason")  # type: ignore[union-attr]
        assert not reason.required and reason.max_value == mod.REASON_MAX
        amount = commands["purge"].get_parameter("amount")  # type: ignore[union-attr]
        assert (amount.min_value, amount.max_value) == (1, 100)
        days = commands["ban"].get_parameter("delete_message_days")  # type: ignore[union-attr]
        assert (days.min_value, days.max_value, days.default) == (0, 7, 0)
        assert tree.get_command("timeout").to_dict(tree)  # type: ignore[union-attr]  # serialises for sync
    finally:
        await client.close()


class _Response:
    def __init__(self) -> None:
        self.deferred: list[dict[str, Any]] = []
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return bool(self.deferred or self.sent)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, message: str, ephemeral: bool) -> None:
        assert ephemeral
        self.sent.append(message)


def _interaction(user_id: int = MOD) -> Any:
    followups: list[str] = []

    async def followup_send(message: str, ephemeral: bool) -> None:
        assert ephemeral
        followups.append(message)

    return SimpleNamespace(
        guild_id=GUILD, guild=SimpleNamespace(id=GUILD, name="Test Server"), channel_id=CHANNEL,
        user=SimpleNamespace(id=user_id, name="mod"), response=_Response(),
        followup=SimpleNamespace(send=followup_send), followups=followups,
    )


async def test_command_callbacks_defer_and_reply_ephemerally(staff: App, fake: FakeDiscord) -> None:
    client, tree = await _mod_tree(staff.services)
    try:
        timeout_cmd = tree.get_command("timeout")
        bad = _interaction()
        await timeout_cmd.callback(bad, SimpleNamespace(id=TARGET), "soon", None)  # type: ignore[union-attr]
        assert "Use a duration like" in bad.response.sent[0] and not bad.response.deferred
        good = _interaction()
        await timeout_cmd.callback(good, SimpleNamespace(id=TARGET), "10m", "spam")  # type: ignore[union-attr]
        assert good.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert "timed out until" in good.followups[0]
        refused = _interaction()
        await tree.get_command("kick").callback(refused, SimpleNamespace(id=OWNER), None)  # type: ignore[union-attr]
        assert "server owner" in refused.followups[0]
        unban = _interaction()
        await tree.get_command("unban").callback(unban, "not-an-id", None)  # type: ignore[union-attr]
        assert "isn't a valid user ID" in unban.response.sent[0]
    finally:
        await client.close()
