"""Leaderboard aggregation, tie-breaking and rank calculation.

Ordering rule (used by boards, pagination, ``/stats`` and reset winners alike;
users banned from the leaderboard are left out of all of them, see
``Repo.aggregate_counts``):

1. Higher message total first.
2. Equal totals: the user who *reached that final total first* ranks higher.
3. Identical milestone timestamps: ascending Discord user ID.

Why the milestone is ``MAX(last_message_id)`` over the period's buckets
------------------------------------------------------------------------
Each counted message adds exactly one point and nothing ever subtracts (deleted
messages stay counted, edits never count). So a user whose period total is
``T`` reached ``T`` with their ``T``-th counted message in creation order — which
is necessarily their *latest counted message within the period*. Every period
boundary is a UTC midnight and counts are bucketed by ``message.created_at``, so
the daily buckets inside ``[start, end)`` contain exactly the period's counted
messages, and the maximum ``last_message_id`` over them is that milestone
message. Messages outside the period (e.g. after a historical period ended),
uncounted messages (excluded channels, bots) and the global ``last_seen_at`` are
never consulted.

Snowflakes encode a millisecond timestamp in their upper bits; the tie-break
compares ``snowflake >> 22`` (the timestamp) so that two milestones in the same
millisecond fall through to the user-ID rule exactly as specified, instead of
being decided by Discord's internal worker/sequence bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from bot.db.repo import AggregateRow, Repo
from bot.periods import board_range, day_range

TOP_LIMIT = 100
DISCORD_EPOCH_MS = 1420070400000


@dataclass(frozen=True)
class RankEntry:
    rank: int
    user_id: int
    count: int
    milestone_id: int
    display_name: str | None
    username: str | None
    left: bool

    @property
    def name(self) -> str:
        """Plain-text name (last known), never dependent on mention resolution."""
        return self.display_name or self.username or f"User {self.user_id}"


def milestone_ms(snowflake: int) -> int:
    """Milliseconds since the Discord epoch encoded in a snowflake."""
    return snowflake >> 22


def sort_key(count: int, milestone_id: int, user_id: int) -> tuple[int, int, int]:
    return (-count, milestone_ms(milestone_id), user_id)


def rank_rows(rows: Iterable[AggregateRow], limit: int | None = None) -> list[RankEntry]:
    """Order aggregate rows by the canonical rule and assign 1-based ranks."""
    ordered = sorted(
        (r for r in rows if r.total > 0),
        key=lambda r: sort_key(r.total, r.milestone_id, r.user_id),
    )
    if limit is not None:
        ordered = ordered[:limit]
    return [
        RankEntry(
            rank=i,
            user_id=r.user_id,
            count=r.total,
            milestone_id=r.milestone_id,
            display_name=r.display_name,
            username=r.username,
            left=r.left_at is not None,
        )
        for i, r in enumerate(ordered, start=1)
    ]


@dataclass(frozen=True)
class Standing:
    rank: int | None
    count: int


class Ranker:
    """Computes rankings from the persisted daily buckets (the source of truth)."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    async def ranking(
        self,
        guild_id: int,
        start: datetime | None,
        end: datetime | None,
        *,
        limit: int | None = TOP_LIMIT,
    ) -> list[RankEntry]:
        """Ranking over ``[start, end)``; ``None`` bounds mean all persisted history."""
        if start is None and end is None:
            rows = await self._repo.aggregate_counts(guild_id)
        elif start is not None and end is not None:
            start_day, end_day = day_range(start, end)
            rows = await self._repo.aggregate_counts(guild_id, start_day, end_day)
        else:
            raise ValueError("start and end must both be given or both be None")
        return rank_rows(rows, limit)

    async def board_ranking(
        self, guild_id: int, board_type: str, now: datetime, *, limit: int | None = TOP_LIMIT
    ) -> list[RankEntry]:
        start, end = board_range(board_type, now)
        return await self.ranking(guild_id, start, end, limit=limit)

    async def standing(self, guild_id: int, board_type: str, now: datetime, user_id: int) -> Standing:
        """A user's rank and count on a board, computed from the very same ordering."""
        entries = await self.board_ranking(guild_id, board_type, now, limit=None)
        for entry in entries:
            if entry.user_id == user_id:
                return Standing(entry.rank, entry.count)
        return Standing(None, 0)
