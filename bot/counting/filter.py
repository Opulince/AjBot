"""Message eligibility and excluded-channel resolution.

A message counts when it is a guild message from a human (not a bot, not a
webhook), of type ``default`` or ``reply``, and neither its channel, its thread
parent (threads and forum posts), nor the relevant category is excluded.

Ancestry is taken from discord.py's channel cache when possible. When it cannot
be determined from the cache (for example an uncached thread whose message
arrives as a ``PartialMessageable``) it is fetched through :class:`DiscordOps`.
If it still cannot be resolved the message is *not* counted (fail closed), so
exclusions can never leak because of a cache miss.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Collection, Protocol

import discord

from bot.counting.buffer import CountBuffer
from bot.db.repo import Repo
from bot.discord_ops import ChannelInfo, DiscordOps, OpError

log = logging.getLogger(__name__)

COUNTABLE_TYPES = frozenset({discord.MessageType.default, discord.MessageType.reply})
THREAD_TYPES = frozenset(
    {discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.news_thread}
)


class ExclusionStore(Protocol):
    def excluded_ids(self, guild_id: int) -> Collection[int]: ...


def rejection_reason(message: Any) -> str | None:
    """Return why a message is ineligible (ignoring channel exclusions), or ``None``."""
    if getattr(message, "guild", None) is None:
        return "dm"
    if getattr(message, "webhook_id", None) is not None:
        return "webhook"
    author = message.author
    if getattr(author, "bot", False) or getattr(author, "system", False):
        return "bot"
    if message.type not in COUNTABLE_TYPES:
        return "message_type"
    return None


def ancestry_from_cache(channel: Any) -> ChannelInfo | None:
    """Resolve a channel's ancestry from cached objects only, or ``None`` if unknown."""
    channel_type = getattr(channel, "type", None)
    if channel_type in THREAD_TYPES:
        parent = getattr(channel, "parent", None)
        parent_id = getattr(channel, "parent_id", None)
        if parent is None or parent_id is None:
            return None
        return ChannelInfo(channel.id, parent_id, getattr(parent, "category_id", None))
    if not hasattr(channel, "category_id"):
        # PartialMessageable / Object: no ancestry information available.
        return None
    return ChannelInfo(channel.id, None, channel.category_id)


def is_excluded(ancestry: ChannelInfo, excluded: Collection[int]) -> bool:
    return any(i in excluded for i in ancestry.ids())


class ExclusionCache:
    """In-memory mirror of ``excluded_channels``; updated only after a DB commit."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._by_guild: dict[int, set[int]] = {}

    async def load(self) -> None:
        self._by_guild = await self._repo.load_all_exclusions()
        total = sum(len(v) for v in self._by_guild.values())
        log.info("Loaded %d channel/category exclusions across %d guilds", total, len(self._by_guild))

    def excluded_ids(self, guild_id: int) -> Collection[int]:
        return self._by_guild.get(guild_id, frozenset())

    def set_excluded(self, guild_id: int, channel_id: int, present: bool) -> None:
        ids = self._by_guild.setdefault(guild_id, set())
        if present:
            ids.add(channel_id)
        else:
            ids.discard(channel_id)


class ChannelResolver:
    """Resolves ancestry for channels missing from the cache, with a small TTL cache."""

    def __init__(self, ops: DiscordOps, *, ttl_seconds: float = 600.0, max_entries: int = 2048) -> None:
        self._ops = ops
        self._ttl = ttl_seconds
        self._max = max_entries
        self._cache: OrderedDict[int, tuple[float, ChannelInfo]] = OrderedDict()

    async def resolve(self, guild_id: int, channel: Any) -> ChannelInfo | None:
        cached = ancestry_from_cache(channel)
        if cached is not None:
            return cached
        channel_id = channel.id
        entry = self._cache.get(channel_id)
        now = time.monotonic()
        if entry is not None and now - entry[0] < self._ttl:
            self._cache.move_to_end(channel_id)
            return entry[1]
        try:
            info = await self._ops.resolve_channel(guild_id, channel_id)
        except OpError as exc:
            log.warning(
                "Could not resolve channel %s in guild %s (%s); message not counted",
                channel_id,
                guild_id,
                exc.describe(),
            )
            return None
        self._cache[channel_id] = (now, info)
        self._cache.move_to_end(channel_id)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return info


class MessageCounter:
    """Decides whether a gateway message counts and records it in the buffer."""

    def __init__(self, exclusions: ExclusionStore, resolver: ChannelResolver, buffer: CountBuffer) -> None:
        self._exclusions = exclusions
        self._resolver = resolver
        self._buffer = buffer

    async def handle(self, message: Any) -> bool:
        """Return ``True`` if the message was counted."""
        if rejection_reason(message) is not None:
            return False
        guild_id = message.guild.id
        ancestry = await self._resolver.resolve(guild_id, message.channel)
        if ancestry is None:
            return False
        # Read exclusions after the (possible) await so changes made meanwhile apply.
        excluded = self._exclusions.excluded_ids(guild_id)
        if is_excluded(ancestry, excluded):
            return False
        author = message.author
        display_name = getattr(author, "display_name", None) or author.name
        # record() is synchronous: the duplicate check and the increment happen
        # without yielding to the event loop, so concurrent handlers for the same
        # message cannot both count it.
        return self._buffer.record(
            guild_id=guild_id,
            user_id=author.id,
            message_id=message.id,
            created_at=message.created_at,
            display_name=str(display_name),
            username=str(author.name),
        )
