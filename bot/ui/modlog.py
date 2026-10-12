"""Mod-log embeds: one per moderation action (also used by ``/case view``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

import discord

from bot.periods import unix_timestamp

ACTION_LABELS = {
    "warn": "Warn",
    "timeout": "Timeout",
    "untimeout": "Timeout removed",
    "kick": "Kick",
    "ban": "Ban",
    "unban": "Unban",
    "purge": "Purge",
    "lbban": "Leaderboard ban",
    "lbunban": "Leaderboard unban",
    "resetcounts": "Counts reset",
}
ACTION_COLORS = {
    "warn": 0xEAB308,
    "timeout": 0xF97316,
    "untimeout": 0x22C55E,
    "kick": 0xEF4444,
    "ban": 0xB91C1C,
    "unban": 0x22C55E,
    "purge": 0x64748B,
    "lbban": 0x8B5CF6,
    "lbunban": 0x22C55E,
    "resetcounts": 0x8B5CF6,
}
INFO_COLOR = 0x64748B
FIELD_LIMIT = 1024
# Actions whose duration ends at a known moment worth showing.
EXPIRING_ACTIONS = frozenset({"timeout", "ban"})


def format_duration(seconds: int) -> str:
    """Compact human duration: ``90`` -> ``1m 30s``, ``86400`` -> ``1d``."""
    remaining = max(int(seconds), 0)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        value, remaining = divmod(remaining, size)
        if value:
            parts.append(f"{value}{unit}")
    return " ".join(parts) or "0s"


def clip(text: str, limit: int = FIELD_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_case_embed(
    *,
    case_id: int | None,
    action: str,
    target_id: int | None,
    moderator_id: int,
    reason: str | None,
    duration_seconds: int | None,
    created_at: datetime,
    extra: Mapping[str, Any] | None = None,
) -> discord.Embed:
    """The mod-log entry for a case. ``case_id`` is ``None`` if the case record could not be saved.

    Everything shown comes from the stored case, so re-rendering it later (``/case reason``)
    reproduces the same entry.
    """
    extra = extra or {}
    label = ACTION_LABELS.get(action, action)
    title = f"{label} · Case #{case_id}" if case_id is not None else f"{label} · case not saved"
    embed = discord.Embed(title=title, color=ACTION_COLORS.get(action, INFO_COLOR), timestamp=created_at)
    if target_id is not None:
        embed.add_field(name="User", value=f"<@{target_id}> (`{target_id}`)", inline=True)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    if duration_seconds:
        value = format_duration(duration_seconds)
        if action in EXPIRING_ACTIONS:
            ts = unix_timestamp(created_at + timedelta(seconds=duration_seconds))
            value += f" · ends <t:{ts}:f> (<t:{ts}:R>)"
        embed.add_field(name="Duration", value=value, inline=False)
    elif action == "ban":
        embed.add_field(name="Duration", value="Permanent", inline=False)
    if action == "purge" and "channel_id" in extra:
        embed.add_field(
            name="Messages",
            value=f"{extra.get('deleted', 0)} deleted in <#{extra['channel_id']}> (newest {extra.get('scanned', '?')} scanned)",
            inline=False,
        )
    if action == "ban" and extra.get("delete_message_days"):
        embed.add_field(name="Deleted messages", value=f"Last {extra['delete_message_days']} day(s)", inline=True)
    if extra.get("period"):
        embed.add_field(name="Period", value=str(extra["period"]), inline=True)
    if extra.get("ban_case"):
        embed.add_field(name="Ban case", value=f"#{extra['ban_case']}", inline=True)
    if extra.get("warn_case"):
        embed.add_field(name="Triggered by", value=f"Warning #{extra['warn_case']}", inline=True)
    deleted = extra.get("deleted_message")
    if isinstance(deleted, Mapping):
        excerpt = deleted.get("excerpt")
        text = f"In <#{deleted.get('channel_id')}>" + (f": {excerpt}" if excerpt else " (no text)")
        embed.add_field(name="Deleted message", value=clip(text), inline=False)
    embed.add_field(name="Reason", value=clip(reason) if reason else "*No reason given*", inline=False)
    if action == "warn" and "dm" in extra:
        embed.add_field(
            name="DM", value="Delivered" if extra["dm"] else "Not delivered (closed DMs or an error)", inline=True
        )
    return embed


def build_info_embed(title: str, description: str, *, moderator_id: int, created_at: datetime) -> discord.Embed:
    """A mod-log entry that is not a case (e.g. warnings cleared)."""
    embed = discord.Embed(title=title, description=clip(description, 4096), color=INFO_COLOR, timestamp=created_at)
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    return embed
