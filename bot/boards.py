"""Board rendering, periodic edits, single-board recovery and crash-safe reposting.

Normal updates edit the four existing board messages in place. Only a confirmed
``NotFound`` causes that one board to be reposted; Forbidden, rate limits and
transient failures just skip the round.

Full reposts (first setup, a moved leaderboard channel, or a ``repost_needed``
flag left by an older version that announced resets) use a persisted generation
protocol stored in ``board_repost``:

1. ``POSTING`` is persisted with a new generation and the old message IDs.
2. Recent channel history is scanned for bot messages carrying this
   generation's marker; matches are adopted instead of reposted.
3. The still-missing boards are posted in order, each new ID persisted
   immediately after its send succeeds.
4. ``DELETING`` is persisted, old boards are deleted (NotFound = success).
5. ``board_messages`` is switched to the new IDs and the op marked ``DONE``
   in one transaction.

Old boards are never deleted before all four replacements exist, so a crash at
any point leaves at least one functioning set of boards (possibly temporarily
duplicated), and rerunning the procedure converges to exactly four boards.

All public methods assume the caller holds the guild's lock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord

from bot.clock import Clock
from bot.db.repo import DECIDED_STATUSES, BoardMessage, GuildConfig, RepostOp, RepostStatus, Repo, StaleRunError
from bot.discord_ops import (
    RETRYABLE_ERRORS,
    DiscordOps,
    OpBadRequest,
    OpError,
    OpForbidden,
    OpNotFound,
    Presence,
    check_presence,
)
from bot.periods import ALLTIME, BOARD_ORDER, PeriodType, next_reset, period_before, period_containing
from bot.ranking import Ranker
from bot.ui.embeds import TOP_SHOWN, PreviousWinner, board_marker, build_board_embed, parse_board_marker
from bot.ui.views import board_view

log = logging.getLogger(__name__)

REPOST_SCAN_MARGIN = timedelta(minutes=15)
REPOST_SCAN_LIMIT = 1000  # paged by discord.py, 100 messages per request
SINGLE_SCAN_LIMIT = 100
SWEEP_LIMIT = 200
MAX_DELETE_ATTEMPTS = 5
AUDIT_TTL = timedelta(hours=6)
# Pause between the four in-place edits of one refresh. Sent back to back, the
# last edit hit Discord's per-channel rate limit (429, ~5 s retry) every cycle.
EDIT_SPACING_SECONDS = 2.0


async def _edit_pause(delay: float) -> None:
    """Indirection so tests can skip the pause between board edits."""
    await asyncio.sleep(delay)


class BoardService:
    def __init__(self, repo: Repo, ranker: Ranker, ops: DiscordOps, clock: Clock) -> None:
        self._repo = repo
        self._ranker = ranker
        self._ops = ops
        self._clock = clock
        self._audited: dict[tuple[int, int], datetime] = {}

    # --------------------------------------------------------------- render

    async def _previous_winner(
        self, guild_id: int, board_type: str, cfg: GuildConfig, now: datetime
    ) -> PreviousWinner | None:
        """Result of the period immediately before the current one, or ``None`` if it was never tracked.

        A period is tracked when it ended after installation (see
        ``ended_periods``). A tracked period whose run is missing or not yet
        decided is reported as pending; an older result is never substituted.
        """
        period = period_before(period_containing(board_type, now))
        if cfg.installed_at is None or period.end <= cfg.installed_at:
            return None
        run = await self._repo.previous_result(guild_id, PeriodType(board_type), period.key)
        if run is None or run.status not in DECIDED_STATUSES:
            return PreviousWinner(period.key, None, None, None, pending=True)
        if run.winner_user_id is None:
            return PreviousWinner(run.period_key, None, None, None)
        member = await self._repo.get_member(guild_id, run.winner_user_id)
        name = (member.display_name or member.username) if member else None
        left = member is not None and member.left_at is not None
        return PreviousWinner(run.period_key, run.winner_user_id, name, run.winner_count, left)

    async def render(
        self, guild_id: int, board_type: str, generation: int, cfg: GuildConfig, *, with_banner: bool = True
    ) -> discord.Embed:
        now = self._clock.now()
        entries = await self._ranker.board_ranking(guild_id, board_type, now, limit=TOP_SHOWN)
        previous = None if board_type == ALLTIME else await self._previous_winner(guild_id, board_type, cfg, now)
        reset_at = None if board_type == ALLTIME else next_reset(board_type, now)
        return build_board_embed(
            guild_id=guild_id,
            board_type=board_type,
            generation=generation,
            entries=entries,
            previous=previous,
            next_reset=reset_at,
            banner_url=cfg.banner_url if with_banner else None,
            now=now,
        )

    # -------------------------------------------------------- normal update

    async def update_guild(self, guild_id: int) -> None:
        """Edit the existing boards in place (the five-minute refresh)."""
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is None or not cfg.installed:
            return
        if await self._repo.get_active_repost(guild_id) is not None:
            # The repost procedure owns the boards until it finishes.
            if not await self.process_repost(guild_id):
                return
        boards = await self._repo.get_board_messages(guild_id)
        if not boards or any(b.channel_id != cfg.board_channel_id for b in boards.values()):
            # No boards yet, or /setup moved the leaderboard channel (e.g. a crash
            # happened before its repost started): move them with a full repost.
            reason = "missing boards" if not boards else "board channel changed"
            log.info("Guild %s: %s; starting a repost", guild_id, reason)
            await self.request_repost(guild_id, reason)
            await self.process_repost(guild_id)
            return
        await self._audit_members(guild_id)
        generation = max(b.generation for b in boards.values())
        for index, board_type in enumerate(BOARD_ORDER):
            if index:
                await _edit_pause(EDIT_SPACING_SECONDS)
            board = boards.get(board_type)
            if board is None:
                await self._recreate_single(guild_id, board_type, None, generation, cfg)
                continue
            await self._edit_board(guild_id, board, cfg)

    async def _edit_board(self, guild_id: int, board: BoardMessage, cfg: GuildConfig) -> None:
        embed = await self.render(guild_id, board.board_type, board.generation, cfg)
        try:
            await self._ops.edit_message(board.channel_id, board.message_id, embed=embed, view=board_view(board.board_type))
        except OpNotFound:
            log.warning(
                "Board %s message %s in guild %s is missing; recreating that board only",
                board.board_type,
                board.message_id,
                guild_id,
            )
            await self._recreate_single(guild_id, board.board_type, board, board.generation, cfg)
        except OpBadRequest as exc:
            if cfg.banner_url:
                log.error(
                    "Discord rejected board %s in guild %s (%s); retrying without the banner image. "
                    "Check the configured banner URL.",
                    board.board_type,
                    guild_id,
                    exc.describe(),
                )
                embed = await self.render(guild_id, board.board_type, board.generation, cfg, with_banner=False)
                try:
                    await self._ops.edit_message(
                        board.channel_id, board.message_id, embed=embed, view=board_view(board.board_type)
                    )
                except OpError as exc2:
                    log.error("Board %s edit failed in guild %s: %s", board.board_type, guild_id, exc2.describe())
            else:
                log.error("Board %s edit rejected in guild %s: %s", board.board_type, guild_id, exc.describe())
        except OpForbidden as exc:
            log.error(
                "Missing permission to edit board %s in guild %s: %s", board.board_type, guild_id, exc.describe()
            )
        except RETRYABLE_ERRORS as exc:
            log.warning(
                "Board %s edit in guild %s failed temporarily (%s); will retry next cycle",
                board.board_type,
                guild_id,
                exc.describe(),
            )

    async def _recreate_single(
        self,
        guild_id: int,
        board_type: str,
        board: BoardMessage | None,
        generation: int,
        cfg: GuildConfig,
    ) -> None:
        """Repost one missing board, adopting an orphan from a previous attempt if present."""
        assert cfg.board_channel_id is not None
        channel_id = cfg.board_channel_id
        marker = board_marker(guild_id, board_type, generation)
        try:
            history = await self._ops.recent_history(channel_id, after=None, limit=SINGLE_SCAN_LIMIT)
        except OpError as exc:
            log.warning("Cannot scan history to recover board %s in guild %s: %s", board_type, guild_id, exc.describe())
            return
        bot_id = self._ops.bot_user_id
        stale_id = board.message_id if board else None
        for msg in reversed(history):  # newest first
            if msg.author_id == bot_id and msg.id != stale_id and msg.contains_marker(marker):
                await self._repo.set_board_message(guild_id, board_type, channel_id, msg.id, generation)
                log.info("Adopted existing board %s message %s in guild %s", board_type, msg.id, guild_id)
                return
        try:
            message_id = await self._send_board(guild_id, board_type, generation, cfg, channel_id)
        except OpError as exc:
            log.error("Could not recreate board %s in guild %s: %s", board_type, guild_id, exc.describe())
            return
        await self._repo.set_board_message(guild_id, board_type, channel_id, message_id, generation)
        log.info("Recreated board %s as message %s in guild %s", board_type, message_id, guild_id)
        tracked = {b.message_id for b in (await self._repo.get_board_messages(guild_id)).values()}
        await self._sweep_strays(guild_id, channel_id, keep=tracked, max_generation=generation)

    async def _send_board(
        self, guild_id: int, board_type: str, generation: int, cfg: GuildConfig, channel_id: int
    ) -> int:
        """Post one board; if Discord rejects the banner image, post it without the banner."""
        embed = await self.render(guild_id, board_type, generation, cfg)
        try:
            return await self._ops.send_message(channel_id, embed=embed, view=board_view(board_type))
        except OpBadRequest:
            if not cfg.banner_url:
                raise
            log.error("Board %s rejected with banner in guild %s; sending without it", board_type, guild_id)
        embed = await self.render(guild_id, board_type, generation, cfg, with_banner=False)
        return await self._ops.send_message(channel_id, embed=embed, view=board_view(board_type))

    async def _sweep_strays(self, guild_id: int, channel_id: int, *, keep: set[int], max_generation: int) -> None:
        """Delete untracked bot board messages of this guild (same or older generation).

        Strays come from missed adoption scans (very busy channels) or from a
        message created twice by a transport-level retry. Best effort: a failed
        scan or delete is logged and retried on a later repost/recreation.
        """
        try:
            history = await self._ops.recent_history(channel_id, after=None, limit=SWEEP_LIMIT)
        except OpError as exc:
            log.warning("Stray-board sweep skipped in guild %s: %s", guild_id, exc.describe())
            return
        bot_id = self._ops.bot_user_id
        for msg in history:
            if msg.author_id != bot_id or msg.id in keep:
                continue
            parsed = next((m for m in map(parse_board_marker, msg.marker_texts) if m), None)
            if parsed is None or parsed[0] != guild_id or parsed[2] > max_generation:
                continue
            try:
                await self._ops.delete_message(channel_id, msg.id)
                log.info("Deleted stray %s board message %s (generation %s) in guild %s", parsed[1], msg.id, parsed[2], guild_id)
            except OpNotFound:
                pass
            except OpError as exc:
                log.warning("Could not delete stray board message %s in guild %s: %s", msg.id, guild_id, exc.describe())

    async def _audit_members(self, guild_id: int) -> None:
        """Refresh the "(left)" state of users shown in the top 10s (rate-limited by a TTL).

        Without the privileged Members intent a departure can only be confirmed
        by a ``NotFound`` from ``fetch_member``. Errors other than NotFound never
        mark anyone as departed.
        """
        now = self._clock.now()
        users: dict[int, bool] = {}
        for board_type in BOARD_ORDER:
            for entry in await self._ranker.board_ranking(guild_id, board_type, now, limit=TOP_SHOWN):
                users.setdefault(entry.user_id, entry.left)
        for user_id, marked_left in users.items():
            key = (guild_id, user_id)
            last = self._audited.get(key)
            if last is not None and now - last < AUDIT_TTL:
                continue
            result = await check_presence(self._ops, guild_id, user_id, attempts=1)
            if result.presence is Presence.UNKNOWN:
                continue
            observed = self._clock.now()  # when the answer arrived, not when the audit began
            if result.presence is Presence.ABSENT and not marked_left:
                if await self._repo.mark_member_left(guild_id, user_id, observed):
                    log.info("Member %s confirmed to have left guild %s", user_id, guild_id)
                else:
                    continue  # seen after the check (clock skew / just rejoined): re-check next cycle
            elif result.presence is Presence.PRESENT and marked_left:
                await self._repo.mark_member_present(guild_id, user_id)
            self._audited[key] = observed
        if len(self._audited) > 50_000:  # bound memory on very long uptimes
            self._audited.clear()

    # ------------------------------------------------------------- reposts

    async def request_repost(self, guild_id: int, reason: str) -> RepostOp | None:
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is None or not cfg.installed:
            return None
        assert cfg.board_channel_id is not None
        op, created = await self._repo.create_repost(guild_id, cfg.board_channel_id, reason, self._clock.now())
        if created:
            log.info(
                "Board repost started: guild=%s generation=%s reason=%s old=%s",
                guild_id,
                op.generation,
                reason,
                sorted(op.old_ids),
            )
        return op

    async def reconcile(self, guild_id: int) -> bool:
        """Finish any in-progress repost, then start one if a legacy ``repost_needed`` flag asks for it.

        Resets no longer set the flag (they don't post announcements), but a
        database written by an older version may still have it set.
        Returns ``True`` when no repost work remains.
        """
        if not await self.process_repost(guild_id):
            return False
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is not None and cfg.repost_needed:
            await self.request_repost(guild_id, "legacy reset announcement")
            return await self.process_repost(guild_id)
        return True

    async def _fail_repost(self, op: RepostOp, step: str, exc: OpError) -> int:
        attempts = await self._repo.repost_record_error(
            op.guild_id, op.generation, f"{step}: {exc.describe()}", self._clock.now()
        )
        log.warning(
            "Board repost guild=%s generation=%s failed at %s (attempt %d): %s",
            op.guild_id,
            op.generation,
            step,
            attempts,
            exc.describe(),
        )
        return attempts

    async def process_repost(self, guild_id: int) -> bool:
        """Advance the active repost operation as far as possible. ``True`` when none remains."""
        op = await self._repo.get_active_repost(guild_id)
        if op is None:
            return True
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is None or not cfg.installed:
            return False
        if op.status is RepostStatus.POSTING and op.channel_id != cfg.board_channel_id:
            return await self._abandon(op)
        try:
            if op.status is RepostStatus.POSTING:
                op = await self._post_missing(op, cfg)
                if op is None:
                    return False
            if op.status is RepostStatus.DELETING:
                return await self._delete_old(op)
        except StaleRunError:
            log.warning("Board repost for guild %s changed concurrently; will re-read next cycle", guild_id)
            return False
        return op.status is RepostStatus.DONE

    async def _abandon(self, op: RepostOp) -> bool:
        """Drop a POSTING op whose target channel is no longer the configured one.

        The old boards it would have replaced stay tracked in ``board_messages``
        (nothing was deleted yet), so the next repost replaces them in the new
        channel. Boards this op already posted are removed best-effort; failures
        (e.g. the old channel was deleted) do not block the move.
        """
        for board_type, message_id in op.new_ids.items():
            try:
                await self._ops.delete_message(op.channel_id, message_id)
            except OpError as exc:
                log.warning(
                    "Could not delete %s board %s from abandoned repost in guild %s: %s",
                    board_type,
                    message_id,
                    op.guild_id,
                    exc.describe(),
                )
        await self._repo.repost_abandon(op.guild_id, op.generation, self._clock.now())
        log.warning(
            "Board repost guild=%s generation=%s abandoned: the leaderboard channel changed",
            op.guild_id,
            op.generation,
        )
        return True

    async def _post_missing(self, op: RepostOp, cfg: GuildConfig) -> RepostOp | None:
        missing = [t for t in BOARD_ORDER if t not in op.new_ids]
        if missing:
            # Adopt boards from this generation that were sent before a crash
            # but whose IDs never reached the database.
            try:
                history = await self._ops.recent_history(
                    op.channel_id, after=op.created_at - REPOST_SCAN_MARGIN, limit=REPOST_SCAN_LIMIT
                )
            except OpError as exc:
                await self._fail_repost(op, "history scan", exc)
                return None
            bot_id = self._ops.bot_user_id
            known = set(op.new_ids.values())
            for msg in history:
                if msg.author_id != bot_id or msg.id in known:
                    continue
                for board_type in list(missing):
                    if msg.contains_marker(board_marker(op.guild_id, board_type, op.generation)):
                        op = await self._repo.repost_add_new_id(
                            op.guild_id, op.generation, board_type, msg.id, self._clock.now()
                        )
                        known.add(msg.id)
                        missing.remove(board_type)
                        log.info(
                            "Board repost guild=%s generation=%s adopted %s message %s from history",
                            op.guild_id,
                            op.generation,
                            board_type,
                            msg.id,
                        )
                        break
            for board_type in [t for t in BOARD_ORDER if t not in op.new_ids]:
                try:
                    message_id = await self._send_board(op.guild_id, board_type, op.generation, cfg, op.channel_id)
                except OpError as exc:
                    await self._fail_repost(op, f"send {board_type}", exc)
                    return None
                # Persist immediately: a crash after this point is recovered by ID.
                op = await self._repo.repost_add_new_id(
                    op.guild_id, op.generation, board_type, message_id, self._clock.now()
                )
        op = await self._repo.repost_set_status(
            op.guild_id, op.generation, RepostStatus.POSTING, RepostStatus.DELETING, self._clock.now()
        )
        log.info("Board repost guild=%s generation=%s: all four boards posted", op.guild_id, op.generation)
        return op

    async def _delete_old(self, op: RepostOp) -> bool:
        new_ids = set(op.new_ids.values())
        notes: list[str] = []
        for board_type, (channel_id, message_id) in op.old_ids.items():
            if message_id in new_ids:
                continue
            try:
                await self._ops.delete_message(channel_id, message_id)
            except OpNotFound:
                pass  # already gone: exactly what we wanted
            except OpForbidden as exc:
                attempts = await self._fail_repost(op, f"delete old {board_type}", exc)
                if attempts < MAX_DELETE_ATTEMPTS:
                    return False
                note = f"could not delete old {board_type} board {message_id}: {exc.describe()}"
                log.error("Board repost guild=%s: giving up — %s", op.guild_id, note)
                notes.append(note)
            except OpError as exc:
                await self._fail_repost(op, f"delete old {board_type}", exc)
                return False
        await self._sweep_strays(op.guild_id, op.channel_id, keep=new_ids, max_generation=op.generation)
        await self._repo.repost_complete(op.guild_id, op.generation, self._clock.now(), "; ".join(notes) or None)
        log.info("Board repost guild=%s generation=%s complete", op.guild_id, op.generation)
        return True

    # ----------------------------------------------------------------- setup

    async def ensure_boards(self, guild_id: int) -> bool:
        """Create the boards if needed (used by /setup). Safe to call repeatedly."""
        cfg = await self._repo.get_guild_config(guild_id)
        if cfg is None or not cfg.installed:
            return False
        if not await self.process_repost(guild_id):
            return False
        boards = await self._repo.get_board_messages(guild_id)
        complete = set(boards) == set(BOARD_ORDER)
        same_channel = all(b.channel_id == cfg.board_channel_id for b in boards.values())
        if complete and same_channel:
            await self.update_guild(guild_id)
            return True
        await self.request_repost(guild_id, "setup")
        return await self.process_repost(guild_id)
