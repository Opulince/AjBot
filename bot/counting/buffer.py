"""In-memory count accumulation with crash-tolerant, retrying flushes.

Counts are accumulated per ``(guild_id, user_id, day)`` and written to SQLite in
one transaction roughly every ``FLUSH_INTERVAL_SECONDS``. A failed flush merges
its snapshot back into the live buffer, so database failures never silently
drop counts. A hard kill still loses whatever is buffered at that moment
(normally at most one flush interval); that limitation is documented.

Concurrency model: everything that touches the live buffer is synchronous (no
``await``), and asyncio runs one coroutine at a time, so record / detach /
merge-back are atomic with respect to each other. Message handlers keep
accumulating into the fresh live buffer while a detached snapshot is being
written.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from bot.clock import ensure_utc
from bot.db.repo import CountDelta, MemberUpdate, Repo
from bot.periods import utc_day_key

log = logging.getLogger(__name__)

CountKey = tuple[int, int, str]
MemberKey = tuple[int, int]


@dataclass
class _Delta:
    count: int
    max_message_id: int


@dataclass
class _Meta:
    display_name: str
    username: str
    last_seen: datetime


class CountBuffer:
    def __init__(
        self,
        repo: Repo,
        *,
        flush_interval: float = 5.0,
        max_backoff: float = 60.0,
        dedupe_capacity: int = 10_000,
    ) -> None:
        self._repo = repo
        self._interval = flush_interval
        self._max_backoff = max(max_backoff, flush_interval)
        self._capacity = dedupe_capacity
        self._live: dict[CountKey, _Delta] = {}
        self._members: dict[MemberKey, _Meta] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._flush_lock = asyncio.Lock()
        self.consecutive_failures = 0

    # --------------------------------------------------------------- record

    def record(
        self,
        *,
        guild_id: int,
        user_id: int,
        message_id: int,
        created_at: datetime,
        display_name: str,
        username: str,
    ) -> bool:
        """Count one message. Returns ``False`` for a duplicate delivery.

        Must stay free of ``await``: atomicity of the duplicate check relies on it.
        """
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return False
        self._seen[message_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

        created_at = ensure_utc(created_at)
        key = (guild_id, user_id, utc_day_key(created_at))
        delta = self._live.get(key)
        if delta is None:
            self._live[key] = _Delta(1, message_id)
        else:
            delta.count += 1
            if message_id > delta.max_message_id:
                delta.max_message_id = message_id

        mkey = (guild_id, user_id)
        meta = self._members.get(mkey)
        if meta is None or created_at >= meta.last_seen:
            self._members[mkey] = _Meta(display_name, username, created_at)
        return True

    @property
    def pending_messages(self) -> int:
        return sum(d.count for d in self._live.values())

    @property
    def pending_keys(self) -> int:
        return len(self._live)

    def snapshot_pending(self) -> dict[CountKey, tuple[int, int]]:
        """Copy of the live buffer, for diagnostics and tests."""
        return {k: (d.count, d.max_message_id) for k, d in self._live.items()}

    # ---------------------------------------------------------------- flush

    async def flush(self) -> bool:
        """Persist everything buffered so far. Returns ``True`` on success.

        Concurrent callers are serialised, so when this returns ``True`` every
        message recorded before the call is durably stored. The write runs in a
        shielded task: cancelling the caller cannot interrupt a transaction
        halfway and lose track of whether the snapshot was committed.
        """
        return await asyncio.shield(self._locked_flush())

    async def _locked_flush(self) -> bool:
        async with self._flush_lock:
            return await self._flush_once()

    async def _flush_once(self) -> bool:
        if not self._live and not self._members:
            return True
        counts, members = self._live, self._members
        self._live, self._members = {}, {}
        count_rows = [
            CountDelta(g, u, day, d.count, d.max_message_id) for (g, u, day), d in counts.items()
        ]
        member_rows = [
            MemberUpdate(g, u, m.display_name, m.username, m.last_seen) for (g, u), m in members.items()
        ]
        try:
            await self._repo.apply_count_batch(count_rows, member_rows)
        except Exception:
            self._merge_back(counts, members)
            self.consecutive_failures += 1
            log.exception(
                "Count flush failed (attempt %d); %d buckets / %d messages kept in memory for retry",
                self.consecutive_failures,
                len(counts),
                sum(d.count for d in counts.values()),
            )
            return False
        if self.consecutive_failures:
            log.info("Count flush recovered after %d failed attempts", self.consecutive_failures)
        self.consecutive_failures = 0
        log.debug(
            "Flushed %d messages in %d buckets (%d member updates)",
            sum(d.count for d in counts.values()),
            len(counts),
            len(members),
        )
        return True

    def _merge_back(self, counts: dict[CountKey, _Delta], members: dict[MemberKey, _Meta]) -> None:
        """Return a failed snapshot to the live buffer without losing concurrent increments."""
        for key, failed in counts.items():
            live = self._live.get(key)
            if live is None:
                self._live[key] = failed
            else:
                live.count += failed.count
                live.max_message_id = max(live.max_message_id, failed.max_message_id)
        for member_key, meta in members.items():
            live_meta = self._members.get(member_key)
            if live_meta is None or meta.last_seen > live_meta.last_seen:
                self._members[member_key] = meta

    def next_delay(self) -> float:
        """Delay before the next periodic flush: the interval, or bounded backoff after failures."""
        if self.consecutive_failures == 0:
            return self._interval
        exponent = min(self.consecutive_failures, 16)  # avoid float overflow on long outages
        delay = min(self._max_backoff, self._interval * (2 ** exponent))
        return delay * random.uniform(0.85, 1.15)

    async def run(self, stop: asyncio.Event) -> None:
        """Periodic flush loop. Exits when ``stop`` is set (the final flush is the caller's job)."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.next_delay())
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                await self.flush()
            except Exception:  # pragma: no cover - flush already handles DB errors
                log.exception("Unexpected error in the periodic flush; continuing")
