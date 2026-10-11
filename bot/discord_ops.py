"""Thin interface over the Discord API operations the business logic needs.

Business logic (resets, boards, role reconciliation) talks to :class:`DiscordOps`
instead of discord.py objects, so unit tests can substitute an in-memory fake.
All discord.py exceptions are translated into a small hierarchy that separates
the failure classes the bot must treat differently:

* :class:`OpNotFound`    — the resource is confirmed not to exist (404)
* :class:`OpForbidden`   — missing permission / role hierarchy (403)
* :class:`OpRateLimited` — 429 that discord.py did not absorb itself
* :class:`OpTransient`   — Discord server errors (5xx)
* :class:`OpNetwork`     — connection problems and timeouts
* :class:`OpBadRequest`  — other 4xx: our request was rejected (not retryable)

Anything else is an unexpected programming error and propagates unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Iterator, Sequence, TypeVar
from urllib.parse import unquote

import aiohttp
import discord

from bot.clock import ensure_utc

log = logging.getLogger(__name__)

T = TypeVar("T")


class OpError(Exception):
    """Base class for classified Discord API failures."""

    kind = "error"

    def __init__(self, message: str = "", *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    def describe(self) -> str:
        text = str(self) or self.__class__.__name__
        return f"{self.kind}: {text}"


class OpNotFound(OpError):
    kind = "not_found"


class OpForbidden(OpError):
    kind = "forbidden"


class OpRateLimited(OpError):
    kind = "rate_limited"

    def __init__(self, message: str = "", *, retry_after: float = 5.0, code: int | None = None) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after


class OpTransient(OpError):
    kind = "transient_http"


class OpNetwork(OpError):
    kind = "network"


class OpBadRequest(OpError):
    kind = "bad_request"


RETRYABLE_ERRORS: tuple[type[OpError], ...] = (OpRateLimited, OpTransient, OpNetwork)


@dataclass(frozen=True)
class MemberInfo:
    user_id: int
    display_name: str
    username: str
    role_ids: frozenset[int]


@dataclass(frozen=True)
class HistoryMessage:
    id: int
    author_id: int
    content: str
    footers: tuple[str, ...]
    created_at: datetime
    links: tuple[str, ...] = ()  # embed author/title URLs (boards carry their marker in the author link)

    @property
    def marker_texts(self) -> tuple[str, ...]:
        """Every place a marker may be stored: content, embed footers and (URL-decoded) embed links."""
        return (self.content, *self.footers, *(unquote(link) for link in self.links))

    def contains_marker(self, marker: str) -> bool:
        """Exact marker match (``board:1:daily:5`` must not match ``board:1:daily:50``)."""
        return any(has_marker(text, marker) for text in self.marker_texts)


def has_marker(text: str, marker: str) -> bool:
    pattern = r"(?<![\w:-])" + re.escape(marker) + r"(?![\w:-])"
    return re.search(pattern, text or "") is not None


@dataclass(frozen=True)
class ChannelInfo:
    """A channel's exclusion ancestry: itself, its thread parent, and its category."""

    channel_id: int
    parent_id: int | None
    category_id: int | None

    def ids(self) -> set[int]:
        return {i for i in (self.channel_id, self.parent_id, self.category_id) if i is not None}


@dataclass(frozen=True)
class RoleCheck:
    ok: bool
    reason: str | None = None
    missing_role: bool = False


class Presence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"  # confirmed NotFound
    UNKNOWN = "unknown"  # could not determine (never treat as departure)


@dataclass(frozen=True)
class PresenceResult:
    presence: Presence
    member: MemberInfo | None = None
    error: OpError | None = None


class DiscordOps(ABC):
    """Operations used by the business logic. Implemented by discord.py or a test fake."""

    @property
    @abstractmethod
    def bot_user_id(self) -> int: ...

    @abstractmethod
    async def fetch_member(self, guild_id: int, user_id: int) -> MemberInfo:
        """Return the member, or raise :class:`OpNotFound` if they are not in the guild."""

    @abstractmethod
    async def send_message(
        self,
        channel_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        mention_user_ids: Sequence[int] = (),
    ) -> int:
        """Send a message and return its ID. Only ``mention_user_ids`` may be pinged."""

    @abstractmethod
    async def edit_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None: ...

    @abstractmethod
    async def delete_message(self, channel_id: int, message_id: int) -> None: ...

    @abstractmethod
    async def recent_history(
        self, channel_id: int, *, after: datetime | None, limit: int
    ) -> list[HistoryMessage]:
        """Messages created after ``after`` (oldest first), at most ``limit``."""

    @abstractmethod
    async def add_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None: ...

    @abstractmethod
    async def remove_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None: ...

    @abstractmethod
    def check_role_manageable(self, guild_id: int, role_id: int) -> RoleCheck:
        """Pre-flight check from cache: role exists, bot has Manage Roles, hierarchy allows it."""

    @abstractmethod
    async def resolve_channel(self, guild_id: int, channel_id: int) -> ChannelInfo:
        """Fetch a channel's ancestry when it is not available from the cache."""

    async def member_has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        member = await self.fetch_member(guild_id, user_id)
        return role_id in member.role_ids


# --------------------------------------------------------------------- helpers


def classify_exception(exc: BaseException) -> OpError | None:
    """Translate a discord.py / aiohttp exception. Returns ``None`` for unexpected errors."""
    if isinstance(exc, OpError):
        return exc
    if isinstance(exc, discord.NotFound):
        return OpNotFound(str(exc), code=exc.code)
    if isinstance(exc, discord.Forbidden):
        return OpForbidden(str(exc), code=exc.code)
    if isinstance(exc, discord.RateLimited):
        return OpRateLimited(str(exc), retry_after=float(exc.retry_after))
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", 0) or 0
        if status == 429:
            retry_after = 5.0
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            if headers is not None:
                try:
                    retry_after = float(headers.get("Retry-After", retry_after))
                except (TypeError, ValueError):
                    pass
            return OpRateLimited(str(exc), retry_after=retry_after, code=exc.code)
        if status >= 500:
            return OpTransient(str(exc), code=exc.code)
        return OpBadRequest(str(exc), code=exc.code)
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError, discord.ConnectionClosed)):
        return OpNetwork(f"{exc.__class__.__name__}: {exc}")
    return None


@contextmanager
def translate_errors(operation: str) -> Iterator[None]:
    try:
        yield
    except OpError:
        raise
    except Exception as exc:
        classified = classify_exception(exc)
        if classified is None:
            raise
        raise classified from exc


async def _retry_sleep(delay: float) -> None:
    """Indirection so tests can make in-call retries instantaneous."""
    await asyncio.sleep(delay)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    name: str,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run ``operation``, retrying retryable failures with exponential backoff and jitter.

    Only rate limits, transient HTTP errors and network errors are retried, and
    only a bounded number of times. NotFound / Forbidden / bad requests are
    raised immediately because retrying cannot fix them.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except RETRYABLE_ERRORS as exc:
            if attempt >= attempts:
                raise
            if isinstance(exc, OpRateLimited):
                delay = min(max(exc.retry_after, base_delay), 60.0)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                delay *= random.uniform(0.75, 1.25)
            log.warning(
                "Discord operation %s failed (%s), retry %d/%d in %.1fs",
                name,
                exc.describe(),
                attempt,
                attempts - 1,
                delay,
            )
            await (sleep or _retry_sleep)(delay)


async def check_presence(ops: DiscordOps, guild_id: int, user_id: int, *, attempts: int = 3) -> PresenceResult:
    """Determine membership, distinguishing a confirmed absence from an unknown state."""
    try:
        member = await with_retries(
            lambda: ops.fetch_member(guild_id, user_id), name="fetch_member", attempts=attempts
        )
    except OpNotFound:
        return PresenceResult(Presence.ABSENT)
    except OpError as exc:
        return PresenceResult(Presence.UNKNOWN, error=exc)
    return PresenceResult(Presence.PRESENT, member=member)


# ------------------------------------------------------------ discord.py impl


class DiscordPyOps(DiscordOps):
    """Production implementation backed by a connected :class:`discord.Client`."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    @property
    def bot_user_id(self) -> int:
        user = self._client.user
        if user is None:
            raise RuntimeError("Discord client is not logged in")
        return user.id

    def _messageable(self, channel_id: int) -> discord.abc.Messageable:
        channel = self._client.get_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            return channel
        return self._client.get_partial_messageable(channel_id)

    def _partial_message(self, channel_id: int, message_id: int) -> discord.PartialMessage:
        channel = self._messageable(channel_id)
        return channel.get_partial_message(message_id)  # type: ignore[attr-defined]

    async def _guild(self, guild_id: int) -> discord.Guild:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            guild = await self._client.fetch_guild(guild_id)
        return guild

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberInfo:
        with translate_errors("fetch_member"):
            guild = await self._guild(guild_id)
            member = await guild.fetch_member(user_id)
        return MemberInfo(
            user_id=member.id,
            display_name=member.display_name,
            username=member.name,
            role_ids=frozenset(r.id for r in member.roles),
        )

    async def send_message(
        self,
        channel_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        mention_user_ids: Sequence[int] = (),
    ) -> int:
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=False,
            replied_user=False,
            users=[discord.Object(id=u) for u in mention_user_ids] if mention_user_ids else False,
        )
        # discord.py retries 5xx / connection resets internally with the same
        # payload; a nonce makes Discord return the already-created message
        # (enforce_nonce) instead of creating a duplicate.
        kwargs: dict = {"allowed_mentions": allowed, "nonce": secrets.randbits(63)}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        with translate_errors("send_message"):
            message = await self._messageable(channel_id).send(**kwargs)
        return message.id

    async def edit_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        kwargs: dict = {}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        with translate_errors("edit_message"):
            await self._partial_message(channel_id, message_id).edit(**kwargs)

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        with translate_errors("delete_message"):
            await self._partial_message(channel_id, message_id).delete()

    async def recent_history(
        self, channel_id: int, *, after: datetime | None, limit: int
    ) -> list[HistoryMessage]:
        # With ``after`` the oldest messages after that moment are returned; without
        # it, the newest ``limit`` messages. Either way the result is oldest first.
        after_obj = discord.Object(id=discord.utils.time_snowflake(ensure_utc(after))) if after else None
        result: list[HistoryMessage] = []
        with translate_errors("history"):
            channel = self._messageable(channel_id)
            async for msg in channel.history(limit=limit, after=after_obj, oldest_first=after_obj is not None):
                footers = tuple(e.footer.text for e in msg.embeds if e.footer and e.footer.text)
                links = tuple(url for e in msg.embeds for url in (e.author.url, e.url) if url)
                result.append(
                    HistoryMessage(
                        id=msg.id,
                        author_id=msg.author.id,
                        content=msg.content or "",
                        footers=footers,
                        created_at=msg.created_at,
                        links=links,
                    )
                )
        if after_obj is None:
            result.reverse()
        return result

    async def add_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        with translate_errors("add_role"):
            await self._client.http.add_role(guild_id, user_id, role_id, reason=reason)

    async def remove_role(self, guild_id: int, user_id: int, role_id: int, *, reason: str) -> None:
        with translate_errors("remove_role"):
            await self._client.http.remove_role(guild_id, user_id, role_id, reason=reason)

    def check_role_manageable(self, guild_id: int, role_id: int) -> RoleCheck:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            return RoleCheck(False, "guild is not available in the cache")
        role = guild.get_role(role_id)
        if role is None:
            return RoleCheck(False, f"role {role_id} no longer exists", missing_role=True)
        return evaluate_role_manageable(guild, role)

    async def resolve_channel(self, guild_id: int, channel_id: int) -> ChannelInfo:
        with translate_errors("resolve_channel"):
            guild = await self._guild(guild_id)
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None:
                channel = await guild.fetch_channel(channel_id)
            if isinstance(channel, discord.Thread):
                parent: Any = guild.get_channel(channel.parent_id)
                if parent is None:
                    parent = await guild.fetch_channel(channel.parent_id)
                category_id = getattr(parent, "category_id", None)
                return ChannelInfo(channel.id, channel.parent_id, category_id)
            return ChannelInfo(channel.id, None, getattr(channel, "category_id", None))


def evaluate_role_manageable(guild: discord.Guild, role: discord.Role) -> RoleCheck:
    """Whether the bot can assign ``role``. Administrator does *not* bypass hierarchy."""
    me = guild.me
    if role.is_default():
        return RoleCheck(False, "@everyone cannot be used as a reward role")
    if role.managed:
        return RoleCheck(False, f"role '{role.name}' is managed by an integration and cannot be assigned")
    if me is None:
        return RoleCheck(False, "bot member is not cached for this guild")
    if not me.guild_permissions.manage_roles:
        return RoleCheck(False, "bot lacks the Manage Roles permission")
    if role >= me.top_role:
        return RoleCheck(
            False,
            f"role '{role.name}' is not below the bot's highest role '{me.top_role.name}' "
            "(move the bot's role above the reward roles)",
        )
    return RoleCheck(True)
