"""Moderation actions and their safety rules.

Every action runs in the same order:

1. **Safety checks**, failing closed: nobody can act on themselves, the bot or
   the server owner; the moderator's highest role must be strictly above the
   target's (the owner is exempt); for anything Discord itself enforces, the
   bot's highest role must be strictly above the target's too. Roles are
   always fetched fresh (cached members never receive role updates without the
   Members intent). If they can't be fetched, nothing happens.
2. **The Discord action**, with ``moderator (id): reason`` as the audit-log
   reason. If Discord refuses, there is no case.
3. **The case record**, only after Discord succeeded. If that write fails the
   action still happened, so the error is logged and the action is reported as
   done (and still posted to the mod log, without a case number).
4. **The mod-log entry**, best effort: a failed post never fails the action.

Command callbacks (:mod:`bot.commands.moderation`) are thin wrappers over the
functions here, which tests call directly. Every refusal is a
:class:`ModError` whose message is safe to show to the moderator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

import discord

from bot.db.repo import ModCase
from bot.discord_ops import (
    MemberRank,
    OpBadRequest,
    OpError,
    OpForbidden,
    OpNotFound,
    with_retries,
)
from bot.periods import PeriodType, day_range, period_containing, unix_timestamp
from bot.ui.modlog import build_case_embed, build_info_embed, clip, format_duration

if TYPE_CHECKING:
    from bot.main import Services

log = logging.getLogger(__name__)

MAX_TIMEOUT = timedelta(days=28)  # Discord's limit
MAX_TEMPBAN = timedelta(days=365)
# Discord refuses to bulk-delete messages older than 14 days; keep a margin for clock skew.
BULK_DELETE_WINDOW = timedelta(days=14) - timedelta(minutes=5)
REASON_MAX = 400  # Discord's audit-log reason limit is 512, and it also carries the moderator
AUDIT_REASON_MAX = 512
LIST_LIMIT = 1900

_DURATION_PART = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_DURATION_FULL = re.compile(r"^\s*(?:\d+\s*[smhdw]\s*)+$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SNOWFLAKE = re.compile(r"^\s*<?@?!?(\d{15,21})>?\s*$")


class ModError(Exception):
    """A refused or failed moderation action; the message is shown to the moderator as is."""


@dataclass(frozen=True)
class Actor:
    """The moderator running an action."""

    user_id: int
    name: str

    def audit(self, reason: str | None) -> str:
        """Audit-log reason: Discord shows the bot as the actor, so name the moderator."""
        return clip(f"{self.name} ({self.user_id}): {reason or 'no reason given'}", AUDIT_REASON_MAX)


# ------------------------------------------------------------------- parsing


def parse_duration(text: str) -> timedelta:
    """``30s``, ``10m``, ``2h``, ``7d``, ``1w`` or combinations like ``1h30m``."""
    if not text or not _DURATION_FULL.match(text):
        raise ModError("Use a duration like `30s`, `10m`, `2h`, `7d` or `1h30m`.")
    seconds = sum(int(n) * _UNIT_SECONDS[unit.lower()] for n, unit in _DURATION_PART.findall(text))
    if seconds <= 0:
        raise ModError("The duration must be longer than zero.")
    if seconds > 10 * 365 * 86400:
        raise ModError("That duration is far too long.")
    return timedelta(seconds=seconds)


def parse_user_id(text: str) -> int:
    """A user ID typed by hand (raw digits or a ``<@id>`` mention)."""
    match = _SNOWFLAKE.match(text or "")
    if match is None:
        raise ModError("That isn't a valid user ID. Copy it with Developer Mode → right-click → Copy User ID.")
    return int(match[1])


def fit_lines(header: str, lines: list[str], *, limit: int = LIST_LIMIT) -> str:
    """Whole lines under Discord's 2000-character limit, saying how many were left out."""
    shown: list[str] = []
    length = len(header)
    for i, line in enumerate(lines):
        if length + len(line) + 1 > limit - 40:  # room for the "more" marker
            shown.append(f"…and {len(lines) - i} more not shown")
            break
        shown.append(line)
        length += len(line) + 1
    return "\n".join([header, *shown])


def _ts(moment: datetime, style: str = "f") -> str:
    return f"<t:{unix_timestamp(moment)}:{style}>"


def case_note(case: ModCase | None) -> str:
    if case is None:
        return " (The case record couldn't be saved; the error was logged.)"
    return f" Case #{case.case_id}."


# ------------------------------------------------------------------- safety


async def _rank(services: "Services", guild_id: int, user_id: int) -> MemberRank:
    return await with_retries(lambda: services.ops.member_rank(guild_id, user_id), name="member_rank")


async def check_target(
    services: "Services",
    guild_id: int,
    actor: Actor,
    target_id: int,
    *,
    verb: str,
    bot_acts: bool = True,
    require_member: bool = True,
) -> MemberRank | None:
    """Apply the safety rules; raise :class:`ModError` if the action is not allowed.

    ``bot_acts``: Discord performs the action as the bot, so the bot's role
    must outrank the target too. ``require_member=False`` allows targets who
    aren't in the server (bans); they have no roles to compare, and ``None``
    is returned for them.
    """
    if target_id == actor.user_id:
        raise ModError(f"You can't {verb} yourself.")
    if target_id == services.ops.bot_user_id:
        raise ModError(f"I won't {verb} myself.")
    try:
        target = await _rank(services, guild_id, target_id)
    except OpNotFound:
        if require_member:
            raise ModError(f"<@{target_id}> isn't a member of this server.") from None
        return None
    except OpError as exc:
        raise ModError(f"I couldn't check <@{target_id}>'s roles ({exc.kind}), so nothing was done. Try again.") from exc
    if target.owner:
        raise ModError(f"You can't {verb} the server owner.")
    try:
        moderator = await _rank(services, guild_id, actor.user_id)
    except OpError as exc:
        raise ModError(f"I couldn't check your roles ({exc.kind}), so nothing was done. Try again.") from exc
    if not moderator.owner and not moderator.top_role > target.top_role:
        raise ModError(f"You can only {verb} members whose highest role is below yours.")
    if bot_acts:
        try:
            bot = await _rank(services, guild_id, services.ops.bot_user_id)
        except OpError as exc:
            raise ModError(f"I couldn't check my own roles ({exc.kind}), so nothing was done. Try again.") from exc
        if not bot.top_role > target.top_role:
            raise ModError(
                f"My highest role isn't above <@{target_id}>'s highest role, so Discord won't let me {verb} them. "
                "Move my role higher in Server Settings → Roles."
            )
    return target


def discord_failure(exc: OpError, *, verb: str, target_id: int | None) -> ModError:
    """A clear, moderator-facing explanation of a failed Discord action."""
    who = f"<@{target_id}>" if target_id is not None else "that"
    if isinstance(exc, OpForbidden):
        return ModError(
            f"Discord refused to {verb} {who} ({exc}). Check my permissions and that my role is above theirs."
        )
    if isinstance(exc, OpNotFound):
        return ModError(f"Discord couldn't find {who} ({exc}).")
    if isinstance(exc, OpBadRequest):
        return ModError(f"Discord rejected the request to {verb} {who} ({exc}).")
    return ModError(
        f"Discord didn't respond properly ({exc.describe()}); the action may or may not have gone through. "
        "Check, then try again."
    )


async def run_discord_action(
    operation: Callable[[], Awaitable[Any]], *, verb: str, target_id: int | None, not_found: str | None = None
) -> None:
    """Run a Discord action (retrying transient errors), turning failures into a clear :class:`ModError`."""
    try:
        await with_retries(operation, name=verb)
    except OpError as exc:
        if not_found is not None and isinstance(exc, OpNotFound):
            raise ModError(not_found) from exc
        raise discord_failure(exc, verb=verb, target_id=target_id) from exc


# ------------------------------------------------------------------ mod log


async def post_modlog(services: "Services", guild_id: int, embed: discord.Embed) -> tuple[int, int] | None:
    """Post an entry to the configured mod-log channel, pinging nobody. Never raises."""
    try:
        cfg = await services.repo.get_guild_config(guild_id)
        if cfg is None or cfg.modlog_channel_id is None:
            return None
        channel_id = cfg.modlog_channel_id
        message_id = await services.ops.send_message(channel_id, embed=embed, mention_user_ids=[])
    except OpError as exc:
        log.warning("Could not post to the mod log of guild %s: %s", guild_id, exc.describe())
        return None
    except Exception:
        log.exception("Could not post to the mod log of guild %s", guild_id)
        return None
    return channel_id, message_id


def case_embed(case: ModCase) -> discord.Embed:
    return build_case_embed(
        case_id=case.case_id,
        action=case.action,
        target_id=case.target_id,
        moderator_id=case.moderator_id,
        reason=case.reason,
        duration_seconds=case.duration_seconds,
        created_at=case.created_at,
        extra=case.extra,
    )


async def log_case(services: "Services", case: ModCase) -> None:
    posted = await post_modlog(services, case.guild_id, case_embed(case))
    if posted is None:
        return
    try:
        await services.repo.set_case_modlog(case.guild_id, case.case_id, *posted)
    except Exception:
        log.exception("Could not store the mod-log message of case #%s in guild %s", case.case_id, case.guild_id)


async def record_case(
    services: "Services",
    guild_id: int,
    action: str,
    *,
    target_id: int | None,
    actor: Actor,
    reason: str | None,
    duration_seconds: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> ModCase | None:
    """Write the case for an action that already happened on Discord, then log it.

    Returns ``None`` if the write failed: the action stands, the error is
    logged and the mod log still gets an entry (without a case number).
    """
    now = services.clock.now()
    try:
        case = await services.repo.create_case(
            guild_id, action, target_id=target_id, moderator_id=actor.user_id, reason=reason, now=now,
            duration_seconds=duration_seconds, extra=extra,
        )
    except Exception:
        log.exception(
            "Moderation %s on %s in guild %s succeeded on Discord but its case could not be saved",
            action, target_id, guild_id,
        )
        embed = build_case_embed(
            case_id=None, action=action, target_id=target_id, moderator_id=actor.user_id, reason=reason,
            duration_seconds=duration_seconds, created_at=now, extra=extra,
        )
        await post_modlog(services, guild_id, embed)
        return None
    await log_case(services, case)
    return case


# ------------------------------------------------------------------ actions


async def apply_timeout(
    services: "Services", guild_id: int, actor: Actor, target_id: int, duration: timedelta, reason: str | None,
    *, extra: Mapping[str, Any] | None = None,
) -> tuple[datetime, ModCase | None]:
    """Checks, Discord timeout and case. Returns ``(until, case)``; raises :class:`ModError` if refused."""
    if duration <= timedelta(0) or duration > MAX_TIMEOUT:
        raise ModError("A timeout must be between 1 second and 28 days.")
    target = await check_target(services, guild_id, actor, target_id, verb="time out")
    assert target is not None
    if target.administrator:
        raise ModError(f"Discord doesn't allow timing out <@{target_id}>: they have the Administrator permission.")
    until = services.clock.now() + duration
    await run_discord_action(
        lambda: services.ops.timeout_member(guild_id, target_id, until, reason=actor.audit(reason)),
        verb="time out", target_id=target_id,
    )
    case = await record_case(
        services, guild_id, "timeout", target_id=target_id, actor=actor, reason=reason,
        duration_seconds=int(duration.total_seconds()), extra=extra,
    )
    return until, case


async def timeout(
    services: "Services", guild_id: int, actor: Actor, target_id: int, duration: timedelta, reason: str | None
) -> str:
    until, case = await apply_timeout(services, guild_id, actor, target_id, duration, reason)
    return f"<@{target_id}> is timed out until {_ts(until)} ({_ts(until, 'R')}).{case_note(case)}"


async def untimeout(services: "Services", guild_id: int, actor: Actor, target_id: int, reason: str | None) -> str:
    target = await check_target(services, guild_id, actor, target_id, verb="remove the timeout of")
    assert target is not None
    if target.timed_out_until is None or target.timed_out_until <= services.clock.now():
        raise ModError(f"<@{target_id}> isn't timed out.")
    await run_discord_action(
        lambda: services.ops.timeout_member(guild_id, target_id, None, reason=actor.audit(reason)),
        verb="remove the timeout of", target_id=target_id,
    )
    case = await record_case(services, guild_id, "untimeout", target_id=target_id, actor=actor, reason=reason)
    return f"Removed the timeout of <@{target_id}>.{case_note(case)}"


async def _notify(services: "Services", user_id: int, content: str) -> bool:
    """Best-effort DM: closed DMs or any Discord error just mean "not delivered"."""
    try:
        return await services.ops.dm_user(user_id, content=content)
    except OpError as exc:
        log.info("Could not DM user %s: %s", user_id, exc.describe())
        return False


async def warn(
    services: "Services",
    guild_id: int,
    guild_name: str,
    actor: Actor,
    target_id: int,
    reason: str,
    *,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Warn a member: DM them (the Discord part, best effort), record the case, then escalate.

    A DM that can't be delivered never fails the warning. When the new active
    count matches a ``/warnconfig`` threshold, an automatic timeout follows as
    its own case; if it isn't possible the warning still stands and the reason
    is reported here and in the mod log.
    """
    await check_target(services, guild_id, actor, target_id, verb="warn", bot_acts=False)
    server = discord.utils.escape_markdown(guild_name)
    delivered = await _notify(services, target_id, clip(f"You received a warning in **{server}**.\nReason: {reason}", 2000))
    try:
        case, active = await services.repo.create_warning(
            guild_id, target_id, moderator_id=actor.user_id, reason=reason, now=services.clock.now(),
            extra={**(extra or {}), "dm": delivered},
        )
    except Exception as exc:
        log.exception("Could not save a warning for %s in guild %s", target_id, guild_id)
        sent = " (they did get the DM)" if delivered else ""
        raise ModError(f"The warning couldn't be saved{sent}, so it doesn't count. The error was logged.") from exc
    await log_case(services, case)
    dm = "They were notified by DM." if delivered else "I couldn't DM them (closed DMs), but the warning is recorded."
    message = f"Warned <@{target_id}> (case #{case.case_id}). Active warnings: **{active}**. {dm}"
    escalation = await escalate(services, guild_id, actor, target_id, active, case.case_id)
    return f"{message}\n{escalation}" if escalation else message


async def escalate(
    services: "Services", guild_id: int, actor: Actor, target_id: int, active: int, warn_case_id: int
) -> str | None:
    """Apply the timeout configured for exactly ``active`` warnings, if any. Never raises."""
    try:
        seconds = await services.repo.get_warn_threshold(guild_id, active)
    except Exception:
        log.exception("Could not read the warn thresholds of guild %s", guild_id)
        return "**Auto-timeout not checked:** the escalation rules couldn't be read (error logged)."
    if seconds is None:
        return None
    length = format_duration(seconds)
    try:
        until, case = await apply_timeout(
            services, guild_id, actor, target_id, timedelta(seconds=seconds), f"Auto: {active} warnings",
            extra={"warn_case": warn_case_id},
        )
    except ModError as exc:
        log.info("Auto-timeout for %s in guild %s not applied: %s", target_id, guild_id, exc)
        await post_modlog(services, guild_id, build_info_embed(
            "Auto-timeout not applied",
            f"<@{target_id}> reached {active} warnings (warning #{warn_case_id}), which should time them out "
            f"for {length}, but it wasn't possible: {exc}",
            moderator_id=actor.user_id, created_at=services.clock.now(),
        ))
        return f"**Auto-timeout not applied** ({active} warnings → {length}): {exc}"
    return f"That's {active} warnings, so they're timed out for {length} (until {_ts(until)}).{case_note(case)}"


async def delete_and_warn(
    services: "Services",
    guild_id: int,
    guild_name: str,
    actor: Actor,
    *,
    channel_id: int,
    message_id: int,
    author_id: int,
    content: str | None,
    reason: str,
) -> str:
    """Delete one message, then warn its author (the warning records where and what it was)."""
    await check_target(services, guild_id, actor, author_id, verb="warn", bot_acts=False)
    try:
        await with_retries(lambda: services.ops.delete_message(channel_id, message_id), name="delete_message")
    except OpNotFound:
        pass  # already deleted: the warning still applies
    except OpError as exc:
        raise discord_failure(exc, verb="delete the message of", target_id=author_id) from exc
    excerpt = clip(discord.utils.escape_markdown(content.strip()), 300) if content and content.strip() else None
    deleted = {"channel_id": channel_id, "message_id": message_id, "excerpt": excerpt}
    message = await warn(services, guild_id, guild_name, actor, author_id, reason, extra={"deleted_message": deleted})
    return f"Deleted the message. {message}"


async def list_warnings(services: "Services", guild_id: int, user_id: int) -> str:
    warnings = await services.repo.active_warnings(guild_id, user_id)
    if not warnings:
        return f"<@{user_id}> has no active warnings."
    lines = [
        f"**#{w.case_id}** · {_ts(w.created_at, 'd')} · by <@{w.moderator_id}> · "
        f"{discord.utils.escape_markdown(clip(w.reason or 'no reason', 120))}"
        for w in warnings
    ]
    return fit_lines(f"Active warnings for <@{user_id}> ({len(warnings)}):", lines)


async def clear_warning(services: "Services", guild_id: int, actor: Actor, case_id: int) -> str:
    case = await services.repo.clear_warning(guild_id, case_id)
    if case is None:
        raise ModError(f"Case #{case_id} isn't an active warning in this server.")
    await post_modlog(services, guild_id, build_info_embed(
        "Warning cleared", f"Warning #{case_id} for <@{case.target_id}> no longer counts.",
        moderator_id=actor.user_id, created_at=services.clock.now(),
    ))
    return f"Cleared warning #{case_id} for <@{case.target_id}>. The case stays in the history."


async def clear_warnings(services: "Services", guild_id: int, actor: Actor, user_id: int) -> str:
    cleared = await services.repo.clear_warnings(guild_id, user_id)
    if not cleared:
        raise ModError(f"<@{user_id}> has no active warnings.")
    await post_modlog(services, guild_id, build_info_embed(
        "Warnings cleared", f"{cleared} active warning(s) for <@{user_id}> no longer count.",
        moderator_id=actor.user_id, created_at=services.clock.now(),
    ))
    return f"Cleared {cleared} warning(s) for <@{user_id}>. The cases stay in the history."


async def kick(services: "Services", guild_id: int, actor: Actor, target_id: int, reason: str | None) -> str:
    await check_target(services, guild_id, actor, target_id, verb="kick")
    await run_discord_action(
        lambda: services.ops.kick_member(guild_id, target_id, reason=actor.audit(reason)),
        verb="kick", target_id=target_id,
    )
    case = await record_case(services, guild_id, "kick", target_id=target_id, actor=actor, reason=reason)
    return f"Kicked <@{target_id}>.{case_note(case)}"


async def ban(
    services: "Services",
    guild_id: int,
    actor: Actor,
    target_id: int,
    reason: str | None,
    *,
    delete_message_days: int = 0,
    duration: timedelta | None = None,
) -> str:
    """Ban a user, permanently or (with ``duration``) until a scheduled unban.

    A permanent ban cancels any pending scheduled unban from an earlier tempban,
    and a new tempban replaces it, so a stale schedule never lifts a newer ban.
    """
    if not 0 <= delete_message_days <= 7:
        raise ModError("Messages can be deleted for 0 to 7 days.")
    if duration is not None and not timedelta(0) < duration <= MAX_TEMPBAN:
        raise ModError("A temporary ban can last at most 365 days. Leave the duration empty for a permanent ban.")
    await check_target(services, guild_id, actor, target_id, verb="ban", require_member=False)
    extra = {"delete_message_days": delete_message_days} if delete_message_days else None
    seconds = int(duration.total_seconds()) if duration is not None else None
    async with services.mod_locks.get(guild_id):  # shared with the unban scheduler
        await run_discord_action(
            lambda: services.ops.ban_user(
                guild_id, target_id, delete_message_seconds=delete_message_days * 86400, reason=actor.audit(reason)
            ),
            verb="ban", target_id=target_id,
        )
        case = await record_case(
            services, guild_id, "ban", target_id=target_id, actor=actor, reason=reason,
            duration_seconds=seconds, extra=extra,
        )
        if duration is None:
            schedule = await _cancel_scheduled_unban(services, guild_id, target_id)
            return f"Banned <@{target_id}> permanently.{case_note(case)}{schedule}"
        until = services.clock.now() + duration
        schedule = await _schedule_unban(services, guild_id, target_id, until, case)
    return (
        f"Banned <@{target_id}> for {format_duration(seconds or 0)} "
        f"(until {_ts(until)}, {_ts(until, 'R')}).{case_note(case)}{schedule}"
    )


async def _schedule_unban(
    services: "Services", guild_id: int, user_id: int, until: datetime, case: ModCase | None
) -> str:
    try:
        await services.repo.schedule_unban(guild_id, user_id, until, case.case_id if case else None)
    except Exception:
        log.exception("Tempban of %s in guild %s: the automatic unban could not be scheduled", user_id, guild_id)
        return (
            f"\n**The automatic unban couldn't be scheduled** (error logged), so this ban won't end by itself. "
            f"Unban them manually after {_ts(until)}."
        )
    return ""


async def _cancel_scheduled_unban(services: "Services", guild_id: int, user_id: int) -> str:
    try:
        cancelled = await services.repo.delete_scheduled_unban(guild_id, user_id)
    except Exception:
        log.exception("Could not cancel the scheduled unban of %s in guild %s", user_id, guild_id)
        return (
            "\n**Warning:** an earlier tempban's automatic unban couldn't be cancelled (error logged) and may still "
            "lift this ban. Check again later."
        )
    return " Their earlier tempban's automatic unban was cancelled." if cancelled else ""


async def unban(services: "Services", guild_id: int, actor: Actor, user_id: int, reason: str | None) -> str:
    async with services.mod_locks.get(guild_id):  # shared with the unban scheduler
        await run_discord_action(
            lambda: services.ops.unban_user(guild_id, user_id, reason=actor.audit(reason)),
            verb="unban", target_id=user_id, not_found=f"<@{user_id}> (`{user_id}`) isn't banned.",
        )
        try:
            await services.repo.delete_scheduled_unban(guild_id, user_id)
        except Exception:
            # Harmless if it stays: when it comes due, "Unknown Ban" just closes it.
            log.exception("Could not delete the scheduled unban of %s in guild %s", user_id, guild_id)
        case = await record_case(services, guild_id, "unban", target_id=user_id, actor=actor, reason=reason)
    return f"Unbanned <@{user_id}> (`{user_id}`).{case_note(case)}"


async def purge(
    services: "Services", guild_id: int, actor: Actor, channel_id: int, amount: int, user_id: int | None
) -> str:
    """Delete among the newest ``amount`` messages, optionally only one user's.

    No hierarchy check: there is no single member being acted on, and Discord
    applies no role hierarchy to deleting messages (Manage Messages is enough).
    """
    if not 1 <= amount <= 100:
        raise ModError("You can purge 1 to 100 messages at a time.")
    not_before = services.clock.now() - BULK_DELETE_WINDOW
    reason = f"purge of the newest {amount} message(s)" + (f" by user {user_id}" if user_id else "")
    try:
        result = await with_retries(
            lambda: services.ops.purge(
                channel_id, limit=amount, author_id=user_id, not_before=not_before, reason=actor.audit(reason)
            ),
            name="purge",
        )
    except OpForbidden as exc:
        raise ModError(f"Discord refused the purge ({exc}). I need Manage Messages and Read Message History here.") from exc
    except OpError as exc:
        raise ModError(f"The purge failed ({exc.describe()}). Some messages may already be gone; check and retry.") from exc
    skipped = []
    if result.skipped_old:
        skipped.append(f"{result.skipped_old} older than 14 days (Discord can't bulk-delete those)")
    if result.skipped_pinned:
        skipped.append(f"{result.skipped_pinned} pinned")
    tail = f" Skipped {' and '.join(skipped)}." if skipped else ""
    who = f" from <@{user_id}>" if user_id else ""
    if not result.deleted:
        return f"Nothing to delete{who} among the newest {amount} message(s).{tail}"
    extra = {
        "channel_id": channel_id,
        "scanned": amount,
        "deleted": result.deleted,
        "skipped_old": result.skipped_old,
        "skipped_pinned": result.skipped_pinned,
    }
    case = await record_case(services, guild_id, "purge", target_id=user_id, actor=actor, reason=None, extra=extra)
    return f"Deleted {result.deleted} message(s){who}.{tail}{case_note(case)}"


# ------------------------------------------------------ leaderboard moderation


async def refresh_boards(services: "Services", guild_id: int) -> None:
    """Edit the boards now so a leaderboard change shows immediately. Never raises."""
    try:
        async with services.locks.get(guild_id):
            await services.boards.update_guild(guild_id)
    except Exception:
        log.exception("Board refresh after a leaderboard change failed for guild %s", guild_id)


async def lb_ban(services: "Services", guild_id: int, actor: Actor, target_id: int, reason: str | None) -> str:
    """Hide a user from every board, /stats and reset winners; stop counting their messages.

    Their stored counts are kept (hidden at query time), so :func:`lb_unban`
    restores them; messages sent while banned are never counted.
    """
    await check_target(
        services, guild_id, actor, target_id, verb="ban from the leaderboard", bot_acts=False, require_member=False
    )
    added = await services.repo.add_lb_ban(
        guild_id, target_id, moderator_id=actor.user_id, reason=reason, now=services.clock.now()
    )
    if not added:
        raise ModError(f"<@{target_id}> is already banned from the leaderboard.")
    services.lb_bans.set_banned(guild_id, target_id, True)
    case = await record_case(services, guild_id, "lbban", target_id=target_id, actor=actor, reason=reason)
    await refresh_boards(services, guild_id)
    return (
        f"<@{target_id}> is banned from the leaderboard: hidden from the boards and /stats, can't win, and new "
        f"messages don't count. Their earlier counts are kept for if you lift it.{case_note(case)}"
    )


async def lb_unban(services: "Services", guild_id: int, actor: Actor, target_id: int, reason: str | None) -> str:
    if not await services.repo.remove_lb_ban(guild_id, target_id):
        raise ModError(f"<@{target_id}> isn't banned from the leaderboard.")
    services.lb_bans.set_banned(guild_id, target_id, False)
    case = await record_case(services, guild_id, "lbunban", target_id=target_id, actor=actor, reason=reason)
    await refresh_boards(services, guild_id)
    return (
        f"<@{target_id}> is back on the leaderboard with their earlier counts. Messages sent while they were "
        f"banned don't count.{case_note(case)}"
    )


async def list_lb_bans(services: "Services", guild_id: int) -> str:
    bans = await services.repo.list_lb_bans(guild_id)
    if not bans:
        return "Nobody is banned from the leaderboard."
    lines = [
        f"• <@{b.user_id}> (`{b.user_id}`) · {_ts(b.created_at, 'd')} · by <@{b.moderator_id}>"
        + (f" · {discord.utils.escape_markdown(clip(b.reason, 120))}" if b.reason else "")
        for b in bans
    ]
    return fit_lines(f"Banned from the leaderboard ({len(bans)}):", lines)


RESET_PERIODS = ("daily", "weekly", "monthly", "alltime")


def reset_scope(period: str, now: datetime) -> tuple[str | None, str | None, str]:
    """``(start_day, end_day, label)`` of the *current* period (``None`` days = all history)."""
    if period == "alltime":
        return None, None, "all-time"
    if period not in RESET_PERIODS:
        raise ModError("Pick daily, weekly, monthly or alltime.")
    current = period_containing(PeriodType(period), now)
    start_day, end_day = day_range(current.start, current.end)
    return start_day, end_day, f"{period} ({current.key})"


async def check_reset_counts(services: "Services", guild_id: int, actor: Actor, target_id: int, period: str) -> str:
    """Validate a /resetcounts request before asking for confirmation; returns the scope label."""
    _, _, label = reset_scope(period, services.clock.now())
    await check_target(
        services, guild_id, actor, target_id, verb="reset the counts of", bot_acts=False, require_member=False
    )
    return label


async def reset_counts(services: "Services", guild_id: int, actor: Actor, target_id: int, period: str) -> str:
    """Delete a user's counted messages for the current period (or all history).

    The buffer is flushed first, under the guild lock (so no reset computes a
    winner halfway): otherwise counts still in memory would be written back
    after the delete and the reset would partly undo itself.
    """
    start_day, end_day, label = reset_scope(period, services.clock.now())
    await check_target(
        services, guild_id, actor, target_id, verb="reset the counts of", bot_acts=False, require_member=False
    )
    async with services.locks.get(guild_id):
        if not await services.buffer.flush():
            raise ModError(
                "Recent messages couldn't be saved right now, so nothing was reset (they would have come back). "
                "Try again in a minute."
            )
        deleted = await services.repo.delete_counts(guild_id, target_id, start_day, end_day)
    case = await record_case(
        services, guild_id, "resetcounts", target_id=target_id, actor=actor, reason=None,
        extra={"period": label, "deleted_days": deleted},
    )
    await refresh_boards(services, guild_id)
    if not deleted:
        return f"<@{target_id}> had no counted messages in the {label} range; nothing to reset.{case_note(case)}"
    return f"Reset <@{target_id}>'s {label} count ({deleted} day(s) of counts removed).{case_note(case)}"


async def set_count_cooldown(services: "Services", guild_id: int, seconds: int) -> str:
    if not 0 <= seconds <= 60:
        raise ModError("The cooldown must be between 0 and 60 seconds.")
    await services.repo.set_count_cooldown(guild_id, seconds, services.clock.now())
    services.cooldowns.set_cooldown(guild_id, seconds)
    log.info("Count cooldown for guild %s set to %ss", guild_id, seconds)
    if seconds == 0:
        return "Count cooldown turned off: every eligible message counts."
    return (
        f"Count cooldown set to {seconds}s: a message counts only if it comes at least {seconds}s after the "
        "author's previous counted message."
    )


# --------------------------------------------------------- warn escalation


async def add_warn_threshold(services: "Services", guild_id: int, count: int, duration: timedelta) -> str:
    if not 1 <= count <= 100:
        raise ModError("The warning count must be between 1 and 100.")
    if duration > MAX_TIMEOUT:
        raise ModError("An automatic timeout can be at most 28 days (Discord's limit).")
    seconds = int(duration.total_seconds())
    await services.repo.set_warn_threshold(guild_id, count, seconds)
    log.info("Warn threshold set: guild=%s count=%s timeout=%ss", guild_id, count, seconds)
    return f"At exactly {count} active warning(s), members are timed out for {format_duration(seconds)}."


async def remove_warn_threshold(services: "Services", guild_id: int, count: int) -> str:
    if not await services.repo.remove_warn_threshold(guild_id, count):
        raise ModError(f"There is no rule for {count} warning(s).")
    return f"Removed the rule for {count} warning(s)."


async def list_warn_thresholds(services: "Services", guild_id: int) -> str:
    rules = await services.repo.list_warn_thresholds(guild_id)
    if not rules:
        return "No automatic timeouts are configured. Add one with `/warnconfig add`."
    lines = [f"• {r.warn_count} warning(s) → timeout {format_duration(r.timeout_seconds)}" for r in rules]
    return fit_lines("Automatic timeouts (when a member reaches exactly this many active warnings):", lines)


# -------------------------------------------------------------------- cases


def case_link(guild_id: int, case: ModCase) -> str | None:
    if case.modlog_channel_id is None or case.modlog_message_id is None:
        return None
    return f"https://discord.com/channels/{guild_id}/{case.modlog_channel_id}/{case.modlog_message_id}"


async def view_case(services: "Services", guild_id: int, case_id: int) -> discord.Embed:
    case = await services.repo.get_case(guild_id, case_id)
    if case is None:
        raise ModError(f"There is no case #{case_id} in this server.")
    embed = case_embed(case)
    if case.action == "warn":
        embed.add_field(name="Status", value="Active" if case.active else "Cleared", inline=True)
    link = case_link(guild_id, case)
    if link:
        embed.add_field(name="Mod log", value=f"[Jump to entry]({link})", inline=True)
    return embed


async def edit_case_reason(services: "Services", guild_id: int, case_id: int, reason: str) -> str:
    """Change a case's reason and re-render its mod-log entry (best effort)."""
    case = await services.repo.update_case_reason(guild_id, case_id, reason)
    if case is None:
        raise ModError(f"There is no case #{case_id} in this server.")
    if case.modlog_channel_id is None or case.modlog_message_id is None:
        return f"Updated the reason of case #{case_id}. (It has no mod-log entry to edit.)"
    try:
        await services.ops.edit_message(case.modlog_channel_id, case.modlog_message_id, embed=case_embed(case))
    except OpError as exc:
        log.warning("Could not edit the mod-log entry of case #%s in guild %s: %s", case_id, guild_id, exc.describe())
        return f"Updated the reason of case #{case_id}, but its mod-log entry couldn't be edited ({exc.kind})."
    return f"Updated the reason of case #{case_id} and its mod-log entry."


async def set_modlog(services: "Services", guild_id: int, channel_id: int | None) -> str:
    await services.repo.set_modlog_channel(guild_id, channel_id, services.clock.now())
    log.info("Mod log for guild %s set to %s", guild_id, channel_id)
    if channel_id is None:
        return "Mod log turned off. Cases are still recorded."
    return f"Moderation actions will be logged in <#{channel_id}>."

