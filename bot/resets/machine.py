"""The per-run reset state machine.

States and transitions (every transition is persisted before moving on)::

    PENDING ──compute──▶ COMPUTED ──announce──▶ ANNOUNCED ──roles──▶ ROLES_DONE ──▶ DONE
       │                                            │
       └──▶ SKIPPED_EMPTY (nobody eligible)         └──▶ ROLES_SUPERSEDED (a later run owns the role)

* PENDING: waits until ``period_end + RESET_GRACE_SECONDS``, then forces a
  buffer flush; a failed flush leaves the run retryable, so a winner is never
  computed from counts that only exist in memory.
* COMPUTED: the ranking over the run's *persisted* range is walked until a
  member confirmed to still be in the guild is found. The winner is frozen.
  Unknown membership (network errors, rate limits, Forbidden) aborts and
  retries — it never disqualifies anyone.
* ANNOUNCED: the announcement step is resolved (always silent). Winners are
  shown in the boards' "Previous Member of …" field instead of a public post,
  so nothing is sent and ``announcement_message_id`` stays NULL. The state
  name is kept because it is part of the ``reset_runs`` CHECK constraint.
  Legacy: a run that an older version left mid-announce (``announce_started_at``
  set) gets its channel scanned once for the marker ``reset:{guild}:{type}:{key}``
  so an already-posted announcement is adopted rather than orphaned; a scan
  that fails is logged and the step still resolves silently.
  While a run is ANNOUNCED, the runner may also send the winner a
  congratulation DM (``/winnerdm``); ``winner_dm_sent_at`` is recorded after
  the attempt, so it is never retried (a crash in between can send it twice).
* ROLES_DONE: the reward role is moved to the winner and verified against the
  actual Discord member roles before ``role_holders`` is updated (atomically
  with the status change). Every step is idempotent, so a crash between any two
  Discord calls is repaired by simply running the step again.
* DONE / SKIPPED_EMPTY / ROLES_SUPERSEDED are terminal.

Which runs may move roles is decided by the runner (see
:mod:`bot.resets.runner`); this module executes single transitions.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import discord

from bot.clock import Clock
from bot.counting.buffer import CountBuffer
from bot.db.repo import Repo, ResetRun, RunStatus
from bot.discord_ops import (
    DiscordOps,
    OpError,
    OpNotFound,
    Presence,
    PresenceResult,
    check_presence,
    with_retries,
)
from bot.ranking import Ranker
from bot.ui.embeds import PERIOD_NOUNS, period_label, plural, reset_marker

log = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 1800.0
ANNOUNCE_SCAN_MARGIN = timedelta(minutes=15)
ANNOUNCE_SCAN_LIMIT = 1000  # paged by discord.py, 100 messages per request
PRESENCE_CACHE_TTL = timedelta(seconds=60)
TEST_RUN_MAX_DELAY = timedelta(minutes=2)


def retry_delay(attempts: int) -> float:
    """Bounded exponential backoff with jitter for a run that failed ``attempts`` times."""
    exponent = min(max(attempts - 1, 0), 16)  # avoid float overflow for long-stuck runs
    base = min(RETRY_BASE_SECONDS * (2 ** exponent), RETRY_MAX_SECONDS)
    return base * random.uniform(0.9, 1.1)


class ResetMachine:
    def __init__(
        self,
        repo: Repo,
        ops: DiscordOps,
        buffer: CountBuffer,
        ranker: Ranker,
        clock: Clock,
        *,
        grace_seconds: int,
    ) -> None:
        self._repo = repo
        self._ops = ops
        self._buffer = buffer
        self._ranker = ranker
        self._clock = clock
        self._grace = timedelta(seconds=grace_seconds)
        self._presence_cache: dict[tuple[int, int], tuple[datetime, PresenceResult]] = {}

    # ------------------------------------------------------------- helpers

    def due_at(self, run: ResetRun) -> datetime:
        """Forced test runs are due immediately; real runs after the grace period."""
        return run.period_end if run.is_test else run.period_end + self._grace

    def backing_off(self, run: ResetRun, now: datetime) -> bool:
        return run.next_attempt_at is not None and now < run.next_attempt_at

    async def fail(self, run: ResetRun, step: str, error: OpError | str) -> ResetRun:
        """Record a failed attempt with bounded backoff; never advances the state."""
        message = error.describe() if isinstance(error, OpError) else str(error)
        now = self._clock.now()
        delay = retry_delay(run.attempts + 1)
        updated = await self._repo.record_run_error(run, f"{step}: {message}", now + timedelta(seconds=delay), now)
        log.warning(
            "Reset %s state=%s step=%s attempt=%d failed: %s (retry in %.0fs)",
            run.describe(),
            run.status.value,
            step,
            updated.attempts,
            message,
            delay,
        )
        return updated

    async def _transition(self, run: ResetRun, status: RunStatus, **fields: object) -> ResetRun:
        updated = await self._repo.update_run(run, self._clock.now(), status=status, clear_error=True, **fields)
        log.info("Reset %s transition %s -> %s", run.describe(), run.status.value, status.value)
        return updated

    async def _presence(self, guild_id: int, user_id: int) -> PresenceResult:
        now = self._clock.now()
        cached = self._presence_cache.get((guild_id, user_id))
        if cached is not None and now - cached[0] < PRESENCE_CACHE_TTL:
            return cached[1]
        result = await check_presence(self._ops, guild_id, user_id)
        if result.presence is not Presence.UNKNOWN:
            self._presence_cache[(guild_id, user_id)] = (now, result)
            if len(self._presence_cache) > 10_000:
                self._presence_cache.clear()
        return result

    # ------------------------------------------------------------- compute

    async def compute(self, run: ResetRun) -> ResetRun:
        if run.status is not RunStatus.PENDING:
            return run
        now = self._clock.now()
        if now < self.due_at(run):
            return run
        if run.is_test and now > run.period_end + max(self._grace, TEST_RUN_MAX_DELAY):
            # Day buckets cannot be split at period_end, so a late computation
            # would include messages sent after the test period ended.
            note = "stale forced test reset: could not be computed promptly, so no winner was chosen"
            log.warning("Reset %s: %s", run.describe(), note)
            return await self._transition(run, RunStatus.SKIPPED_EMPTY, last_error=note)
        if not await self._buffer.flush():
            return await self.fail(run, "flush", "buffer flush failed; winner not computed from unpersisted counts")
        entries = await self._ranker.ranking(run.guild_id, run.period_start, run.period_end, limit=None)
        for entry in entries:
            result = await self._presence(run.guild_id, entry.user_id)
            if result.presence is Presence.PRESENT:
                log.info(
                    "Reset %s winner computed: user=%s count=%d (rank %d)",
                    run.describe(),
                    entry.user_id,
                    entry.count,
                    entry.rank,
                )
                return await self._transition(
                    run, RunStatus.COMPUTED, winner_user_id=entry.user_id, winner_count=entry.count
                )
            if result.presence is Presence.ABSENT:
                if await self._repo.mark_member_left(run.guild_id, entry.user_id, self._clock.now()):
                    log.info("Member %s confirmed to have left guild %s", entry.user_id, run.guild_id)
                log.info("Reset %s skipping departed user %s (rank %d)", run.describe(), entry.user_id, entry.rank)
                continue
            assert result.error is not None
            return await self.fail(run, "compute", f"membership of user {entry.user_id} unknown: {result.error.describe()}")
        log.info("Reset %s has no eligible members", run.describe())
        return await self._transition(run, RunStatus.SKIPPED_EMPTY)

    # ------------------------------------------------------------ announce

    async def _find_announcement(self, run: ResetRun, channel_id: int) -> int | None:
        assert run.announce_started_at is not None
        marker = reset_marker(run.guild_id, run.period_type.value, run.period_key)
        history = await self._ops.recent_history(
            channel_id, after=run.announce_started_at - ANNOUNCE_SCAN_MARGIN, limit=ANNOUNCE_SCAN_LIMIT
        )
        bot_id = self._ops.bot_user_id
        for msg in history:
            if msg.author_id == bot_id and msg.contains_marker(marker):
                return msg.id
        return None

    async def announce(self, run: ResetRun) -> ResetRun:
        """Resolve the announcement step without posting anything (COMPUTED -> ANNOUNCED)."""
        if run.status is not RunStatus.COMPUTED:
            return run
        found: int | None = None
        if run.announce_started_at is not None and run.announcement_channel_id is not None:
            # Legacy: an older version may have posted before crashing. Adopt it so
            # the run records the message; never post a new one.
            try:
                found = await self._find_announcement(run, run.announcement_channel_id)
            except OpError as exc:
                log.warning(
                    "Reset %s cannot scan channel %s for an earlier announcement (%s); resolving silently",
                    run.describe(),
                    run.announcement_channel_id,
                    exc.describe(),
                )
        updated = await self._repo.mark_announced(run, self._clock.now(), message_id=found)
        if found is not None:
            log.info("Reset %s adopted existing announcement message %s", run.describe(), found)
        log.info("Reset %s transition COMPUTED -> ANNOUNCED (silent)", run.describe())
        return updated

    # ----------------------------------------------------------- winner DM

    async def send_winner_dm(self, run: ResetRun) -> ResetRun:
        """Best-effort congratulation DM to the winner, attempted at most once per run.

        Closed DMs and Discord errors are logged and never fail or retry the
        run: the attempt is recorded either way.
        """
        if run.status is not RunStatus.ANNOUNCED or run.winner_dm_sent_at is not None or run.winner_user_id is None:
            return run
        winner = run.winner_user_id
        noun = PERIOD_NOUNS[run.period_type.value].title()
        server = self._ops.guild_name(run.guild_id)
        where = f" in **{discord.utils.escape_markdown(server)}**" if server else ""
        text = (
            f"Congratulations! You're the **Member of the {noun}**{where} for "
            f"{period_label(run.period_type.value, run.period_key, run.is_test)}, with {plural(run.winner_count or 0)}."
        )
        try:
            delivered = await with_retries(lambda: self._ops.dm_user(winner, content=text), name="winner_dm")
        except OpError as exc:
            log.warning("Reset %s: winner DM to %s failed: %s", run.describe(), winner, exc.describe())
        else:
            if delivered:
                log.info("Reset %s: winner %s was sent a DM", run.describe(), winner)
            else:
                log.info("Reset %s: winner %s doesn't accept DMs; skipped", run.describe(), winner)
        now = self._clock.now()
        return await self._repo.update_run(run, now, winner_dm_sent_at=now)

    # --------------------------------------------------------------- roles

    async def apply_roles(self, run: ResetRun) -> ResetRun:
        if run.status is not RunStatus.ANNOUNCED:
            return run
        now = self._clock.now()
        guild_id = run.guild_id
        cfg = await self._repo.get_guild_config(guild_id)
        configured = cfg.role_for(run.period_type) if cfg else None
        # Always follow the configured role: if an admin switched it with /setrole
        # while this run's role work was in flight (e.g. because the old one was
        # unmanageable), retrying the stale role forever would block every later run.
        role_id = configured if configured is not None else run.role_id
        if run.role_started_at is not None and run.role_id is not None and role_id != run.role_id:
            log.info("Reset %s reward role changed from %s to %s mid-transition", run.describe(), run.role_id, role_id)
            run = await self._repo.update_run(run, now, role_id=role_id)

        if role_id is None:
            updated = await self._repo.complete_roles(
                run, now, holder_user_id=None, role_id=None, since_key=None,
                note="no reward role configured", update_holder=False,
            )
            log.info("Reset %s transition ANNOUNCED -> ROLES_DONE (no reward role configured)", run.describe())
            return updated

        check = self._ops.check_role_manageable(guild_id, role_id)
        if not check.ok and check.missing_role:
            updated = await self._repo.complete_roles(
                run, now, holder_user_id=None, role_id=None, since_key=None,
                note=f"reward role {role_id} no longer exists; role not assigned", update_holder=False,
            )
            log.warning("Reset %s: reward role %s no longer exists; role step skipped", run.describe(), role_id)
            return updated
        if not check.ok:
            return await self.fail(run, "roles-precheck", f"forbidden: {check.reason}")

        if run.role_started_at is None:
            run = await self._repo.update_run(run, now, role_started_at=now, role_id=role_id)

        winner = run.winner_user_id
        assert winner is not None
        holder = await self._repo.get_role_holder(guild_id, run.period_type)
        previous = holder.user_id if holder and holder.user_id != winner and holder.role_id == role_id else None
        reason = f"Leaderboard {run.period_type.value} reward ({run.period_key})"
        if run.is_test:
            reason += " [forced test reset]"

        try:
            winner_present = True
            try:
                info = await with_retries(lambda: self._ops.fetch_member(guild_id, winner), name="fetch_member")
            except OpNotFound:
                winner_present = False
            if winner_present and role_id not in info.role_ids:
                await with_retries(
                    lambda: self._ops.add_role(guild_id, winner, role_id, reason=reason), name="add_role"
                )
                log.info("Reset %s added role %s to winner %s", run.describe(), role_id, winner)

            if previous is not None:
                try:
                    prev_info = await with_retries(
                        lambda: self._ops.fetch_member(guild_id, previous), name="fetch_member"
                    )
                except OpNotFound:
                    prev_info = None
                    log.info("Reset %s previous holder %s left the guild; nothing to remove", run.describe(), previous)
                if prev_info is not None and role_id in prev_info.role_ids:
                    await with_retries(
                        lambda: self._ops.remove_role(guild_id, previous, role_id, reason=reason), name="remove_role"
                    )
                    log.info("Reset %s removed role %s from previous holder %s", run.describe(), role_id, previous)

            # Verify against actual Discord state before recording anything.
            if winner_present:
                try:
                    check_info = await with_retries(
                        lambda: self._ops.fetch_member(guild_id, winner), name="fetch_member"
                    )
                except OpNotFound:
                    winner_present = False
                else:
                    if role_id not in check_info.role_ids:
                        return await self.fail(run, "roles-verify", "winner does not have the reward role yet")
            if previous is not None:
                try:
                    prev_check = await with_retries(
                        lambda: self._ops.fetch_member(guild_id, previous), name="fetch_member"
                    )
                except OpNotFound:
                    prev_check = None
                if prev_check is not None and role_id in prev_check.role_ids:
                    return await self.fail(run, "roles-verify", "previous holder still has the reward role")
        except OpError as exc:
            return await self.fail(run, "roles", exc)

        if winner_present:
            since = holder.since_key if holder and holder.user_id == winner and holder.role_id == role_id else run.period_key
            updated = await self._repo.complete_roles(
                run, self._clock.now(), holder_user_id=winner, role_id=role_id, since_key=since
            )
        else:
            log.warning("Reset %s: winner %s left before the role could be assigned", run.describe(), winner)
            updated = await self._repo.complete_roles(
                run, self._clock.now(), holder_user_id=None, role_id=role_id, since_key=None,
                note="winner left the server before the role could be assigned",
            )
        log.info("Reset %s transition ANNOUNCED -> ROLES_DONE (holder=%s)", run.describe(), winner if winner_present else None)
        return updated

    async def supersede_roles(self, run: ResetRun) -> ResetRun:
        if run.status is not RunStatus.ANNOUNCED:
            return run
        return await self._transition(run, RunStatus.ROLES_SUPERSEDED)

    async def finish(self, run: ResetRun) -> ResetRun:
        if run.status is not RunStatus.ROLES_DONE:
            return run
        # Keep last_error: at this point it can only hold the role step's note
        # (e.g. "winner left the server"), which stays useful for operators.
        updated = await self._repo.update_run(run, self._clock.now(), status=RunStatus.DONE)
        log.info("Reset %s transition ROLES_DONE -> DONE", run.describe())
        return updated
