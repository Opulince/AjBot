"""Database queries and persistence operations.

Every statement is parameterised. The only dynamic SQL fragments are column
names chosen from fixed, code-defined whitelists — never user input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

import aiosqlite

from bot.clock import from_iso, to_iso
from bot.db.database import Database
from bot.periods import BOARD_ORDER, Period, PeriodType


class RunStatus(str, Enum):
    PENDING = "PENDING"
    COMPUTED = "COMPUTED"
    ANNOUNCED = "ANNOUNCED"
    ROLES_DONE = "ROLES_DONE"
    DONE = "DONE"
    SKIPPED_EMPTY = "SKIPPED_EMPTY"
    ROLES_SUPERSEDED = "ROLES_SUPERSEDED"


TERMINAL_STATUSES = frozenset({RunStatus.DONE, RunStatus.SKIPPED_EMPTY, RunStatus.ROLES_SUPERSEDED})
# Statuses whose run has a frozen winner.
WINNER_STATUSES = frozenset(
    {RunStatus.COMPUTED, RunStatus.ANNOUNCED, RunStatus.ROLES_DONE, RunStatus.DONE, RunStatus.ROLES_SUPERSEDED}
)
DECIDED_STATUSES = WINNER_STATUSES | {RunStatus.SKIPPED_EMPTY}


class RepostStatus(str, Enum):
    POSTING = "POSTING"
    DELETING = "DELETING"
    DONE = "DONE"


class StaleRunError(Exception):
    """A run changed underneath an update (optimistic status check failed)."""


_ROLE_COLUMNS = {
    PeriodType.DAILY: "role_daily_id",
    PeriodType.WEEKLY: "role_weekly_id",
    PeriodType.MONTHLY: "role_monthly_id",
}

# Columns a reset-run update may touch.
_RUN_FIELDS = frozenset(
    {
        "winner_user_id",
        "winner_count",
        "announcement_channel_id",
        "announcement_message_id",
        "announce_started_at",
        "role_started_at",
        "role_id",
        "attempts",
        "last_error",
        "next_attempt_at",
    }
)
_RUN_DATETIME_FIELDS = frozenset({"announce_started_at", "role_started_at", "next_attempt_at"})


@dataclass(frozen=True)
class GuildConfig:
    guild_id: int
    board_channel_id: int | None
    banner_url: str | None
    role_daily_id: int | None
    role_weekly_id: int | None
    role_monthly_id: int | None
    installed_at: datetime | None
    repost_needed: bool

    def role_for(self, ptype: PeriodType | str) -> int | None:
        ptype = PeriodType(ptype)
        return {
            PeriodType.DAILY: self.role_daily_id,
            PeriodType.WEEKLY: self.role_weekly_id,
            PeriodType.MONTHLY: self.role_monthly_id,
        }[ptype]

    @property
    def installed(self) -> bool:
        return self.installed_at is not None and self.board_channel_id is not None


@dataclass(frozen=True)
class CountDelta:
    guild_id: int
    user_id: int
    day: str
    count: int
    max_message_id: int


@dataclass(frozen=True)
class MemberUpdate:
    guild_id: int
    user_id: int
    display_name: str
    username: str
    last_seen_at: datetime


@dataclass(frozen=True)
class AggregateRow:
    user_id: int
    total: int
    milestone_id: int
    display_name: str | None
    username: str | None
    left_at: datetime | None


@dataclass(frozen=True)
class MemberRow:
    guild_id: int
    user_id: int
    display_name: str
    username: str
    last_seen_at: datetime
    left_at: datetime | None


@dataclass(frozen=True)
class BoardMessage:
    guild_id: int
    board_type: str
    channel_id: int
    message_id: int
    generation: int


@dataclass(frozen=True)
class RepostOp:
    guild_id: int
    generation: int
    channel_id: int
    status: RepostStatus
    new_ids: dict[str, int]
    old_ids: dict[str, tuple[int, int]]
    reason: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ResetRun:
    id: int
    guild_id: int
    period_type: PeriodType
    period_key: str
    period_start: datetime
    period_end: datetime
    is_test: bool
    status: RunStatus
    winner_user_id: int | None
    winner_count: int | None
    announcement_channel_id: int | None
    announcement_message_id: int | None
    announce_started_at: datetime | None
    role_started_at: datetime | None
    role_id: int | None
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def describe(self) -> str:
        kind = "test " if self.is_test else ""
        return f"{kind}run#{self.id} guild={self.guild_id} {self.period_type.value}:{self.period_key}"


@dataclass(frozen=True)
class LaterRuns:
    total: int
    pending: int
    with_winner: int


@dataclass(frozen=True)
class RoleHolder:
    guild_id: int
    period_type: PeriodType
    user_id: int
    role_id: int
    since_key: str


def _dt(value: str | None) -> datetime | None:
    return from_iso(value) if value is not None else None


def _guild_config(row: aiosqlite.Row) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        board_channel_id=row["board_channel_id"],
        banner_url=row["banner_url"],
        role_daily_id=row["role_daily_id"],
        role_weekly_id=row["role_weekly_id"],
        role_monthly_id=row["role_monthly_id"],
        installed_at=_dt(row["installed_at"]),
        repost_needed=bool(row["repost_needed"]),
    )


def _repost(row: aiosqlite.Row) -> RepostOp:
    new_ids = {str(k): int(v) for k, v in json.loads(row["new_ids_json"]).items()}
    old_ids = {str(k): (int(v[0]), int(v[1])) for k, v in json.loads(row["old_ids_json"]).items()}
    return RepostOp(
        guild_id=row["guild_id"],
        generation=row["generation"],
        channel_id=row["channel_id"],
        status=RepostStatus(row["status"]),
        new_ids=new_ids,
        old_ids=old_ids,
        reason=row["reason"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _run(row: aiosqlite.Row) -> ResetRun:
    return ResetRun(
        id=row["id"],
        guild_id=row["guild_id"],
        period_type=PeriodType(row["period_type"]),
        period_key=row["period_key"],
        period_start=from_iso(row["period_start"]),
        period_end=from_iso(row["period_end"]),
        is_test=bool(row["is_test"]),
        status=RunStatus(row["status"]),
        winner_user_id=row["winner_user_id"],
        winner_count=row["winner_count"],
        announcement_channel_id=row["announcement_channel_id"],
        announcement_message_id=row["announcement_message_id"],
        announce_started_at=_dt(row["announce_started_at"]),
        role_started_at=_dt(row["role_started_at"]),
        role_id=row["role_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        next_attempt_at=_dt(row["next_attempt_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


async def _fetchone(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


async def _fetchall(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
    async with conn.execute(sql, params) as cur:
        return list(await cur.fetchall())


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ guilds

    async def get_guild_config(self, guild_id: int) -> GuildConfig | None:
        async with self.db.read() as conn:
            row = await _fetchone(conn, "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        return _guild_config(row) if row else None

    async def list_installed_guilds(self) -> list[GuildConfig]:
        async with self.db.read() as conn:
            rows = await _fetchall(
                conn,
                "SELECT * FROM guild_config WHERE installed_at IS NOT NULL AND board_channel_id IS NOT NULL "
                "ORDER BY guild_id",
            )
        return [_guild_config(r) for r in rows]

    async def setup_guild(self, guild_id: int, channel_id: int, now: datetime) -> GuildConfig:
        """Configure the board channel. ``installed_at`` is set once and then preserved."""
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO guild_config (guild_id, board_channel_id, installed_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET "
                "  board_channel_id = excluded.board_channel_id, "
                "  installed_at = COALESCE(guild_config.installed_at, excluded.installed_at), "
                "  updated_at = excluded.updated_at",
                (guild_id, channel_id, stamp, stamp),
            )
            row = await _fetchone(conn, "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        assert row is not None
        return _guild_config(row)

    async def _ensure_guild_row(self, conn: aiosqlite.Connection, guild_id: int, stamp: str) -> None:
        await conn.execute(
            "INSERT INTO guild_config (guild_id, updated_at) VALUES (?, ?) ON CONFLICT (guild_id) DO NOTHING",
            (guild_id, stamp),
        )

    async def set_reward_role(self, guild_id: int, ptype: PeriodType | str, role_id: int | None, now: datetime) -> None:
        column = _ROLE_COLUMNS[PeriodType(ptype)]  # whitelisted column name
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            await self._ensure_guild_row(conn, guild_id, stamp)
            await conn.execute(
                f"UPDATE guild_config SET {column} = ?, updated_at = ? WHERE guild_id = ?",
                (role_id, stamp, guild_id),
            )

    async def set_banner(self, guild_id: int, url: str | None, now: datetime) -> None:
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            await self._ensure_guild_row(conn, guild_id, stamp)
            await conn.execute(
                "UPDATE guild_config SET banner_url = ?, updated_at = ? WHERE guild_id = ?",
                (url, stamp, guild_id),
            )

    # -------------------------------------------------------------- exclusions

    async def add_exclusion(self, guild_id: int, channel_id: int, now: datetime) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO excluded_channels (guild_id, channel_id, created_at) VALUES (?, ?, ?)",
                (guild_id, channel_id, to_iso(now)),
            )
            return cur.rowcount > 0

    async def remove_exclusion(self, guild_id: int, channel_id: int) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM excluded_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            return cur.rowcount > 0

    async def remove_exclusions(self, guild_id: int, channel_ids: Iterable[int]) -> int:
        """Delete many exclusions in one transaction; returns how many rows existed."""
        params = [(guild_id, channel_id) for channel_id in set(channel_ids)]
        if not params:
            return 0
        async with self.db.transaction() as conn:
            cur = await conn.executemany(
                "DELETE FROM excluded_channels WHERE guild_id = ? AND channel_id = ?",
                params,
            )
            return cur.rowcount

    async def list_exclusions(self, guild_id: int) -> list[int]:
        async with self.db.read() as conn:
            rows = await _fetchall(
                conn,
                "SELECT channel_id FROM excluded_channels WHERE guild_id = ? ORDER BY created_at, channel_id",
                (guild_id,),
            )
        return [r["channel_id"] for r in rows]

    async def load_all_exclusions(self) -> dict[int, set[int]]:
        async with self.db.read() as conn:
            rows = await _fetchall(conn, "SELECT guild_id, channel_id FROM excluded_channels")
        result: dict[int, set[int]] = {}
        for r in rows:
            result.setdefault(r["guild_id"], set()).add(r["channel_id"])
        return result

    # ------------------------------------------------------------------ counts

    async def apply_count_batch(self, counts: Sequence[CountDelta], members: Sequence[MemberUpdate]) -> None:
        """Atomically add count deltas and upsert member metadata in one transaction."""
        if not counts and not members:
            return
        async with self.db.transaction() as conn:
            if counts:
                await conn.executemany(
                    "INSERT INTO daily_counts (guild_id, user_id, day, count, last_message_id) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (guild_id, user_id, day) DO UPDATE SET "
                    "  count = daily_counts.count + excluded.count, "
                    "  last_message_id = MAX(daily_counts.last_message_id, excluded.last_message_id)",
                    [(c.guild_id, c.user_id, c.day, c.count, c.max_message_id) for c in counts],
                )
            if members:
                await conn.executemany(
                    "INSERT INTO members (guild_id, user_id, display_name, username, last_seen_at, left_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                    "  display_name = CASE WHEN excluded.last_seen_at >= members.last_seen_at "
                    "                      THEN excluded.display_name ELSE members.display_name END, "
                    "  username = CASE WHEN excluded.last_seen_at >= members.last_seen_at "
                    "                  THEN excluded.username ELSE members.username END, "
                    "  left_at = CASE WHEN members.left_at IS NOT NULL "
                    "                  AND excluded.last_seen_at > members.left_at "
                    "                 THEN NULL ELSE members.left_at END, "
                    "  last_seen_at = MAX(members.last_seen_at, excluded.last_seen_at)",
                    [
                        (m.guild_id, m.user_id, m.display_name, m.username, to_iso(m.last_seen_at))
                        for m in members
                    ],
                )

    async def aggregate_counts(
        self, guild_id: int, start_day: str | None = None, end_day: str | None = None
    ) -> list[AggregateRow]:
        """Per-user totals and milestone snowflake over ``[start_day, end_day)``, or all history."""
        base = (
            "SELECT dc.user_id AS user_id, SUM(dc.count) AS total, MAX(dc.last_message_id) AS milestone, "
            "       m.display_name AS display_name, m.username AS username, m.left_at AS left_at "
            "FROM daily_counts AS dc "
            "LEFT JOIN members AS m ON m.guild_id = dc.guild_id AND m.user_id = dc.user_id "
            "WHERE dc.guild_id = ? "
        )
        params: list[Any] = [guild_id]
        if start_day is not None and end_day is not None:
            base += "AND dc.day >= ? AND dc.day < ? "
            params += [start_day, end_day]
        elif start_day is not None or end_day is not None:
            raise ValueError("start_day and end_day must be given together")
        base += "GROUP BY dc.user_id HAVING SUM(dc.count) > 0"
        async with self.db.read() as conn:
            rows = await _fetchall(conn, base, params)
        return [
            AggregateRow(
                user_id=r["user_id"],
                total=int(r["total"]),
                milestone_id=int(r["milestone"]),
                display_name=r["display_name"],
                username=r["username"],
                left_at=_dt(r["left_at"]),
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- members

    async def get_member(self, guild_id: int, user_id: int) -> MemberRow | None:
        rows = await self.get_members(guild_id, [user_id])
        return rows.get(user_id)

    async def get_members(self, guild_id: int, user_ids: Iterable[int]) -> dict[int, MemberRow]:
        ids = list(dict.fromkeys(user_ids))
        result: dict[int, MemberRow] = {}
        if not ids:
            return result
        async with self.db.read() as conn:
            # Chunk to stay well under SQLite's bound-parameter limit.
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = await _fetchall(
                    conn,
                    f"SELECT * FROM members WHERE guild_id = ? AND user_id IN ({placeholders})",
                    [guild_id, *chunk],
                )
                for r in rows:
                    result[r["user_id"]] = MemberRow(
                        guild_id=r["guild_id"],
                        user_id=r["user_id"],
                        display_name=r["display_name"],
                        username=r["username"],
                        last_seen_at=from_iso(r["last_seen_at"]),
                        left_at=_dt(r["left_at"]),
                    )
        return result

    async def mark_member_left(self, guild_id: int, user_id: int, at: datetime) -> bool:
        """Record a *confirmed* departure, unless the member was seen after ``at``."""
        stamp = to_iso(at)
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE members SET left_at = ? "
                "WHERE guild_id = ? AND user_id = ? AND left_at IS NULL AND last_seen_at <= ?",
                (stamp, guild_id, user_id, stamp),
            )
            return cur.rowcount > 0

    async def mark_member_present(self, guild_id: int, user_id: int) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE members SET left_at = NULL WHERE guild_id = ? AND user_id = ? AND left_at IS NOT NULL",
                (guild_id, user_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------ boards

    async def get_board_messages(self, guild_id: int) -> dict[str, BoardMessage]:
        async with self.db.read() as conn:
            rows = await _fetchall(conn, "SELECT * FROM board_messages WHERE guild_id = ?", (guild_id,))
        return {
            r["board_type"]: BoardMessage(
                guild_id=r["guild_id"],
                board_type=r["board_type"],
                channel_id=r["channel_id"],
                message_id=r["message_id"],
                generation=r["generation"],
            )
            for r in rows
        }

    async def set_board_message(
        self, guild_id: int, board_type: str, channel_id: int, message_id: int, generation: int
    ) -> None:
        if board_type not in BOARD_ORDER:
            raise ValueError(f"invalid board type {board_type!r}")
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO board_messages (guild_id, board_type, channel_id, message_id, generation) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, board_type) DO UPDATE SET "
                "  channel_id = excluded.channel_id, message_id = excluded.message_id, "
                "  generation = excluded.generation",
                (guild_id, board_type, channel_id, message_id, generation),
            )

    async def get_active_repost(self, guild_id: int) -> RepostOp | None:
        async with self.db.read() as conn:
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND status <> 'DONE'", (guild_id,)
            )
        return _repost(row) if row else None

    async def get_repost(self, guild_id: int, generation: int) -> RepostOp | None:
        async with self.db.read() as conn:
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
        return _repost(row) if row else None

    async def create_repost(self, guild_id: int, channel_id: int, reason: str, now: datetime) -> tuple[RepostOp, bool]:
        """Start a four-board replacement. Returns ``(op, created)``.

        If an operation is already in progress it is returned unchanged
        (``created=False``) so the caller can finish it first. Creating a new
        operation clears ``repost_needed`` in the same transaction.
        """
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND status <> 'DONE'", (guild_id,)
            )
            if row is not None:
                return _repost(row), False
            gen_row = await _fetchone(
                conn,
                "SELECT MAX(g) AS g FROM ("
                "  SELECT MAX(generation) AS g FROM board_repost WHERE guild_id = ? "
                "  UNION ALL SELECT MAX(generation) FROM board_messages WHERE guild_id = ?)",
                (guild_id, guild_id),
            )
            generation = int(gen_row["g"] or 0) + 1 if gen_row else 1
            old_rows = await _fetchall(
                conn, "SELECT board_type, channel_id, message_id FROM board_messages WHERE guild_id = ?", (guild_id,)
            )
            old_ids = {r["board_type"]: [r["channel_id"], r["message_id"]] for r in old_rows}
            await conn.execute(
                "INSERT INTO board_repost (guild_id, generation, channel_id, status, new_ids_json, old_ids_json, "
                "  reason, created_at, updated_at) VALUES (?, ?, ?, 'POSTING', '{}', ?, ?, ?, ?)",
                (guild_id, generation, channel_id, json.dumps(old_ids), reason, stamp, stamp),
            )
            await conn.execute("UPDATE guild_config SET repost_needed = 0 WHERE guild_id = ?", (guild_id,))
            created = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
        assert created is not None
        return _repost(created), True

    async def repost_add_new_id(
        self, guild_id: int, generation: int, board_type: str, message_id: int, now: datetime
    ) -> RepostOp:
        if board_type not in BOARD_ORDER:
            raise ValueError(f"invalid board type {board_type!r}")
        async with self.db.transaction() as conn:
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
            if row is None or row["status"] != RepostStatus.POSTING.value:
                raise StaleRunError(f"repost gen {generation} for guild {guild_id} is not POSTING")
            new_ids = json.loads(row["new_ids_json"])
            new_ids[board_type] = message_id
            await conn.execute(
                "UPDATE board_repost SET new_ids_json = ?, updated_at = ? WHERE guild_id = ? AND generation = ?",
                (json.dumps(new_ids), to_iso(now), guild_id, generation),
            )
            updated = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
        assert updated is not None
        return _repost(updated)

    async def repost_set_status(
        self, guild_id: int, generation: int, expected: RepostStatus, status: RepostStatus, now: datetime
    ) -> RepostOp:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE board_repost SET status = ?, attempts = 0, last_error = NULL, updated_at = ? "
                "WHERE guild_id = ? AND generation = ? AND status = ?",
                (status.value, to_iso(now), guild_id, generation, expected.value),
            )
            if cur.rowcount != 1:
                raise StaleRunError(f"repost gen {generation} for guild {guild_id} is not {expected.value}")
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
        assert row is not None
        return _repost(row)

    async def repost_record_error(self, guild_id: int, generation: int, error: str, now: datetime) -> int:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE board_repost SET attempts = attempts + 1, last_error = ?, updated_at = ? "
                "WHERE guild_id = ? AND generation = ?",
                (error[:1000], to_iso(now), guild_id, generation),
            )
            row = await _fetchone(
                conn,
                "SELECT attempts FROM board_repost WHERE guild_id = ? AND generation = ?",
                (guild_id, generation),
            )
        return int(row["attempts"]) if row else 0

    async def repost_abandon(self, guild_id: int, generation: int, now: datetime) -> None:
        """Close a POSTING op without touching ``board_messages`` (its target channel is obsolete)."""
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE board_repost SET status = 'DONE', last_error = 'abandoned: board channel changed', "
                "  updated_at = ? WHERE guild_id = ? AND generation = ? AND status = 'POSTING'",
                (to_iso(now), guild_id, generation),
            )
            if cur.rowcount != 1:
                raise StaleRunError(f"repost gen {generation} for guild {guild_id} is not POSTING")

    async def repost_complete(self, guild_id: int, generation: int, now: datetime, note: str | None = None) -> None:
        """Switch ``board_messages`` to the new boards and mark the operation DONE, atomically."""
        async with self.db.transaction() as conn:
            row = await _fetchone(
                conn, "SELECT * FROM board_repost WHERE guild_id = ? AND generation = ?", (guild_id, generation)
            )
            if row is None or row["status"] != RepostStatus.DELETING.value:
                raise StaleRunError(f"repost gen {generation} for guild {guild_id} is not DELETING")
            op = _repost(row)
            await conn.execute("DELETE FROM board_messages WHERE guild_id = ?", (guild_id,))
            await conn.executemany(
                "INSERT INTO board_messages (guild_id, board_type, channel_id, message_id, generation) "
                "VALUES (?, ?, ?, ?, ?)",
                [(guild_id, t, op.channel_id, mid, generation) for t, mid in op.new_ids.items()],
            )
            await conn.execute(
                "UPDATE board_repost SET status = 'DONE', last_error = ?, updated_at = ? "
                "WHERE guild_id = ? AND generation = ?",
                (note, to_iso(now), guild_id, generation),
            )

    # ------------------------------------------------------------------ resets

    async def latest_real_run(self, guild_id: int, ptype: PeriodType | str) -> ResetRun | None:
        async with self.db.read() as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM reset_runs WHERE guild_id = ? AND period_type = ? AND is_test = 0 "
                "ORDER BY period_end DESC LIMIT 1",
                (guild_id, PeriodType(ptype).value),
            )
        return _run(row) if row else None

    async def create_runs(self, guild_id: int, periods: Sequence[Period], now: datetime) -> int:
        """Insert PENDING runs with exact persisted boundaries. Existing runs are left untouched."""
        if not periods:
            return 0
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            cur = await conn.executemany(
                "INSERT OR IGNORE INTO reset_runs (guild_id, period_type, period_key, period_start, period_end, "
                "  is_test, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, 'PENDING', ?, ?)",
                [(guild_id, p.type.value, p.key, to_iso(p.start), to_iso(p.end), stamp, stamp) for p in periods],
            )
            return max(cur.rowcount, 0)

    async def create_test_run(
        self, guild_id: int, ptype: PeriodType | str, key: str, start: datetime, end: datetime, now: datetime
    ) -> ResetRun:
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO reset_runs (guild_id, period_type, period_key, period_start, period_end, "
                "  is_test, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 'PENDING', ?, ?)",
                (guild_id, PeriodType(ptype).value, key, to_iso(start), to_iso(end), stamp, stamp),
            )
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (cur.lastrowid,))
        assert row is not None
        return _run(row)

    async def get_run(self, run_id: int) -> ResetRun | None:
        async with self.db.read() as conn:
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (run_id,))
        return _run(row) if row else None

    async def get_run_by_key(self, guild_id: int, ptype: PeriodType | str, key: str) -> ResetRun | None:
        async with self.db.read() as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM reset_runs WHERE guild_id = ? AND period_type = ? AND period_key = ?",
                (guild_id, PeriodType(ptype).value, key),
            )
        return _run(row) if row else None

    async def list_runs(self, guild_id: int, ptype: PeriodType | str | None = None) -> list[ResetRun]:
        sql = "SELECT * FROM reset_runs WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if ptype is not None:
            sql += " AND period_type = ?"
            params.append(PeriodType(ptype).value)
        sql += " ORDER BY period_end, id"
        async with self.db.read() as conn:
            rows = await _fetchall(conn, sql, params)
        return [_run(r) for r in rows]

    async def open_runs(self, guild_id: int) -> list[ResetRun]:
        async with self.db.read() as conn:
            rows = await _fetchall(
                conn,
                "SELECT * FROM reset_runs WHERE guild_id = ? "
                "AND status NOT IN ('DONE', 'SKIPPED_EMPTY', 'ROLES_SUPERSEDED') ORDER BY period_end, id",
                (guild_id,),
            )
        return [_run(r) for r in rows]

    async def later_run_summary(self, run: ResetRun, *, real_only: bool) -> "LaterRuns":
        """Counts of later runs of the same guild/type: total, still PENDING, with a frozen winner."""
        sql = (
            "SELECT COUNT(*) AS total, "
            "  COALESCE(SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END), 0) AS pending, "
            "  COALESCE(SUM(CASE WHEN status IN ('COMPUTED', 'ANNOUNCED', 'ROLES_DONE', 'DONE', "
            "    'ROLES_SUPERSEDED') THEN 1 ELSE 0 END), 0) AS with_winner "
            "FROM reset_runs WHERE guild_id = ? AND period_type = ? AND period_end > ? AND id <> ?"
        )
        if real_only:
            sql += " AND is_test = 0"
        async with self.db.read() as conn:
            row = await _fetchone(conn, sql, (run.guild_id, run.period_type.value, to_iso(run.period_end), run.id))
        assert row is not None
        return LaterRuns(int(row["total"]), int(row["pending"]), int(row["with_winner"]))

    async def later_runs(self, run: ResetRun, *, real_only: bool) -> list[ResetRun]:
        """Runs of the same guild and type whose period ends after ``run``'s."""
        sql = "SELECT * FROM reset_runs WHERE guild_id = ? AND period_type = ? AND period_end > ? AND id <> ?"
        if real_only:
            sql += " AND is_test = 0"
        sql += " ORDER BY period_end, id"
        async with self.db.read() as conn:
            rows = await _fetchall(conn, sql, (run.guild_id, run.period_type.value, to_iso(run.period_end), run.id))
        return [_run(r) for r in rows]

    async def earlier_role_blockers(self, run: ResetRun) -> list[ResetRun]:
        """Earlier runs of the same type whose role work started but has not finished."""
        end = to_iso(run.period_end)
        async with self.db.read() as conn:
            rows = await _fetchall(
                conn,
                "SELECT * FROM reset_runs WHERE guild_id = ? AND period_type = ? "
                "AND status = 'ANNOUNCED' AND role_started_at IS NOT NULL "
                "AND (period_end < ? OR (period_end = ? AND id < ?)) ORDER BY period_end, id",
                (run.guild_id, run.period_type.value, end, end, run.id),
            )
        return [_run(r) for r in rows]

    @staticmethod
    def _run_assignments(fields: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
        sets: list[str] = []
        params: list[Any] = []
        for name, value in fields.items():
            if name not in _RUN_FIELDS:
                raise ValueError(f"field {name!r} cannot be updated")
            if name in _RUN_DATETIME_FIELDS and isinstance(value, datetime):
                value = to_iso(value)
            sets.append(f"{name} = ?")  # name is whitelisted above
            params.append(value)
        return sets, params

    async def update_run(
        self,
        run: ResetRun,
        now: datetime,
        *,
        status: RunStatus | None = None,
        clear_error: bool = False,
        **fields: Any,
    ) -> ResetRun:
        """Update a run if it is still in ``run.status`` (optimistic check)."""
        sets, params = self._run_assignments(fields)
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if clear_error:
            for name in ("attempts", "last_error", "next_attempt_at"):
                if name not in fields:
                    sets.append(f"{name} = {'0' if name == 'attempts' else 'NULL'}")
        sets.append("updated_at = ?")
        params.append(to_iso(now))
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                f"UPDATE reset_runs SET {', '.join(sets)} WHERE id = ? AND status = ?",
                [*params, run.id, run.status.value],
            )
            if cur.rowcount != 1:
                raise StaleRunError(f"{run.describe()} is no longer {run.status.value}")
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (run.id,))
        assert row is not None
        return _run(row)

    async def record_run_error(self, run: ResetRun, error: str, next_attempt_at: datetime, now: datetime) -> ResetRun:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE reset_runs SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ?",
                (error[:1000], to_iso(next_attempt_at), to_iso(now), run.id),
            )
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (run.id,))
        assert row is not None
        return _run(row)

    async def mark_announced(
        self, run: ResetRun, now: datetime, *, channel_id: int | None, message_id: int | None, repost_needed: bool
    ) -> ResetRun:
        """COMPUTED -> ANNOUNCED, flagging a board repost in the same transaction."""
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE reset_runs SET status = 'ANNOUNCED', announcement_channel_id = COALESCE(?, announcement_channel_id), "
                "  announcement_message_id = ?, attempts = 0, last_error = NULL, next_attempt_at = NULL, "
                "  updated_at = ? WHERE id = ? AND status = 'COMPUTED'",
                (channel_id, message_id, stamp, run.id),
            )
            if cur.rowcount != 1:
                raise StaleRunError(f"{run.describe()} is no longer COMPUTED")
            if repost_needed:
                await conn.execute(
                    "UPDATE guild_config SET repost_needed = 1, updated_at = ? WHERE guild_id = ?",
                    (stamp, run.guild_id),
                )
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (run.id,))
        assert row is not None
        return _run(row)

    async def complete_roles(
        self,
        run: ResetRun,
        now: datetime,
        *,
        holder_user_id: int | None,
        role_id: int | None,
        since_key: str | None,
        note: str | None = None,
        update_holder: bool = True,
    ) -> ResetRun:
        """Persist the verified role holder and move ANNOUNCED -> ROLES_DONE atomically."""
        stamp = to_iso(now)
        async with self.db.transaction() as conn:
            if update_holder:
                if holder_user_id is None or role_id is None:
                    await conn.execute(
                        "DELETE FROM role_holders WHERE guild_id = ? AND period_type = ?",
                        (run.guild_id, run.period_type.value),
                    )
                else:
                    await conn.execute(
                        "INSERT INTO role_holders (guild_id, period_type, user_id, role_id, since_key, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (guild_id, period_type) DO UPDATE SET user_id = excluded.user_id, "
                        "  role_id = excluded.role_id, since_key = excluded.since_key, "
                        "  updated_at = excluded.updated_at",
                        (run.guild_id, run.period_type.value, holder_user_id, role_id, since_key, stamp),
                    )
            cur = await conn.execute(
                "UPDATE reset_runs SET status = 'ROLES_DONE', attempts = 0, last_error = ?, next_attempt_at = NULL, "
                "  updated_at = ? WHERE id = ? AND status = 'ANNOUNCED'",
                (note, stamp, run.id),
            )
            if cur.rowcount != 1:
                raise StaleRunError(f"{run.describe()} is no longer ANNOUNCED")
            row = await _fetchone(conn, "SELECT * FROM reset_runs WHERE id = ?", (run.id,))
        assert row is not None
        return _run(row)

    async def get_role_holder(self, guild_id: int, ptype: PeriodType | str) -> RoleHolder | None:
        async with self.db.read() as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM role_holders WHERE guild_id = ? AND period_type = ?",
                (guild_id, PeriodType(ptype).value),
            )
        if row is None:
            return None
        return RoleHolder(
            guild_id=row["guild_id"],
            period_type=PeriodType(row["period_type"]),
            user_id=row["user_id"],
            role_id=row["role_id"],
            since_key=row["since_key"],
        )

    async def previous_result(self, guild_id: int, ptype: PeriodType | str) -> ResetRun | None:
        """Most recent real run of ``ptype`` whose outcome is decided (winner frozen or empty)."""
        async with self.db.read() as conn:
            row = await _fetchone(
                conn,
                "SELECT * FROM reset_runs WHERE guild_id = ? AND period_type = ? AND is_test = 0 "
                "AND status IN ('COMPUTED', 'ANNOUNCED', 'ROLES_DONE', 'DONE', 'ROLES_SUPERSEDED', 'SKIPPED_EMPTY') "
                "ORDER BY period_end DESC LIMIT 1",
                (guild_id, PeriodType(ptype).value),
            )
        return _run(row) if row else None
