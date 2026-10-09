"""Message eligibility, exclusion inheritance, dedupe and member metadata.

Channels, threads and messages are real discord.py objects built from gateway
payloads, so the filter is exercised against the library's actual attributes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import discord
import pytest

from bot.clock import UTC
from bot.counting.filter import ChannelResolver, ExclusionCache, MessageCounter, ancestry_from_cache, rejection_reason
from bot.discord_ops import ChannelInfo, OpNetwork
from bot.main import LeaderboardBot
from tests.conftest import GUILD
from tests.fakes import App, FakeDiscord, snowflake_at

CATEGORY = 10
TEXT = 20
FORUM = 30
OTHER_CATEGORY = 11
OTHER_TEXT = 21
VOICE = 60
THREAD = 40
FORUM_POST = 41


class World:
    """A guild with real discord.py channel objects."""

    def __init__(self) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        self.state = self.client._connection
        self.guild = discord.Guild(
            data={"id": str(GUILD), "name": "g", "channels": [], "roles": [], "members": [], "threads": []},
            state=self.state,
        )
        self.category = self._chan(discord.CategoryChannel, CATEGORY, 4)
        self.other_category = self._chan(discord.CategoryChannel, OTHER_CATEGORY, 4)
        self.text = self._chan(discord.TextChannel, TEXT, 0, parent=CATEGORY)
        self.other_text = self._chan(discord.TextChannel, OTHER_TEXT, 0, parent=OTHER_CATEGORY)
        self.forum = self._chan(discord.ForumChannel, FORUM, 15, parent=CATEGORY)
        self.voice = self._chan(discord.VoiceChannel, VOICE, 2, parent=OTHER_CATEGORY, bitrate=64000, user_limit=0)
        self.thread = self._thread(THREAD, TEXT)
        self.forum_post = self._thread(FORUM_POST, FORUM)

    def _chan(self, cls: type, cid: int, ctype: int, parent: int | None = None, **extra: Any) -> Any:
        data = {"id": str(cid), "type": ctype, "name": f"c{cid}", "position": 0, "guild_id": str(GUILD),
                "permission_overwrites": [], **extra}
        if parent:
            data["parent_id"] = str(parent)
        channel = cls(state=self.state, guild=self.guild, data=data)
        self.guild._add_channel(channel)
        return channel

    def _thread(self, tid: int, parent: int) -> discord.Thread:
        data = {"id": str(tid), "type": 11, "name": f"t{tid}", "guild_id": str(GUILD), "parent_id": str(parent),
                "owner_id": "5", "message_count": 0, "member_count": 0,
                "thread_metadata": {"archived": False, "auto_archive_duration": 60,
                                    "archive_timestamp": "2026-01-01T00:00:00+00:00", "locked": False}}
        thread = discord.Thread(guild=self.guild, state=self.state, data=data)
        self.guild._add_thread(thread)
        return thread

    def message(
        self,
        channel: Any,
        *,
        author_id: int = 11,
        created_at: datetime | None = None,
        bot: bool = False,
        webhook: bool = False,
        mtype: int = 0,
        content: str = "",
        attachments: list[dict] | None = None,
        sticker: bool = False,
        nick: str | None = None,
        seq: int = 1,
    ) -> discord.Message:
        created_at = created_at or datetime(2026, 3, 10, 12, tzinfo=UTC)
        data: dict[str, Any] = {
            "id": str(snowflake_at(created_at, seq)),
            "channel_id": str(channel.id),
            "guild_id": str(GUILD),
            "author": {"id": str(author_id), "username": f"user{author_id}", "discriminator": "0",
                       "avatar": None, "global_name": f"Global {author_id}", "bot": bot},
            "content": content,
            "timestamp": created_at.isoformat(),
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": attachments or [],
            "embeds": [],
            "pinned": False,
            "type": mtype,
        }
        if webhook:
            data["webhook_id"] = "777"
        if sticker:
            data["sticker_items"] = [{"id": "888", "name": "wave", "format_type": 1}]
        if not webhook:
            data["member"] = {"roles": [], "joined_at": "2025-01-01T00:00:00+00:00", "deaf": False,
                              "mute": False, "nick": nick, "flags": 0}
        if mtype == 19:
            data["message_reference"] = {"message_id": str(snowflake_at(created_at) - 1), "channel_id": str(channel.id)}
        return discord.Message(state=self.state, channel=channel, data=data)


@pytest.fixture
async def world() -> Any:
    w = World()
    yield w
    await w.client.close()


@pytest.fixture
def counter(app: App, fake: FakeDiscord) -> MessageCounter:
    return app.services.counter


async def counted_total(app: App) -> int:
    assert await app.buffer.flush()
    rows = await app.repo.aggregate_counts(GUILD)
    return sum(r.total for r in rows)


async def exclude(app: App, channel_id: int) -> None:
    await app.repo.add_exclusion(GUILD, channel_id, app.services.clock.now())
    app.services.exclusions.set_excluded(GUILD, channel_id, True)


# ------------------------------------------------------------------ basics


async def test_ordinary_guild_message_counts(app: App, world: World, counter: MessageCounter) -> None:
    assert await counter.handle(world.message(world.text, content="hello"))
    assert await counted_total(app) == 1


async def test_bot_accounts_not_counted(app: App, world: World, counter: MessageCounter) -> None:
    assert not await counter.handle(world.message(world.text, bot=True))
    assert await counted_total(app) == 0


async def test_webhook_messages_not_counted(app: App, world: World, counter: MessageCounter) -> None:
    msg = world.message(world.text, webhook=True)
    assert rejection_reason(msg) == "webhook"
    assert not await counter.handle(msg)


@pytest.mark.parametrize("mtype", [7, 6, 8, 18, 21, 20])  # join, pin, boost, thread created, starter, slash
async def test_system_messages_not_counted(app: App, world: World, counter: MessageCounter, mtype: int) -> None:
    assert not await counter.handle(world.message(world.text, mtype=mtype))
    assert await counted_total(app) == 0


async def test_dm_not_counted(app: App, counter: MessageCounter) -> None:
    client = discord.Client(intents=discord.Intents.none())
    try:
        state = client._connection
        dm = discord.DMChannel(me=None, state=state, data={"id": "5", "type": 1, "recipients": []})  # type: ignore[arg-type]
        data = {"id": "123456789012345678", "channel_id": "5", "author": {"id": "11", "username": "u", "discriminator": "0", "avatar": None},
                "content": "hi", "timestamp": "2026-03-10T12:00:00+00:00", "edited_timestamp": None, "tts": False,
                "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
                "pinned": False, "type": 0}
        msg = discord.Message(state=state, channel=dm, data=data)
        assert msg.guild is None
        assert rejection_reason(msg) == "dm"
        assert not await counter.handle(msg)
    finally:
        await client.close()


async def test_replies_count(app: App, world: World, counter: MessageCounter) -> None:
    msg = world.message(world.text, mtype=19)
    assert msg.type is discord.MessageType.reply
    assert await counter.handle(msg)
    assert await counted_total(app) == 1


async def test_attachment_only_and_sticker_messages_count(app: App, world: World, counter: MessageCounter) -> None:
    attachment = {"id": "999", "filename": "a.png", "size": 10, "url": "https://cdn.example/a.png",
                  "proxy_url": "https://media.example/a.png"}
    assert await counter.handle(world.message(world.text, content="", attachments=[attachment], seq=1))
    assert await counter.handle(world.message(world.text, content="", sticker=True, seq=2))
    assert await counted_total(app) == 2


async def test_voice_text_chat_counts_and_honours_category(app: App, world: World, counter: MessageCounter) -> None:
    assert await counter.handle(world.message(world.voice, seq=1))
    await exclude(app, OTHER_CATEGORY)
    assert not await counter.handle(world.message(world.voice, seq=2))
    assert await counted_total(app) == 1


# ------------------------------------------------------------- exclusions


async def test_excluded_channel(app: App, world: World, counter: MessageCounter) -> None:
    await exclude(app, TEXT)
    assert not await counter.handle(world.message(world.text, seq=1))
    assert await counter.handle(world.message(world.other_text, seq=2))
    assert await counted_total(app) == 1


async def test_excluded_category_excludes_its_channels(app: App, world: World, counter: MessageCounter) -> None:
    await exclude(app, CATEGORY)
    assert not await counter.handle(world.message(world.text, seq=1))
    assert await counter.handle(world.message(world.other_text, seq=2))
    assert await counted_total(app) == 1


async def test_thread_inherits_parent_exclusion(app: App, world: World, counter: MessageCounter) -> None:
    await exclude(app, TEXT)
    assert not await counter.handle(world.message(world.thread))
    assert await counted_total(app) == 0


async def test_thread_inherits_parent_category_exclusion(app: App, world: World, counter: MessageCounter) -> None:
    await exclude(app, CATEGORY)
    assert not await counter.handle(world.message(world.thread))
    assert await counted_total(app) == 0


async def test_forum_post_inherits_forum_and_category_exclusion(app: App, world: World, counter: MessageCounter) -> None:
    assert await counter.handle(world.message(world.forum_post, seq=1))
    await exclude(app, FORUM)
    assert not await counter.handle(world.message(world.forum_post, seq=2))
    await app.repo.remove_exclusion(GUILD, FORUM)
    app.services.exclusions.set_excluded(GUILD, FORUM, False)
    await exclude(app, CATEGORY)
    assert not await counter.handle(world.message(world.forum_post, seq=3))
    assert await counted_total(app) == 1


async def test_removing_exclusion_restores_counting(app: App, world: World, counter: MessageCounter) -> None:
    await exclude(app, TEXT)
    assert not await counter.handle(world.message(world.text, seq=1))
    await app.repo.remove_exclusion(GUILD, TEXT)
    app.services.exclusions.set_excluded(GUILD, TEXT, False)
    assert await counter.handle(world.message(world.text, seq=2))


async def test_exclusions_survive_restart(config: Any, fake: FakeDiscord, clock: Any, world: World) -> None:
    first = await App.start(config, fake, clock)
    await exclude(first, CATEGORY)
    await first.shutdown()
    second = await App.start(config, fake, clock)
    try:
        assert not await second.services.counter.handle(world.message(world.thread))
    finally:
        await second.shutdown()


def test_ancestry_from_real_channel_objects(world: World) -> None:
    assert ancestry_from_cache(world.text) == ChannelInfo(TEXT, None, CATEGORY)
    assert ancestry_from_cache(world.thread) == ChannelInfo(THREAD, TEXT, CATEGORY)
    assert ancestry_from_cache(world.forum_post) == ChannelInfo(FORUM_POST, FORUM, CATEGORY)
    assert ancestry_from_cache(world.voice) == ChannelInfo(VOICE, None, OTHER_CATEGORY)
    assert ancestry_from_cache(world.client.get_partial_messageable(5, guild_id=GUILD)) is None


async def test_uncached_thread_resolved_through_api(app: App, world: World, fake: FakeDiscord, counter: MessageCounter) -> None:
    uncached = world.client.get_partial_messageable(70, guild_id=GUILD)
    fake.channel_info[70] = ChannelInfo(70, TEXT, CATEGORY)
    msg = world.message(world.text)
    msg.channel = uncached
    await exclude(app, CATEGORY)
    assert not await counter.handle(msg)  # resolved ancestry includes the excluded category
    msg2 = world.message(world.text, seq=2)
    msg2.channel = uncached
    await app.repo.remove_exclusion(GUILD, CATEGORY)
    app.services.exclusions.set_excluded(GUILD, CATEGORY, False)
    assert await counter.handle(msg2)
    assert fake.count("resolve_channel") == 1  # second lookup served from the resolver cache


async def test_unresolvable_channel_fails_closed(app: App, world: World, fake: FakeDiscord, counter: MessageCounter) -> None:
    msg = world.message(world.text)
    msg.channel = world.client.get_partial_messageable(71, guild_id=GUILD)
    fake.fail_next("resolve_channel", OpNetwork("timeout"))
    assert not await counter.handle(msg)
    assert await counted_total(app) == 0


# --------------------------------------------------------------- timestamps


async def test_message_created_at_235959_counts_for_that_day_even_if_late(app: App, world: World, counter: MessageCounter) -> None:
    created = datetime(2026, 3, 10, 23, 59, 59, tzinfo=UTC)
    app.services.clock.set(created + timedelta(seconds=30))  # event arrives after midnight
    assert await counter.handle(world.message(world.text, created_at=created))
    assert await app.buffer.flush()
    day10 = await app.repo.aggregate_counts(GUILD, "2026-03-10", "2026-03-11")
    day11 = await app.repo.aggregate_counts(GUILD, "2026-03-11", "2026-03-12")
    assert [r.total for r in day10] == [1]
    assert day11 == []


# ---------------------------------------------------------------- dedupe


async def test_duplicate_events_within_process_counted_once(app: App, world: World, counter: MessageCounter) -> None:
    msg = world.message(world.text)
    assert await counter.handle(msg)
    assert not await counter.handle(msg)
    assert await counted_total(app) == 1
    # A duplicate arriving after a flush is still suppressed (the LRU outlives flushes).
    assert not await counter.handle(msg)
    assert await counted_total(app) == 1


async def test_concurrent_duplicate_handling_counts_once(app: App, world: World, fake: FakeDiscord) -> None:
    """Both handlers await channel resolution, then race to record the same message."""
    gate = asyncio.Event()
    fake.channel_info[72] = ChannelInfo(72, TEXT, CATEGORY)

    class SlowResolver(ChannelResolver):
        async def resolve(self, guild_id: int, channel: Any) -> ChannelInfo | None:
            await gate.wait()
            return await super().resolve(guild_id, channel)

    counter = MessageCounter(app.services.exclusions, SlowResolver(fake), app.buffer)
    msg = world.message(world.text)
    msg.channel = world.client.get_partial_messageable(72, guild_id=GUILD)
    tasks = [asyncio.create_task(counter.handle(msg)) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results.count(True) == 1
    assert await counted_total(app) == 1


# --------------------------------------------------------------- metadata


async def test_member_metadata_upserted_and_updated(app: App, world: World, counter: MessageCounter) -> None:
    t1 = datetime(2026, 3, 10, 12, tzinfo=UTC)
    await counter.handle(world.message(world.text, created_at=t1, nick="Nick One", seq=1))
    assert await app.buffer.flush()
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None
    assert (member.display_name, member.username, member.last_seen_at) == ("Nick One", "user11", t1)

    t2 = t1 + timedelta(hours=1)
    await counter.handle(world.message(world.text, created_at=t2, nick="Nick Two", seq=2))
    assert await app.buffer.flush()
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and (member.display_name, member.last_seen_at) == ("Nick Two", t2)


async def test_departed_member_returning_clears_left_at(app: App, world: World, counter: MessageCounter) -> None:
    t1 = datetime(2026, 3, 10, 12, tzinfo=UTC)
    await counter.handle(world.message(world.text, created_at=t1, seq=1))
    assert await app.buffer.flush()
    assert await app.repo.mark_member_left(GUILD, 11, t1 + timedelta(minutes=5))
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and member.left_at is not None
    # A stale message from before the departure must not clear left_at...
    app.record(GUILD, 11, t1 + timedelta(minutes=1))
    assert await app.buffer.flush()
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and member.left_at is not None
    # ...but a new message after it does.
    await counter.handle(world.message(world.text, created_at=t1 + timedelta(minutes=10), seq=3))
    assert await app.buffer.flush()
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and member.left_at is None


def test_edits_and_deletes_have_no_handlers() -> None:
    """Edits never add points and deletes never subtract: the bot does not listen to them."""
    for name in ("on_message_edit", "on_raw_message_edit", "on_message_delete", "on_raw_message_delete",
                 "on_raw_bulk_message_delete"):
        assert not hasattr(LeaderboardBot, name)


def test_only_non_privileged_intents() -> None:
    from bot.main import make_intents

    intents = make_intents()
    assert intents.guilds and intents.guild_messages
    assert not intents.members and not intents.message_content and not intents.presences
    enabled = {name for name, value in intents if value}
    assert enabled == {"guilds", "guild_messages"}


async def test_exclusion_cache_loads_from_db(app: App) -> None:
    await app.repo.add_exclusion(GUILD, 5, app.services.clock.now())
    cache = ExclusionCache(app.repo)
    await cache.load()
    assert set(cache.excluded_ids(GUILD)) == {5}
    assert set(cache.excluded_ids(999)) == set()
