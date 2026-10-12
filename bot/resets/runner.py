"""Reset scheduling, missed-period enumeration and chronological catch-up.

On startup and every tick, for each installed guild (under that guild's lock,
which board updates share):

1. Create PENDING runs for every period boundary passed since the last recorded
   run (or since installation), with exact boundaries persisted. Existing runs
   are never recreated — the unique key prevents duplicates.
2. Compute pass: PENDING runs that are due are computed in chronological order.
   When a real run gets its result, the boards are edited in place right away so
   the "Previous Member of …" field shows it without waiting for the next
   periodic refresh. Nothing is posted publicly.
3. Advance pass, chronological:

   * the announcement step always resolves silently (see
     :meth:`bot.resets.machine.ResetMachine.announce`);
   * while a run is ANNOUNCED, its winner gets a congratulation DM if the
     server enabled ``/winnerdm`` and the run is real (not a forced test) and
     the most recent ended period of its type, so catch-up after downtime
     never DMs historical winners. Checked before any role backoff, so a
     failing role step doesn't delay it;
   * a run's roles step waits while an earlier run of the same type has
     unfinished role work, so role changes happen in period order;
   * a run that never started role work is ``ROLES_SUPERSEDED`` when a later run
     of the same type has a winner (empty later periods do not change roles, so
     the role ends with the latest *eligible* winner); runs that did start role
     work always finish it;
   * forced test runs never supersede real runs.

4. Board repost: only finishes a repost that is already in progress, or one a
   legacy ``repost_needed`` flag still requests. Resets never request one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from bot.boards import BoardService
from bot.clock import Clock
from bot.db.repo import GuildConfig, Repo, ResetRun, RunStatus, StaleRunError
from bot.periods import RESET_TYPES, PeriodType, ended_periods, period_containing
from bot.resets.machine import ResetMachine

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0
COMPUTE_BATCH = 50
MAX_STEPS_PER_RUN = 6


class GuildLocks:
    """One lock per guild, shared by the reset runner, board updates and /setup."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock


class ResetRunner:
    def __init__(
        self,
        repo: Repo,
        machine: ResetMachine,
        boards: BoardService,
        clock: Clock,
        locks: GuildLocks,
    ) -> None:
        self._repo = repo
        self._machine = machine
        self._boards = boards
        self._clock = clock
        self._locks = locks

    # -------------------------------------------------------------- driving

    async def run(self, stop: asyncio.Event, interval: float = TICK_SECONDS) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        try:
            guilds = await self._repo.list_installed_guilds()
        except Exception:
            log.exception("Reset scheduler could not list guilds")
            return
        for cfg in guilds:
            try:
                await self.process_guild(cfg.guild_id)
            except Exception:
                log.exception("Reset processing failed for guild %s", cfg.guild_id)

    async def process_guild(self, guild_id: int) -> None:
        async with self._locks.get(guild_id):
            await self._process_locked(guild_id)

    async def ensure_runs(self, guild_id: int) -> None:
        """Create any missing runs for passed boundaries without advancing them."""
        async with self._locks.get(guild_id):
            cfg = await self._repo.get_guild_config(guild_id)
            if cfg is not None and cfg.installed:
                await self._enumerate(cfg)

    async def force_reset(self, guild_id: int, ptype: PeriodType | str) -> ResetRun:
        """Run the real state machine on a synthetic test period ``[period start, now)``.

        The test key (``test-<current key>-<timestamp>``) can never collide with a
        scheduled run's key, and ``is_test`` keeps the run out of catch-up
        enumeration and away from superseding real runs.
        """
        ptype = PeriodType(ptype)
        async with self._locks.get(guild_id):
            cfg = await self._repo.get_guild_config(guild_id)
            if cfg is None or not cfg.installed:
                raise RuntimeError("The leaderboard is not set up in this server; run /setup first")
            now = self._clock.now()
            current = period_containing(ptype, now)
            key = f"test-{current.key}-{now:%Y%m%dT%H%M%S%f}"
            run = await self._repo.create_test_run(guild_id, ptype, key, current.start, now, now)
            log.info("Forced test reset created: %s range=[%s, %s)", run.describe(), current.start, now)
            await self._process_locked(guild_id)
            refreshed = await self._repo.get_run(run.id)
            assert refreshed is not None
            return refreshed

    # ----------------------------------------------------------- processing

    async def _process_locked(self, guild_id: int) -> None:
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is None or not cfg.installed:
            return
        await self._enumerate(cfg)

        computed = 0
        decided = False
        for run in await self._repo.open_runs(guild_id):
            now = self._clock.now()
            if run.status is not RunStatus.PENDING or self._machine.backing_off(run, now):
                continue
            if now < self._machine.due_at(run):
                continue
            if computed >= COMPUTE_BATCH:
                log.info("Guild %s: compute batch limit reached; continuing next tick", guild_id)
                break
            result = await self._step(self._machine.compute, run)
            computed += 1
            if not run.is_test and result.status is not RunStatus.PENDING:
                decided = True

        for run in await self._repo.open_runs(guild_id):
            await self._advance(run)

        if decided:
            try:
                await self._boards.update_guild(guild_id)
            except Exception:
                log.exception("Board refresh after a reset failed for guild %s", guild_id)

        try:
            await self._boards.reconcile(guild_id)
        except Exception:
            log.exception("Board repost reconciliation failed for guild %s", guild_id)

    async def _enumerate(self, cfg: GuildConfig) -> None:
        assert cfg.installed_at is not None
        now = self._clock.now()
        for ptype in RESET_TYPES:
            last = await self._repo.latest_real_run(cfg.guild_id, ptype)
            periods = ended_periods(ptype, cfg.installed_at, now, after=last.period_end if last else None)
            if not periods:
                continue
            created = await self._repo.create_runs(cfg.guild_id, periods, now)
            if created > 1:
                log.info(
                    "Missed-reset catch-up: guild=%s type=%s created %d runs (%s .. %s)",
                    cfg.guild_id,
                    ptype.value,
                    created,
                    periods[0].key,
                    periods[-1].key,
                )
            elif created == 1:
                log.info("Reset run created: guild=%s %s:%s", cfg.guild_id, ptype.value, periods[0].key)

    async def _step(self, fn: Callable[[ResetRun], Awaitable[ResetRun]], run: ResetRun) -> ResetRun:
        try:
            return await fn(run)
        except StaleRunError as exc:
            log.warning("Reset %s changed concurrently (%s); re-reading next tick", run.describe(), exc)
        except Exception as exc:
            log.exception("Unexpected error processing reset %s in state %s", run.describe(), run.status.value)
            try:
                fresh = await self._repo.get_run(run.id)
                if fresh is not None:
                    return await self._machine.fail(fresh, "unexpected", f"{exc.__class__.__name__}: {exc}")
            except Exception:
                log.exception("Could not record failure for reset %s", run.describe())
        fresh = await self._repo.get_run(run.id)
        return fresh if fresh is not None else run

    async def _wants_winner_dm(self, run: ResetRun) -> bool:
        if run.is_test or run.winner_user_id is None or run.winner_dm_sent_at is not None:
            return False
        current = period_containing(run.period_type, self._clock.now())
        if run.period_end != current.start:
            return False  # a historical period (catch-up): no DM
        cfg = await self._repo.get_guild_config(run.guild_id)
        return cfg is not None and cfg.winner_dm

    async def _role_decision(self, run: ResetRun) -> str:
        """``apply``, ``supersede`` or ``wait``."""
        if run.role_started_at is None:
            later = await self._repo.later_run_summary(run, real_only=not run.is_test)
            if later.pending:
                return "wait"  # cannot know yet whether a later period has a winner
            if later.with_winner:
                return "supersede"
        if await self._repo.earlier_role_blockers(run):
            return "wait"
        return "apply"

    async def _advance(self, run: ResetRun) -> None:
        for _ in range(MAX_STEPS_PER_RUN):
            if run.terminal or run.status is RunStatus.PENDING:
                return
            if run.status is RunStatus.ANNOUNCED and await self._wants_winner_dm(run):
                run = await self._step(self._machine.send_winner_dm, run)
            if self._machine.backing_off(run, self._clock.now()):
                return
            before = run.status
            if run.status is RunStatus.COMPUTED:
                run = await self._step(self._machine.announce, run)
            elif run.status is RunStatus.ANNOUNCED:
                decision = await self._role_decision(run)
                if decision == "wait":
                    return
                if decision == "supersede":
                    run = await self._step(self._machine.supersede_roles, run)
                else:
                    run = await self._step(self._machine.apply_roles, run)
            elif run.status is RunStatus.ROLES_DONE:
                run = await self._step(self._machine.finish, run)
            if run.status is before:
                return  # failed or waiting; retried on a later tick
