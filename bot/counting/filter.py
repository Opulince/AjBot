"""Message eligibility and excluded-channel resolution.

A message counts when it is a guild message from a human (not a bot, not a
webhook), of type ``default`` or ``reply``, created after the guild ran /setup,
and neither its channel, its thread parent (threads and forum posts), nor the
relevant category is excluded. On top of that, a user banned from the
leaderboard (``/lbban``) is not counted, and with ``/countcooldown`` a message
sent within the cooldown of the author's previous *counted* message is not
counted either (anti-farming).

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
from datetime import datetime
from typing import Any, Collection, Protocol

import discord

from bot.clock import ensure_utc
from bot.counting.buffer import CountBuffer
from bot.db.repo import Repo
from bot.discord_ops import ChannelInfo, DiscordOps, OpError
from bot.ranking import milestone_ms

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


class LbBanCache:
    """In-memory mirror of ``lb_banned_users``; updated only after a DB commit."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._by_guild: dict[int, set[int]] = {}

    async def load(self) -> None:
        self._by_guild = await self._repo.load_all_lb_bans()
        total = sum(len(v) for v in self._by_guild.values())
        log.info("Loaded %d leaderboard ban(s) across %d guild(s)", total, len(self._by_guild))

    def is_banned(self, guild_id: int, user_id: int) -> bool:
        return user_id in self._by_guild.get(guild_id, ())

    def set_banned(self, guild_id: int, user_id: int, banned: bool) -> None:
        users = self._by_guild.setdefault(guild_id, set())
        if banned:
            users.add(user_id)
        else:
            users.discard(user_id)


class CooldownTracker:
    """Per-guild count cooldown (``/countcooldown``) and each user's last counted message time.

    The last-counted times live only in memory, in an LRU bounded to
    ``max_users`` entries: after a restart (or once an idle user is evicted)
    their next message always counts. Times come from message snowflakes, so
    they don't depend on when an event happens to be processed.
    """

    def __init__(self, repo: Repo, *, max_users: int = 50_000) -> None:
        self._repo = repo
        self._seconds: dict[int, int] = {}
        self._last: OrderedDict[tuple[int, int], int] = OrderedDict()  # (guild, user) -> ms since the Discord epoch
        self._max = max_users

    async def load(self) -> None:
        self._seconds = await self._repo.load_all_cooldowns()
        log.info("Count cooldown enabled in %d guild(s)", len(self._seconds))

    def cooldown(self, guild_id: int) -> int:
        return self._seconds.get(guild_id, 0)

    def set_cooldown(self, guild_id: int, seconds: int) -> None:
        if seconds > 0:
            self._seconds[guild_id] = seconds
        else:
            self._seconds.pop(guild_id, None)

    def allow(self, guild_id: int, user_id: int, message_id: int) -> bool:
        """Whether this message is far enough from the user's last counted one.

        Must stay free of ``await`` (it is a check-and-set). Delivery can be
        slightly out of order, so the distance is absolute; an allowed message
        moves the user's last time forward only.
        """
        seconds = self._seconds.get(guild_id, 0)
        if seconds <= 0:
            return True
        key = (guild_id, user_id)
        at = milestone_ms(message_id)
        last = self._last.get(key)
        if last is not None and abs(at - last) < seconds * 1000:
            return False
        self._last[key] = max(at, last) if last is not None else at
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return True

    def forget(self, guild_id: int, user_id: int, message_id: int) -> None:
        """Undo :meth:`allow` for a message that ended up not counted (a duplicate delivery)."""
        key = (guild_id, user_id)
        if self._last.get(key) == milestone_ms(message_id):
            del self._last[key]


class InstallCache:
    """In-memory mirror of each guild's ``installed_at`` (set by the first /setup).

    Counting only starts at installation: messages in guilds that were never set
    up, and messages created before ``installed_at``, are not counted. Comparing
    at message precision keeps the install day exact even though day buckets
    cannot be split.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._installed: dict[int, datetime] = {}

    async def load(self) -> None:
        self._installed = {
            cfg.guild_id: cfg.installed_at
            for cfg in await self._repo.list_installed_guilds()
            if cfg.installed_at is not None
        }
        log.info("Counting enabled for %d installed guild(s)", len(self._installed))

    def installed_at(self, guild_id: int) -> datetime | None:
        return self._installed.get(guild_id)

    def set_installed(self, guild_id: int, installed_at: datetime) -> None:
        self._installed[guild_id] = installed_at


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

    def __init__(
        self,
        exclusions: ExclusionStore,
        resolver: ChannelResolver,
        buffer: CountBuffer,
        installs: InstallCache,
        lb_bans: LbBanCache | None = None,
        cooldowns: CooldownTracker | None = None,
    ) -> None:
        self._exclusions = exclusions
        self._resolver = resolver
        self._buffer = buffer
        self._installs = installs
        self._lb_bans = lb_bans
        self._cooldowns = cooldowns

    async def handle(self, message: Any) -> bool:
        """Return ``True`` if the message was counted."""
        if rejection_reason(message) is not None:
            return False
        guild_id = message.guild.id
        installed_at = self._installs.installed_at(guild_id)
        if installed_at is None or ensure_utc(message.created_at) < installed_at:
            return False  # counting starts when /setup is first run
        ancestry = await self._resolver.resolve(guild_id, message.channel)
        if ancestry is None:
            return False
        # Read exclusions after the (possible) await so changes made meanwhile apply.
        excluded = self._exclusions.excluded_ids(guild_id)
        if is_excluded(ancestry, excluded):
            return False
        author = message.author
        # Everything from here on is synchronous (no await): the ban check, the
        # cooldown check-and-set and the buffer's duplicate check + increment all
        # see one consistent state, so concurrent handlers can't double count.
        if self._lb_bans is not None and self._lb_bans.is_banned(guild_id, author.id):
            return False
        if self._cooldowns is not None and not self._cooldowns.allow(guild_id, author.id, message.id):
            return False
        display_name = getattr(author, "display_name", None) or author.name
        counted = self._buffer.record(
            guild_id=guild_id,
            user_id=author.id,
            message_id=message.id,
            created_at=message.created_at,
            display_name=str(display_name),
            username=str(author.name),
        )
        if not counted and self._cooldowns is not None:
            self._cooldowns.forget(guild_id, author.id, message.id)
        return counted
