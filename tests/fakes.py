"""Test doubles: a controllable clock, an in-memory Discord, and an app harness.

``FakeDiscord`` implements :class:`bot.discord_ops.DiscordOps` with real
semantics (members, roles, role hierarchy, channels, message history) plus
failure injection:

* ``fail_next(op, *exceptions)`` raises the given exceptions on the next calls.
* ``crash_after(op, n)`` raises :class:`SimulatedCrash` right *after* the n-th
  call of ``op`` has taken effect — the process "dies" between a successful
  Discord operation and the database commit that would record it.
* ``crash_before(op, n)`` raises before the n-th call takes effect.

``App`` builds a fresh application instance (new DB connection, new services)
over the same database file and the same ``FakeDiscord`` — exactly what a
process restart looks like. ``App.crash()`` closes the database without
flushing anything, like a hard kill.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import discord

from bot.clock import ensure_utc
from bot.config import Config
from bot.db.database import Database
from bot.discord_ops import (
    ChannelInfo,
    DiscordOps,
    HistoryMessage,
    MemberInfo,
    OpForbidden,
    OpNotFound,
    RoleCheck,
)
from bot.main import Services, build_services
from bot.ui.embeds import parse_board_marker

MUTATING_OPS = ("send_message", "edit_message", "delete_message", "add_role", "remove_role")


class SimulatedCrash(BaseException):
    """The process died at this point (not an Exception: handlers must not swallow it)."""


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = ensure_utc(moment)

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


def snowflake_at(moment: datetime, seq: int = 0) -> int:
    """A snowflake whose timestamp is ``moment`` (ms precision) with low bits ``seq``."""
    return discord.utils.time_snowflake(ensure_utc(moment)) | (seq & 0x3FFFFF)


@dataclass
class FakeMember:
    user_id: int
    display_name: str
    username: str
    roles: set[int] = field(default_factory=set)


@dataclass
class FakeRole:
    role_id: int
    position: int
    managed: bool = False


@dataclass
class FakeGuild:
    guild_id: int
    members: dict[int, FakeMember] = field(default_factory=dict)
    roles: dict[int, FakeRole] = field(default_factory=dict)
    bot_top_position: int = 100
    manage_roles: bool = True


@dataclass
class FakeMessage:
    id: int
    channel_id: int
    author_id: int
    content: str
    embed: dict | None
    components: list[str]
    created_at: datetime
    edits: int = 0

    @property
    def footer(self) -> str:
        return ((self.embed or {}).get("footer") or {}).get("text", "")

    @property
    def board(self) -> tuple[int, str, int] | None:
        return parse_board_marker(self.footer)


def _custom_ids(view: discord.ui.View | None) -> list[str]:
    if view is None:
        return []
    return [getattr(item, "custom_id", None) or "" for item in view.children]


class FakeDiscord(DiscordOps):
    def __init__(self, clock: FakeClock, bot_user_id: int = 999_000) -> None:
        self.clock = clock
        self._bot_id = bot_user_id
        self.guilds: dict[int, FakeGuild] = {}
        self.channels: dict[int, list[FakeMessage]] = {}
        self.channel_info: dict[int, ChannelInfo] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._fail: dict[str, list[BaseException]] = {}
        self._crash_after: dict[str, int] = {}
        self._crash_before: dict[str, int] = {}
        self._crash_after_any: int | None = None
        self._crash_before_any: int | None = None
        self._seq = itertools.count(1)
        self.on_call: Callable[[str], Any] | None = None

    # ------------------------------------------------------------ fixtures

    def add_guild(self, guild_id: int, *, bot_top_position: int = 100, manage_roles: bool = True) -> FakeGuild:
        guild = FakeGuild(guild_id, bot_top_position=bot_top_position, manage_roles=manage_roles)
        self.guilds[guild_id] = guild
        return guild

    def add_member(self, guild_id: int, user_id: int, name: str | None = None, roles: tuple[int, ...] = ()) -> FakeMember:
        name = name or f"user{user_id}"
        member = FakeMember(user_id, name, name.lower(), set(roles))
        self.guilds[guild_id].members[user_id] = member
        return member

    def remove_member(self, guild_id: int, user_id: int) -> None:
        self.guilds[guild_id].members.pop(user_id, None)

    def create_role(self, guild_id: int, role_id: int, *, position: int = 10, managed: bool = False) -> FakeRole:
        role = FakeRole(role_id, position, managed)
        self.guilds[guild_id].roles[role_id] = role
        return role

    def add_channel(self, channel_id: int) -> None:
        self.channels.setdefault(channel_id, [])

    def roles_of(self, guild_id: int, user_id: int) -> set[int]:
        member = self.guilds[guild_id].members.get(user_id)
        return set(member.roles) if member else set()

    def holders_of(self, guild_id: int, role_id: int) -> set[int]:
        return {m.user_id for m in self.guilds[guild_id].members.values() if role_id in m.roles}

    def messages(self, channel_id: int) -> list[FakeMessage]:
        return list(self.channels.get(channel_id, []))

    def board_messages(self, channel_id: int) -> list[FakeMessage]:
        return [m for m in self.channels.get(channel_id, []) if m.board is not None]

    def announcements(self, channel_id: int) -> list[FakeMessage]:
        return [m for m in self.channels.get(channel_id, []) if "reset:" in m.footer]

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)

    # ----------------------------------------------------------- injection

    def fail_next(self, op: str, *errors: BaseException) -> None:
        self._fail.setdefault(op, []).extend(errors)

    def crash_after(self, op: str, n: int = 1) -> None:
        self._crash_after[op] = n

    def crash_before(self, op: str, n: int = 1) -> None:
        self._crash_before[op] = n

    def crash_after_mutation(self, n: int) -> None:
        """Crash right after the n-th mutating operation (any of MUTATING_OPS) from now."""
        self._crash_after_any = n

    def crash_before_mutation(self, n: int) -> None:
        self._crash_before_any = n

    def clear_injections(self) -> None:
        self._fail.clear()
        self._crash_after.clear()
        self._crash_before.clear()
        self._crash_after_any = None
        self._crash_before_any = None

    def _before(self, op: str, *args: Any) -> None:
        self.calls.append((op, *args))
        if self.on_call is not None:
            self.on_call(op)
        queue = self._fail.get(op)
        if queue:
            raise queue.pop(0)
        if op in self._crash_before:
            self._crash_before[op] -= 1
            if self._crash_before[op] <= 0:
                del self._crash_before[op]
                raise SimulatedCrash(f"before {op}")
        if op in MUTATING_OPS and self._crash_before_any is not None:
            self._crash_before_any -= 1
            if self._crash_before_any <= 0:
                self._crash_before_any = None
                raise SimulatedCrash(f"before {op}")

    def _after(self, op: str) -> None:
        if op in self._crash_after:
            self._crash_after[op] -= 1
            if self._crash_after[op] <= 0:
                del self._crash_after[op]
                raise SimulatedCrash(f"after {op}")
        if op in MUTATING_OPS and self._crash_after_any is not None:
            self._crash_after_any -= 1
            if self._crash_after_any <= 0:
                self._crash_after_any = None
                raise SimulatedCrash(f"after {op}")

    def _new_id(self) -> int:
        return snowflake_at(self.clock.now(), next(self._seq))

    def _find(self, channel_id: int, message_id: int) -> FakeMessage | None:
        for msg in self.channels.get(channel_id, []):
            if msg.id == message_id:
                return msg
        return None

    # --------------------------------------------------------- DiscordOps

    @property
    def bot_user_id(self) -> int:
        return self._bot_id

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberInfo:
        self._before("fetch_member", guild_id, user_id)
        guild = self.guilds.get(guild_id)
        member = guild.members.get(user_id) if guild else None
        if member is None:
            raise OpNotFound("404 Not Found (error code: 10007): Unknown Member", code=10007)
        info = MemberInfo(user_id, member.display_name, member.username, frozenset(member.roles))
        self._after("fetch_member")
        return info

    async def send_message(
        self,
        channel_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        mention_user_ids: Any = (),
    ) -> int:
        self._before("send_message", channel_id)
        if channel_id not in self.channels:
            raise OpNotFound("Unknown Channel", code=10003)
        msg = FakeMessage(
            id=self._new_id(),
            channel_id=channel_id,
            author_id=self._bot_id,
            content=content or "",
            embed=embed.to_dict() if embed is not None else None,
            components=_custom_ids(view),
            created_at=self.clock.now(),
        )
        self.channels[channel_id].append(msg)
        self._after("send_message")
        return msg.id

    async def edit_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        self._before("edit_message", channel_id, message_id)
        msg = self._find(channel_id, message_id)
        if msg is None:
            raise OpNotFound("Unknown Message", code=10008)
        if embed is not None:
            msg.embed = embed.to_dict()
        if view is not None:
            msg.components = _custom_ids(view)
        msg.edits += 1
        self._after("edit_message")

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self._before("delete_message", channel_id, message_id)
        msg = self._find(channel_id, message_id)
        if msg is None:
            raise OpNotFound("Unknown Message", code=10008)
        self.channels[channel_id].remove(msg)
        self._after("delete_message")

    async def recent_history(self, channel_id: int, *, after: datetime | None, limit: int) -> list[HistoryMessage]:
        self._before("history", channel_id)
        if channel_id not in self.channels:
            raise OpNotFound("Unknown Channel", code=10003)
        msgs = self.channels[channel_id]
        if after is not None:
            selected = [m for m in msgs if m.created_at > ensure_utc(after)][:limit]
        else:
            selected = msgs[-limit:]
        result = [
            HistoryMessage(m.id, m.author_id, m.content, (m.footer,) if m.footer else (), m.created_at)
            for m in selected
        ]
        self._after("history")
        return result

    def _role_forbidden(self, guild: FakeGuild, role_id: int) -> str | None:
        role = guild.roles.get(role_id)
        if role is None:
            return "Unknown Role"
        if role.managed or not guild.manage_roles or role.position >= guild.bot_top_position:
            return "Missing Permissions"
        return None

    async def add_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        self._before("add_role", guild_id, user_id, role_id)
        guild = self.guilds[guild_id]
        member = guild.members.get(user_id)
        if member is None:
            raise OpNotFound("Unknown Member", code=10007)
        problem = self._role_forbidden(guild, role_id)
        if problem == "Unknown Role":
            raise OpNotFound("Unknown Role", code=10011)
        if problem:
            raise OpForbidden("403 Forbidden (error code: 50013): Missing Permissions", code=50013)
        member.roles.add(role_id)
        self._after("add_role")

    async def remove_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        self._before("remove_role", guild_id, user_id, role_id)
        guild = self.guilds[guild_id]
        member = guild.members.get(user_id)
        if member is None:
            raise OpNotFound("Unknown Member", code=10007)
        problem = self._role_forbidden(guild, role_id)
        if problem == "Unknown Role":
            raise OpNotFound("Unknown Role", code=10011)
        if problem:
            raise OpForbidden("403 Forbidden (error code: 50013): Missing Permissions", code=50013)
        member.roles.discard(role_id)
        self._after("remove_role")

    def check_role_manageable(self, guild_id: int, role_id: int) -> RoleCheck:
        guild = self.guilds.get(guild_id)
        if guild is None:
            return RoleCheck(False, "guild is not available")
        role = guild.roles.get(role_id)
        if role is None:
            return RoleCheck(False, f"role {role_id} no longer exists", missing_role=True)
        if role.managed:
            return RoleCheck(False, "role is managed by an integration")
        if not guild.manage_roles:
            return RoleCheck(False, "bot lacks the Manage Roles permission")
        if role.position >= guild.bot_top_position:
            return RoleCheck(False, "role is not below the bot's highest role")
        return RoleCheck(True)

    async def resolve_channel(self, guild_id: int, channel_id: int) -> ChannelInfo:
        self._before("resolve_channel", guild_id, channel_id)
        info = self.channel_info.get(channel_id)
        if info is None:
            raise OpNotFound("Unknown Channel", code=10003)
        self._after("resolve_channel")
        return info


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    values: dict[str, Any] = dict(
        token="test-token-not-real",
        db_path=tmp_path / "leaderboard.db",
        flush_interval_seconds=5.0,
        reset_grace_seconds=120,
        board_update_seconds=300,
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )
    values.update(overrides)
    return Config(**values)


class App:
    """One process lifetime of the bot's application layer."""

    instances: list["App"] = []

    def __init__(self, services: Services) -> None:
        self.services = services
        App.instances.append(self)

    @classmethod
    async def start(cls, config: Config, fake: FakeDiscord, clock: FakeClock) -> "App":
        db = Database(config.db_path)
        await db.connect()
        services = build_services(config, db, fake, clock)
        await services.exclusions.load()
        await services.installs.load()
        return cls(services)

    async def crash(self) -> None:
        """Hard stop: buffered counts are lost, nothing is flushed."""
        await self.services.db.close()

    async def shutdown(self) -> None:
        """Graceful stop: final flush, then close."""
        assert await self.services.buffer.flush()
        await self.services.db.close()

    @property
    def repo(self):  # noqa: ANN201 - convenience
        return self.services.repo

    @property
    def runner(self):  # noqa: ANN201
        return self.services.runner

    @property
    def boards(self):  # noqa: ANN201
        return self.services.boards

    @property
    def buffer(self):  # noqa: ANN201
        return self.services.buffer

    def record(self, guild_id: int, user_id: int, at: datetime, *, seq: int | None = None, name: str | None = None) -> int:
        """Record one counted message created at ``at``; returns its snowflake."""
        message_id = snowflake_at(at, seq if seq is not None else next(_RECORD_SEQ))
        self.services.buffer.record(
            guild_id=guild_id,
            user_id=user_id,
            message_id=message_id,
            created_at=at,
            display_name=name or f"user{user_id}",
            username=(name or f"user{user_id}").lower(),
        )
        return message_id

    async def messages(self, guild_id: int, user_id: int, at: datetime, n: int = 1, *, step_seconds: float = 1.0, name: str | None = None) -> None:
        """Record ``n`` messages starting at ``at`` and flush them."""
        for i in range(n):
            self.record(guild_id, user_id, at + timedelta(seconds=step_seconds * i), name=name)
        assert await self.services.buffer.flush()


_RECORD_SEQ = itertools.count(1)
