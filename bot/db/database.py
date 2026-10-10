"""SQLite connection management, migrations and transactions.

A single aiosqlite connection is used, guarded by an :class:`asyncio.Lock`.
aiosqlite serialises statements on its worker thread, but it does *not* stop two
coroutines from interleaving statements inside one logical transaction, so every
read and write goes through :meth:`Database.transaction` or :meth:`Database.read`,
which hold the lock for their whole duration. Re-entrant use from the same task
raises immediately instead of deadlocking.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _load_migrations() -> list[tuple[int, str]]:
    """Ordered list of ``(version, sql)``. Append new migrations; never edit old ones."""
    return [
        (1, _SCHEMA_FILE.read_text(encoding="utf-8")),
    ]


MIGRATIONS = _load_migrations()
LATEST_VERSION = MIGRATIONS[-1][0]

BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """Raised for unrecoverable database setup problems."""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the database, apply pragmas and run pending migrations."""
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit mode, transactions are explicit.
        conn = await aiosqlite.connect(str(self.path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            async with conn.execute("PRAGMA journal_mode = WAL") as cur:
                row = await cur.fetchone()
            mode = row[0] if row else "unknown"
            if str(mode).lower() != "wal":
                log.warning("SQLite WAL mode unavailable for %s (journal_mode=%s)", self.path, mode)
            await conn.execute("PRAGMA synchronous = FULL")
            await conn.execute("PRAGMA foreign_keys = ON")
            await self._migrate(conn)
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        log.info("Database ready at %s (schema version %d, journal_mode=%s)", self.path, LATEST_VERSION, mode)

    async def close(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            conn, self._conn = self._conn, None
            try:
                await conn.close()
            except Exception:  # pragma: no cover - best effort during shutdown
                log.exception("Error while closing database")
        log.info("Database connection closed")

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        current = int(row[0]) if row else 0
        if current > LATEST_VERSION:
            raise DatabaseError(
                f"Database schema version {current} is newer than this bot supports ({LATEST_VERSION}); "
                "refusing to start to avoid damaging data"
            )
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            log.info("Applying database migration %d", version)
            # executescript() commits any open transaction first; the explicit
            # BEGIN/COMMIT makes the migration and the version bump atomic.
            # The version number is a code constant, never user input.
            script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;"
            try:
                await conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                log.exception("Database migration %d failed and was rolled back", version)
                raise
            log.info("Database migration %d applied", version)

    async def _acquire(self) -> aiosqlite.Connection:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            raise RuntimeError("Database lock is not re-entrant; nested transaction detected")
        await self._lock.acquire()
        if self._conn is None:
            self._lock.release()
            raise DatabaseError("Database is not connected")
        self._owner = task
        return self._conn

    def _release(self) -> None:
        self._owner = None
        self._lock.release()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Exclusive write transaction. Commits on success, rolls back on any exception."""
        conn = await self._acquire()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await self._rollback(conn)
                raise
            try:
                await conn.execute("COMMIT")
            except BaseException:
                await self._rollback(conn)
                raise
        finally:
            self._release()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialised read access (no transaction is opened)."""
        conn = await self._acquire()
        try:
            yield conn
        finally:
            self._release()

    @staticmethod
    async def _rollback(conn: aiosqlite.Connection) -> None:
        try:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
        except (sqlite3.Error, ValueError):  # pragma: no cover - connection already gone
            log.exception("ROLLBACK failed")
