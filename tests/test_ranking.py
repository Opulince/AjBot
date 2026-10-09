"""Leaderboard totals, ordering and the first-to-final-total tie-break.

The core proof is ``test_matches_brute_force_replay``: an independent
implementation replays every message in creation order, records the moment each
user reached their final period total, and must agree with the SQL-backed
ranking for every daily, weekly, monthly and all-time range — including streams
with messages after the period, same-millisecond milestones and late
deliveries.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

import pytest

from bot.clock import UTC
from bot.commands.stats import user_standings
from bot.periods import period_containing
from bot.ranking import Ranker, milestone_ms, rank_rows, sort_key
from bot.db.repo import AggregateRow
from bot.ui.embeds import build_board_embed
from tests.conftest import GUILD, install
from tests.fakes import App, FakeDiscord, snowflake_at


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def ranking(app: App, start: datetime | None, end: datetime | None, limit: int | None = None) -> list[Any]:
    return await app.services.ranker.ranking(GUILD, start, end, limit=limit)


async def order(app: App, start: datetime | None = None, end: datetime | None = None) -> list[tuple[int, int]]:
    return [(e.user_id, e.count) for e in await ranking(app, start, end)]


# --------------------------------------------------------------- totals


async def test_daily_weekly_monthly_alltime_totals(app: App) -> None:
    # User 11: 2 on Mar 9 (Mon), 3 on Mar 10, 1 on Mar 31, 4 on Apr 1.
    await app.messages(GUILD, 11, dt(2026, 3, 9, 10), 2)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 10), 3)
    await app.messages(GUILD, 11, dt(2026, 3, 31, 10), 1)
    await app.messages(GUILD, 11, dt(2026, 4, 1, 10), 4)
    day = period_containing("daily", dt(2026, 3, 10))
    week = period_containing("weekly", dt(2026, 3, 10))
    month = period_containing("monthly", dt(2026, 3, 10))
    assert await order(app, day.start, day.end) == [(11, 3)]
    assert await order(app, week.start, week.end) == [(11, 5)]
    assert await order(app, month.start, month.end) == [(11, 6)]
    assert await order(app) == [(11, 10)]
    april = period_containing("monthly", dt(2026, 4, 1))
    assert await order(app, april.start, april.end) == [(11, 4)]


async def test_ranking_by_descending_count(app: App) -> None:
    await app.messages(GUILD, 11, dt(2026, 3, 10, 9), 2)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 9), 5)
    await app.messages(GUILD, 13, dt(2026, 3, 10, 9), 3)
    entries = await ranking(app, None, None)
    assert [(e.rank, e.user_id, e.count) for e in entries] == [(1, 12, 5), (2, 13, 3), (3, 11, 2)]


async def test_limit_is_top_100_by_default(app: App) -> None:
    for user in range(1000, 1130):
        app.record(GUILD, user, dt(2026, 3, 10, 9))
    assert await app.buffer.flush()
    assert len(await app.services.ranker.ranking(GUILD, None, None)) == 100
    assert len(await app.services.ranker.ranking(GUILD, None, None, limit=None)) == 130


# ------------------------------------------------------------- tie-breaks


async def test_first_to_reach_final_total_wins_tie(app: App) -> None:
    # 12 started earlier, but 11 reached the final total (3) first.
    for minute in (0, 1):
        app.record(GUILD, 12, dt(2026, 3, 10, 9, minute))
    for minute in (0, 5, 10):
        app.record(GUILD, 11, dt(2026, 3, 10, 10, minute))
    app.record(GUILD, 12, dt(2026, 3, 10, 10, 20))
    assert await app.buffer.flush()
    assert await order(app) == [(11, 3), (12, 3)]


async def test_tie_spanning_multiple_days(app: App) -> None:
    week = period_containing("weekly", dt(2026, 3, 10))
    # 11 reaches 4 on Tuesday; 12 reaches 4 on Wednesday despite more activity Monday.
    await app.messages(GUILD, 12, dt(2026, 3, 9, 8), 3)
    await app.messages(GUILD, 11, dt(2026, 3, 9, 20), 2)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 8), 2)
    await app.messages(GUILD, 12, dt(2026, 3, 11, 8), 1)
    assert await order(app, week.start, week.end) == [(11, 4), (12, 4)]
    # The daily ranking for Monday is a different contest.
    monday = period_containing("daily", dt(2026, 3, 9))
    assert await order(app, monday.start, monday.end) == [(12, 3), (11, 2)]


async def test_messages_after_the_period_do_not_affect_historical_tie_break(app: App) -> None:
    """The last-message-in-history (or last_seen_at) must never replace the period milestone."""
    yesterday = period_containing("daily", dt(2026, 3, 10))
    await app.messages(GUILD, 11, dt(2026, 3, 10, 14), 3)  # 11 reaches 3 at 14:00:02
    await app.messages(GUILD, 12, dt(2026, 3, 10, 15), 3)  # 12 reaches 3 at 15:00:02
    await app.messages(GUILD, 11, dt(2026, 3, 11, 9), 5)  # 11 keeps chatting the next day
    member11 = await app.repo.get_member(GUILD, 11)
    member12 = await app.repo.get_member(GUILD, 12)
    assert member11 is not None and member12 is not None
    assert member11.last_seen_at > member12.last_seen_at  # last-seen would wrongly favour 12
    assert await order(app, yesterday.start, yesterday.end) == [(11, 3), (12, 3)]


async def test_uncounted_later_messages_do_not_change_milestone(app: App, fake: FakeDiscord) -> None:
    """Messages in excluded channels after reaching the total are not counted at all."""
    await app.messages(GUILD, 11, dt(2026, 3, 10, 9), 2)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 10), 2)
    # 11 then posts only in an excluded channel: nothing reaches the buffer.
    entries = await ranking(app, None, None)
    assert [e.user_id for e in entries] == [11, 12]


async def test_same_millisecond_milestones_fall_back_to_user_id(app: App) -> None:
    moment = dt(2026, 3, 10, 9, 0, 0)
    # User 15's milestone snowflake is *numerically* lower than user 12's, but
    # both are in the same millisecond, so ascending user ID decides.
    app.record(GUILD, 15, moment, seq=1)
    app.record(GUILD, 12, moment, seq=400)
    assert await app.buffer.flush()
    assert await order(app) == [(12, 1), (15, 1)]


def test_sort_key_uses_millisecond_timestamp() -> None:
    a = snowflake_at(dt(2026, 3, 10, 9), 5)
    b = snowflake_at(dt(2026, 3, 10, 9), 1)
    assert milestone_ms(a) == milestone_ms(b)
    assert sort_key(3, a, 1) < sort_key(3, b, 2)
    assert sort_key(4, a, 9) < sort_key(3, b, 1)


def test_rank_rows_assigns_unique_sequential_ranks() -> None:
    rows = [AggregateRow(u, 1, snowflake_at(dt(2026, 3, 10), 0), None, None, None) for u in (30, 10, 20)]
    ranks = rank_rows(rows)
    assert [(r.rank, r.user_id) for r in ranks] == [(1, 10), (2, 20), (3, 30)]


def _expected_ranking(messages: list[tuple[int, int]], start: datetime | None, end: datetime | None) -> list[tuple[int, int]]:
    """Independent oracle: replay in creation order, remember when each user hit their final total."""
    in_range = []
    for user, snowflake in messages:
        created = datetime.fromtimestamp(((snowflake >> 22) + 1420070400000) / 1000, tz=UTC)
        if start is None or start <= created < end:  # type: ignore[operator]
            in_range.append((snowflake, user))
    in_range.sort()
    totals: dict[int, int] = {}
    for _, user in in_range:
        totals[user] = totals.get(user, 0) + 1
    running: dict[int, int] = {}
    reached_ms: dict[int, int] = {}
    for snowflake, user in in_range:
        running[user] = running.get(user, 0) + 1
        if running[user] == totals[user]:
            reached_ms[user] = snowflake >> 22
    ranked = sorted(totals, key=lambda u: (-totals[u], reached_ms[u], u))
    return [(u, totals[u]) for u in ranked]


@pytest.mark.parametrize("seed", range(12))
async def test_matches_brute_force_replay(app: App, seed: int) -> None:
    rng = random.Random(seed)
    users = list(range(21, 21 + rng.randint(3, 8)))
    base = dt(2026, 2, 23)  # Monday; stream spans late Feb into March (two months, 3+ weeks)
    messages: list[tuple[int, int]] = []
    for _ in range(rng.randint(40, 160)):
        user = rng.choice(users)
        # Coarse times create plenty of exact ties; some messages share milliseconds.
        created = base + timedelta(days=rng.randint(0, 20), hours=rng.choice([0, 6, 12, 23]),
                                   minutes=rng.choice([0, 59]), seconds=rng.choice([0, 59]))
        seq = rng.randint(1, 4000)
        mid = snowflake_at(created, seq)
        if any(m == mid for _, m in messages):
            continue
        messages.append((user, mid))
    # Deliver in random order (late deliveries, out-of-order flushes).
    shuffled = messages[:]
    rng.shuffle(shuffled)
    for i, (user, mid) in enumerate(shuffled):
        created = datetime.fromtimestamp(((mid >> 22) + 1420070400000) / 1000, tz=UTC)
        app.services.buffer.record(guild_id=GUILD, user_id=user, message_id=mid, created_at=created,
                                   display_name="x", username="x")
        if i % 17 == 0:
            assert await app.buffer.flush()
    assert await app.buffer.flush()

    ranges: list[tuple[datetime | None, datetime | None]] = [(None, None)]
    for day in range(21):
        p = period_containing("daily", base + timedelta(days=day))
        ranges.append((p.start, p.end))
    for week in range(3):
        p = period_containing("weekly", base + timedelta(weeks=week))
        ranges.append((p.start, p.end))
    for moment in (dt(2026, 2, 25), dt(2026, 3, 5)):
        p = period_containing("monthly", moment)
        ranges.append((p.start, p.end))
    for start, end in ranges:
        expected = _expected_ranking(messages, start, end)
        actual = [(e.user_id, e.count) for e in await app.services.ranker.ranking(GUILD, start, end, limit=None)]
        assert actual == expected, (start, end)


# -------------------------------------------------------- boards == stats


async def test_board_and_stats_use_identical_ordering(app: App) -> None:
    await install(app, roles=False)
    rng = random.Random(7)
    for user in range(31, 61):
        for _ in range(rng.randint(1, 4)):
            app.record(GUILD, user, dt(2026, 3, 10, rng.randint(0, 11), rng.choice([0, 30])))
    assert await app.buffer.flush()
    now = app.services.clock.now()
    for board_type in ("daily", "weekly", "monthly", "alltime"):
        board = await app.services.ranker.board_ranking(GUILD, board_type, now)
        for entry in board:
            standing = (await user_standings(app.services, GUILD, entry.user_id))[board_type]
            assert (standing.rank, standing.count) == (entry.rank, entry.count)
    nobody = await user_standings(app.services, GUILD, 424242)
    assert all(s.rank is None and s.count == 0 for s in nobody.values())


async def test_board_embed_lists_entries_in_ranking_order(app: App) -> None:
    await app.messages(GUILD, 12, dt(2026, 3, 10, 9), 2)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 9), 4)
    entries = await app.services.ranker.board_ranking(GUILD, "daily", app.services.clock.now())
    embed = build_board_embed(guild_id=GUILD, board_type="daily", generation=1, entries=entries, previous=None,
                              next_reset=dt(2026, 3, 11), banner_url=None, now=app.services.clock.now())
    desc = embed.description or ""
    assert desc.index("<@11>") < desc.index("<@12>")


# ------------------------------------------------------------- departed


async def test_departed_members_remain_visible_with_label(app: App) -> None:
    await app.messages(GUILD, 11, dt(2026, 3, 10, 9), 3, name="Alice")
    await app.messages(GUILD, 12, dt(2026, 3, 10, 9), 1, name="Bob")
    await app.repo.mark_member_left(GUILD, 11, dt(2026, 3, 10, 11))
    entries = await ranking(app, None, None)
    assert [(e.user_id, e.left, e.name) for e in entries] == [(11, True, "Alice"), (12, False, "Bob")]
    embed = build_board_embed(guild_id=GUILD, board_type="alltime", generation=1, entries=entries, previous=None,
                              next_reset=None, banner_url=None, now=app.services.clock.now())
    assert "<@11> · Alice (left)" in (embed.description or "")


async def test_departed_members_skipped_for_rewards(app: App, fake: FakeDiscord, clock: Any) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 5)  # top user...
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 3)
    fake.remove_member(GUILD, 11)  # ...has left the server
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await app.repo.get_run_by_key(GUILD, "daily", "2026-03-10")
    assert run is not None and run.winner_user_id == 12 and run.winner_count == 3
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and member.left_at is not None  # confirmed departure recorded
    # Historical counts are kept.
    assert [(e.user_id, e.count) for e in await ranking(app, None, None)][0] == (11, 5)
