"""Tempban expiry: lifts bans whose scheduled unban time has passed.

Scheduled unbans live in ``scheduled_unbans`` (one row per guild and user),
so they survive restarts: the scheduler tick picks up everything that became
due while the bot was offline. For each due row, under the guild's moderation
lock (which ``/ban`` and ``/unban`` share, so a fresh ban is never undone by a
stale schedule):

1. the row is re-read; if it was removed or replaced meanwhile, nothing happens;
2. the ban is lifted on Discord. "Unknown Ban" counts as success (someone
   already lifted it by hand); any other error records an attempt and backs
   off like a failed reset step, so it is retried later;
3. the row is deleted and the ``unban`` case recorded in one transaction, then
   the case goes to the mod log.

A crash between steps 2 and 3 just repeats step 2 on the next tick, which then
sees "Unknown Ban" and finishes.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from bot.db.repo import ScheduledUnban
from bot.discord_ops import OpError, OpNotFound
from bot.moderation import log_case
from bot.resets.machine import retry_delay

if TYPE_CHECKING:
    from bot.main import Services

log = logging.getLogger(__name__)

UNBAN_BATCH = 50


class UnbanRunner:
    def __init__(self, services: "Services") -> None:
        self._services = services

    async def tick(self) -> int:
        """Process the due unbans; returns how many bans were lifted. Never raises."""
        services = self._services
        try:
            due = await services.repo.due_unbans(services.clock.now(), limit=UNBAN_BATCH)
        except Exception:
            log.exception("Could not list due unbans")
            return 0
        lifted = 0
        for unban in due:
            try:
                lifted += await self.process(unban)
            except Exception:
                log.exception("Scheduled unban of %s in guild %s failed unexpectedly", unban.user_id, unban.guild_id)
        return lifted

    async def process(self, unban: ScheduledUnban) -> bool:
        services = self._services
        async with services.mod_locks.get(unban.guild_id):
            current = await services.repo.get_scheduled_unban(unban.guild_id, unban.user_id)
            now = services.clock.now()
            if current is None or current.unban_at != unban.unban_at or current.unban_at > now:
                return False  # lifted, cancelled or replaced by a newer ban meanwhile
            if current.next_attempt_at is not None and current.next_attempt_at > now:
                return False
            label = f"case #{current.case_id}" if current.case_id is not None else "no case"
            reason = "Tempban expired"
            try:
                await services.ops.unban_user(current.guild_id, current.user_id, reason=f"{reason} ({label})")
            except OpNotFound:
                reason = "Tempban expired (the ban had already been lifted)"
            except OpError as exc:
                delay = retry_delay(current.attempts + 1)
                failed = await services.repo.record_unban_error(
                    current.guild_id, current.user_id, f"unban: {exc.describe()}", now + timedelta(seconds=delay)
                )
                log.warning(
                    "Scheduled unban of %s in guild %s failed (attempt %d): %s (retry in %.0fs)",
                    current.user_id, current.guild_id, failed.attempts if failed else current.attempts + 1,
                    exc.describe(), delay,
                )
                return False
            case = await services.repo.complete_scheduled_unban(
                current, moderator_id=services.ops.bot_user_id, reason=reason, now=services.clock.now()
            )
        log.info("Tempban of %s in guild %s expired; ban lifted (%s)", current.user_id, current.guild_id, label)
        if case is not None:
            await log_case(services, case)
        return True
