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
from bot.periods import unix_timestamp
from bot.ui.modlog import build_case_embed, build_info_embed, clip

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


def case_embed(case: ModCase, *, note: str | None = None) -> discord.Embed:
    return build_case_embed(
        case_id=case.case_id,
        action=case.action,
        target_id=case.target_id,
        moderator_id=case.moderator_id,
        reason=case.reason,
        duration_seconds=case.duration_seconds,
        created_at=case.created_at,
        extra=case.extra,
        note=note,
    )


async def log_case(services: "Services", case: ModCase, *, note: str | None = None) -> None:
    posted = await post_modlog(services, case.guild_id, case_embed(case, note=note))
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
    note: str | None = None,
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
            duration_seconds=duration_seconds, created_at=now, extra=extra, note=note,
        )
        await post_modlog(services, guild_id, embed)
        return None
    await log_case(services, case, note=note)
    return case


# ------------------------------------------------------------------ actions


async def apply_timeout(
    services: "Services", guild_id: int, actor: Actor, target_id: int, duration: timedelta, reason: str | None,
    *, note: str | None = None,
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
        duration_seconds=int(duration.total_seconds()), note=note,
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
    services: "Services", guild_id: int, guild_name: str, actor: Actor, target_id: int, reason: str
) -> str:
    await check_target(services, guild_id, actor, target_id, verb="warn", bot_acts=False)
    try:
        case, active = await services.repo.create_warning(
            guild_id, target_id, moderator_id=actor.user_id, reason=reason, now=services.clock.now()
        )
    except Exception as exc:
        log.exception("Could not save a warning for %s in guild %s", target_id, guild_id)
        raise ModError("The warning couldn't be saved, so nothing was recorded. The error was logged.") from exc
    server = discord.utils.escape_markdown(guild_name)
    delivered = await _notify(services, target_id, clip(f"You received a warning in **{server}**.\nReason: {reason}", 2000))
    dm_note = None if delivered else "DM not delivered (closed DMs or an error)."
    await log_case(services, case, note=dm_note)
    dm = "They were notified by DM." if delivered else "I couldn't DM them (closed DMs), but the warning is recorded."
    return f"Warned <@{target_id}> (case #{case.case_id}). Active warnings: **{active}**. {dm}"


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
) -> str:
    if not 0 <= delete_message_days <= 7:
        raise ModError("Messages can be deleted for 0 to 7 days.")
    await check_target(services, guild_id, actor, target_id, verb="ban", require_member=False)
    await run_discord_action(
        lambda: services.ops.ban_user(
            guild_id, target_id, delete_message_seconds=delete_message_days * 86400, reason=actor.audit(reason)
        ),
        verb="ban", target_id=target_id,
    )
    extra = {"delete_message_days": delete_message_days} if delete_message_days else None
    case = await record_case(services, guild_id, "ban", target_id=target_id, actor=actor, reason=reason, extra=extra)
    return f"Banned <@{target_id}> permanently.{case_note(case)}"


async def unban(services: "Services", guild_id: int, actor: Actor, user_id: int, reason: str | None) -> str:
    await run_discord_action(
        lambda: services.ops.unban_user(guild_id, user_id, reason=actor.audit(reason)),
        verb="unban", target_id=user_id, not_found=f"<@{user_id}> (`{user_id}`) isn't banned.",
    )
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


async def set_modlog(services: "Services", guild_id: int, channel_id: int | None) -> str:
    await services.repo.set_modlog_channel(guild_id, channel_id, services.clock.now())
    log.info("Mod log for guild %s set to %s", guild_id, channel_id)
    if channel_id is None:
        return "Mod log turned off. Cases are still recorded."
    return f"Moderation actions will be logged in <#{channel_id}>."

