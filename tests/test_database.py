"""Connection setup, migrations, transactions and concurrency safety."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import bot.db.database as database_module
from bot.db.database import LATEST_VERSION, Database, DatabaseClosedError, DatabaseError
from bot.db.repo import CountDelta, MemberUpdate, Repo
from tests.conftest import GUILD, T0

EXPECTED_TABLES = {
    "guild_config", "excluded_channels", "daily_counts", "members", "board_messages",
    "board_repost", "reset_runs", "role_holders",
}


async def scalar(db: Database, sql: str) -> object:
    async with db.read() as conn:
        async with conn.execute(sql) as cur:
            row = await cur.fetchone()
    return row[0]


async def test_fresh_database_is_initialised(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "dir" / "lb.db")  # parent directories are created
    await db.connect()
    try:
        assert await scalar(db, "PRAGMA user_version") == LATEST_VERSION
        assert str(await scalar(db, "PRAGMA journal_mode")).lower() == "wal"
        assert int(await scalar(db, "PRAGMA busy_timeout")) == 5000
        async with db.read() as conn:
            async with conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
                tables = {r[0] for r in await cur.fetchall()}
        assert EXPECTED_TABLES <= tables
    finally:
        await db.close()


async def test_reopen_preserves_data_and_skips_applied_migrations(tmp_path: Path) -> None:
    path = tmp_path / "lb.db"
    db = Database(path)
    await db.connect()
    await Repo(db).setup_guild(GUILD, 100, T0)
    await db.close()
    db2 = Database(path)
    await db2.connect()
    try:
        cfg = await Repo(db2).get_guild_config(GUILD)
        assert cfg is not None and cfg.installed_at == T0
    finally:
        await db2.close()


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lb.db"
    db = Database(path)
    await db.connect()
    async with db.transaction() as conn:
        await conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    await db.close()
    with pytest.raises(DatabaseError, match="newer"):
        await Database(path).connect()


async def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lb.db"
    db = Database(path)
    await db.connect()
    await db.close()
    broken = database_module.MIGRATIONS + [
        (LATEST_VERSION + 1, "CREATE TABLE new_feature (x INTEGER);\nTHIS IS NOT SQL;")
    ]
    monkeypatch.setattr(database_module, "MIGRATIONS", broken)
    monkeypatch.setattr(database_module, "LATEST_VERSION", LATEST_VERSION + 1)
    with pytest.raises(sqlite3.OperationalError):
        await Database(path).connect()
    monkeypatch.undo()
    db = Database(path)
    await db.connect()
    try:
        assert await scalar(db, "PRAGMA user_version") == LATEST_VERSION
        assert await scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'new_feature'") == 0
    finally:
        await db.close()


async def test_future_migration_applies_on_existing_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lb.db"
    db = Database(path)
    await db.connect()
    await Repo(db).setup_guild(GUILD, 100, T0)
    await db.close()
    upgraded = database_module.MIGRATIONS + [
        (LATEST_VERSION + 1, "ALTER TABLE guild_config ADD COLUMN locale TEXT NOT NULL DEFAULT 'en';")
    ]
    monkeypatch.setattr(database_module, "MIGRATIONS", upgraded)
    monkeypatch.setattr(database_module, "LATEST_VERSION", LATEST_VERSION + 1)
    db = Database(path)
    await db.connect()
    try:
        assert await scalar(db, "PRAGMA user_version") == LATEST_VERSION + 1
        assert await scalar(db, f"SELECT locale FROM guild_config WHERE guild_id = {GUILD}") == "en"
        assert (await Repo(db).get_guild_config(GUILD)).installed_at == T0  # type: ignore[union-attr]
    finally:
        await db.close()


async def test_nested_transaction_is_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "lb.db")
    await db.connect()
    try:
        with pytest.raises(RuntimeError, match="re-entrant"):
            async with db.transaction():
                async with db.read():
                    pass
        # The lock was released and the connection is still usable.
        assert await scalar(db, "SELECT 1") == 1
    finally:
        await db.close()


async def test_concurrent_transactions_are_serialised(tmp_path: Path) -> None:
    db = Database(tmp_path / "lb.db")
    await db.connect()
    repo = Repo(db)
    try:
        await repo.setup_guild(GUILD, 100, T0)

        async def bump(i: int) -> None:
            await repo.apply_count_batch([CountDelta(GUILD, 11, "2026-03-10", 1, 1000 + i)], [])

        await asyncio.gather(*(bump(i) for i in range(50)))
        rows = await repo.aggregate_counts(GUILD)
        assert rows[0].total == 50 and rows[0].milestone_id == 1049
    finally:
        await db.close()


async def test_rollback_on_exception(tmp_path: Path) -> None:
    db = Database(tmp_path / "lb.db")
    await db.connect()
    try:
        with pytest.raises(ValueError):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO guild_config (guild_id, updated_at) VALUES (5, 'x')")
                raise ValueError("abort")
        assert await scalar(db, "SELECT COUNT(*) FROM guild_config") == 0
    finally:
        await db.close()


async def test_large_snowflakes_and_hostile_strings_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "lb.db")
    await db.connect()
    repo = Repo(db)
    try:
        big = 2**63 - 1
        hostile = "Robert'); DROP TABLE members;--"
        await repo.apply_count_batch(
            [CountDelta(big, big, "2026-03-10", 1, big)],
            [MemberUpdate(big, big, hostile, hostile, T0)],
        )
        member = await repo.get_member(big, big)
        assert member is not None and member.display_name == hostile
        rows = await repo.aggregate_counts(big)
        assert rows[0].user_id == big and rows[0].milestone_id == big
    finally:
        await db.close()


async def test_cancel_during_begin_never_orphans_a_transaction(tmp_path: Path) -> None:
    """Regression: a task cancelled while BEGIN IMMEDIATE is queued must not leave it open."""
    db = Database(tmp_path / "lb.db")
    await db.connect()
    repo = Repo(db)
    try:
        for _ in range(25):  # cancel at slightly different points each round
            for delay in (0, 0, 1):
                task = asyncio.create_task(repo.set_banner(GUILD, "https://cdn.example.com/x.png", T0))
                for _ in range(delay + 1):
                    await asyncio.sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await repo.apply_count_batch([CountDelta(GUILD, 11, "2026-03-10", 1, 1)], [])
        rows = await repo.aggregate_counts(GUILD)
        assert rows[0].total == 75  # every later write succeeded
        async with db.read() as conn:
            assert not conn.in_transaction
    finally:
        await db.close()


async def test_orphaned_transaction_is_rolled_back_on_next_acquire(tmp_path: Path) -> None:
    db = Database(tmp_path / "lb.db")
    await db.connect()
    repo = Repo(db)
    try:
        conn = db._conn
        assert conn is not None
        await conn.execute("BEGIN IMMEDIATE")  # simulate an orphan left behind by a bug
        await conn.execute("INSERT INTO guild_config (guild_id, updated_at) VALUES (9, 'x')")
        await repo.apply_count_batch([CountDelta(GUILD, 11, "2026-03-10", 1, 1)], [])
        assert (await repo.aggregate_counts(GUILD))[0].total == 1
        assert await repo.get_guild_config(9) is None  # the orphan's write was rolled back
    finally:
        await db.close()


async def test_close_before_connect_finishes_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: close() during connect() must not leave a live connection behind."""
    db = Database(tmp_path / "lb.db")
    original = Database._migrate

    async def slow_migrate(self: Database, conn: object) -> None:
        await asyncio.sleep(0.2)
        await original(self, conn)  # type: ignore[arg-type]

    monkeypatch.setattr(Database, "_migrate", slow_migrate)
    connecting = asyncio.create_task(db.connect())
    await asyncio.sleep(0.05)
    await db.close()
    with pytest.raises(DatabaseClosedError, match="Shutdown requested"):
        await connecting
    assert not db.connected
    with pytest.raises(DatabaseClosedError, match="closed"):
        await db.connect()


async def test_closed_errors_are_distinguishable_from_real_database_errors(tmp_path: Path) -> None:
    """Startup treats DatabaseClosedError as "the stop closed it"; every other DatabaseError is a real failure."""
    assert issubclass(DatabaseClosedError, DatabaseError)  # existing `except DatabaseError` callers keep working
    path = tmp_path / "lb.db"

    never_connected = Database(path)
    with pytest.raises(DatabaseError, match="not connected") as excinfo:
        async with never_connected.read():
            pass
    assert not isinstance(excinfo.value, DatabaseClosedError)

    seeded = Database(path)
    await seeded.connect()
    async with seeded.transaction() as conn:
        await conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    await seeded.close()
    with pytest.raises(DatabaseError, match="newer") as excinfo:
        await Database(path).connect()
    assert not isinstance(excinfo.value, DatabaseClosedError)

    db = Database(tmp_path / "other.db")
    await db.connect()
    await db.close()
    for use in (db.read, db.transaction):  # used after close: a late setup_hook step
        with pytest.raises(DatabaseClosedError):
            async with use():
                pass
