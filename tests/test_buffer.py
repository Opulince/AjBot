"""Buffered persistence: flushing, atomic increments, rollback, merge-back, retries."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pytest

from bot.clock import UTC
from bot.counting.buffer import CountBuffer
from bot.db.repo import Repo
from tests.conftest import GUILD
from tests.fakes import App, snowflake_at

T = datetime(2026, 3, 10, 12, tzinfo=UTC)


async def db_counts(app: App) -> dict[tuple[int, str], tuple[int, int]]:
    async with app.services.db.read() as conn:
        async with conn.execute("SELECT user_id, day, count, last_message_id FROM daily_counts") as cur:
            rows = await cur.fetchall()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def rec(buffer: CountBuffer, user: int, at: datetime, seq: int, name: str = "n") -> int:
    mid = snowflake_at(at, seq)
    buffer.record(guild_id=GUILD, user_id=user, message_id=mid, created_at=at, display_name=name, username=name)
    return mid


async def test_normal_flush(app: App) -> None:
    buf = app.buffer
    for i in range(3):
        rec(buf, 11, T + timedelta(seconds=i), i + 1)
    rec(buf, 12, T, 10)
    assert buf.pending_messages == 4
    assert await buf.flush()
    assert buf.pending_messages == 0
    counts = await db_counts(app)
    assert counts[(11, "2026-03-10")][0] == 3
    assert counts[(12, "2026-03-10")][0] == 1


async def test_flush_increments_rather_than_overwrites(app: App) -> None:
    buf = app.buffer
    rec(buf, 11, T, 1)
    rec(buf, 11, T, 2)
    assert await buf.flush()
    rec(buf, 11, T + timedelta(minutes=1), 3)
    assert await buf.flush()
    assert (await db_counts(app))[(11, "2026-03-10")][0] == 3


async def test_flush_of_empty_buffer_is_a_noop(app: App) -> None:
    assert await app.buffer.flush()
    assert await db_counts(app) == {}


async def test_failed_commit_keeps_counts_in_memory(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = app.buffer
    rec(buf, 11, T, 1)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.repo, "apply_count_batch", broken)
    assert not await buf.flush()
    assert buf.pending_messages == 1
    assert buf.consecutive_failures == 1
    assert await db_counts(app) == {}


async def test_mid_transaction_failure_rolls_back_and_merges_back(app: App) -> None:
    """The counts statement succeeds, the members statement fails: nothing may persist."""
    buf = app.buffer
    rec(buf, 11, T, 1)
    rec(buf, 11, T, 2)
    async with app.services.db.transaction() as conn:
        await conn.execute(
            "CREATE TRIGGER fail_members BEFORE INSERT ON members BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    assert not await buf.flush()
    assert await db_counts(app) == {}  # the daily_counts insert was rolled back
    assert buf.pending_messages == 2
    async with app.services.db.transaction() as conn:
        await conn.execute("DROP TRIGGER fail_members")
    assert await buf.flush()
    assert (await db_counts(app))[(11, "2026-03-10")][0] == 2  # exactly once


async def test_merge_back_preserves_concurrent_increments_and_max_id(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = app.buffer
    real_apply = app.repo.apply_count_batch
    started = asyncio.Event()
    release = asyncio.Event()
    fail = {"value": True}

    async def slow_apply(counts: Any, members: Any) -> None:
        started.set()
        await release.wait()
        if fail["value"]:
            raise sqlite3.OperationalError("disk I/O error")
        await real_apply(counts, members)

    monkeypatch.setattr(app.repo, "apply_count_batch", slow_apply)
    high = rec(buf, 11, T + timedelta(seconds=30), 5)
    rec(buf, 11, T, 1)
    flush = asyncio.create_task(buf.flush())
    await started.wait()
    # Messages keep arriving while the snapshot is being written.
    low = rec(buf, 11, T + timedelta(seconds=1), 2)
    rec(buf, 12, T, 3)
    release.set()
    assert not await flush
    pending = buf.snapshot_pending()
    assert pending[(GUILD, 11, "2026-03-10")] == (3, high)  # max ID preserved across merge
    assert low < high
    assert pending[(GUILD, 12, "2026-03-10")][0] == 1

    fail["value"] = False
    release.set()
    assert await buf.flush()
    counts = await db_counts(app)
    assert counts[(11, "2026-03-10")] == (3, high)
    assert counts[(12, "2026-03-10")][0] == 1


async def test_concurrent_messages_during_successful_flush(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = app.buffer
    real_apply = app.repo.apply_count_batch
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_apply(counts: Any, members: Any) -> None:
        started.set()
        await release.wait()
        await real_apply(counts, members)

    monkeypatch.setattr(app.repo, "apply_count_batch", slow_apply)
    rec(buf, 11, T, 1)
    flush = asyncio.create_task(buf.flush())
    await started.wait()
    for i in range(5):
        rec(buf, 11, T + timedelta(seconds=i + 1), 10 + i)
    release.set()
    assert await flush
    assert buf.pending_messages == 5
    assert await buf.flush()
    assert (await db_counts(app))[(11, "2026-03-10")][0] == 6


async def test_repeated_failures_then_success_with_bounded_backoff(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = app.buffer
    real_apply = app.repo.apply_count_batch
    failures = {"left": 4}

    async def flaky(counts: Any, members: Any) -> None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        await real_apply(counts, members)

    monkeypatch.setattr(app.repo, "apply_count_batch", flaky)
    total = 0
    delays = []
    for round_ in range(4):
        rec(buf, 11, T + timedelta(seconds=round_), round_ + 1)
        total += 1
        assert not await buf.flush()
        delays.append(buf.next_delay())
        assert buf.pending_messages == total  # nothing silently dropped
    assert buf.consecutive_failures == 4
    assert all(d <= 60 * 1.15 for d in delays)
    assert delays[0] < delays[-1]
    assert await buf.flush()
    assert buf.consecutive_failures == 0
    assert buf.next_delay() == 5.0
    assert (await db_counts(app))[(11, "2026-03-10")][0] == total


async def test_db_max_id_is_kept_when_lower_id_flushed_later(app: App) -> None:
    buf = app.buffer
    high = rec(buf, 11, T + timedelta(seconds=50), 9)
    assert await buf.flush()
    rec(buf, 11, T, 1)  # an older message delivered late
    assert await buf.flush()
    assert (await db_counts(app))[(11, "2026-03-10")] == (2, high)


async def test_buckets_split_by_utc_day(app: App) -> None:
    buf = app.buffer
    rec(buf, 11, datetime(2026, 3, 10, 23, 59, 59, tzinfo=UTC), 1)
    rec(buf, 11, datetime(2026, 3, 11, 0, 0, 0, tzinfo=UTC), 2)
    assert await buf.flush()
    counts = await db_counts(app)
    assert counts[(11, "2026-03-10")][0] == 1 and counts[(11, "2026-03-11")][0] == 1


async def test_graceful_shutdown_flush_survives_restart(config: Any, fake: Any, clock: Any) -> None:
    first = await App.start(config, fake, clock)
    for i in range(7):
        rec(first.buffer, 11, T + timedelta(seconds=i), i + 1)
    await first.shutdown()  # final flush happens here
    second = await App.start(config, fake, clock)
    try:
        assert (await db_counts(second))[(11, "2026-03-10")][0] == 7
    finally:
        await second.shutdown()


async def test_cancelled_caller_cannot_lose_or_duplicate_a_snapshot(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = app.buffer
    real_apply = app.repo.apply_count_batch
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_apply(counts: Any, members: Any) -> None:
        started.set()
        await release.wait()
        await real_apply(counts, members)

    monkeypatch.setattr(app.repo, "apply_count_batch", slow_apply)
    rec(buf, 11, T, 1)
    rec(buf, 11, T, 2)
    caller = asyncio.create_task(buf.flush())
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    assert await buf.flush()  # waits for the shielded write, then flushes the rest
    assert (await db_counts(app))[(11, "2026-03-10")][0] == 2


async def test_dedupe_cache_is_bounded(app: App) -> None:
    buf = CountBuffer(app.repo, dedupe_capacity=3)
    ids = [rec(buf, 11, T, i) for i in range(1, 5)]  # 4 distinct messages, capacity 3
    assert buf.pending_messages == 4
    # A recent ID is still recognised as a duplicate...
    assert not buf.record(guild_id=GUILD, user_id=11, message_id=ids[-1], created_at=T, display_name="n", username="n")
    # ...the oldest has been evicted: this is the documented in-memory dedupe limitation.
    assert buf.record(guild_id=GUILD, user_id=11, message_id=ids[0], created_at=T, display_name="n", username="n")


async def test_periodic_loop_flushes_and_stops(app: App) -> None:
    buf = CountBuffer(app.repo, flush_interval=0.05)
    rec(buf, 11, T, 1)
    stop = asyncio.Event()
    task = asyncio.create_task(buf.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if buf.pending_messages == 0:
            break
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert buf.pending_messages == 0
    assert (await db_counts(app))[(11, "2026-03-10")][0] == 1


async def test_repo_failure_never_silently_drops(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Interleave many failures and successes: the DB total must equal what was recorded."""
    buf = app.buffer
    real_apply = Repo.apply_count_batch
    toggle = {"n": 0}

    async def sometimes(self: Repo, counts: Any, members: Any) -> None:
        toggle["n"] += 1
        if toggle["n"] % 3 != 0:
            raise sqlite3.OperationalError("flaky")
        await real_apply(self, counts, members)

    monkeypatch.setattr(Repo, "apply_count_batch", sometimes)
    recorded = 0
    for i in range(30):
        rec(buf, 11 + (i % 3), T + timedelta(seconds=i), i + 1)
        recorded += 1
        await buf.flush()
    monkeypatch.setattr(Repo, "apply_count_batch", real_apply)
    assert await buf.flush()
    assert sum(c for c, _ in (await db_counts(app)).values()) == recorded
