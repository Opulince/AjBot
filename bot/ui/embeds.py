"""Embeds for leaderboards, pagination pages, announcements and stats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

import discord

from bot.periods import ALLTIME, unix_timestamp
from bot.ranking import RankEntry, Standing

TOP_SHOWN = 10
NAME_LIMIT = 32

BOARD_TITLES = {
    "daily": "Daily Leaderboard",
    "weekly": "Weekly Leaderboard",
    "monthly": "Monthly Leaderboard",
    "alltime": "All-Time Leaderboard",
}
WINNER_TITLES = {
    "daily": "Member of the Day",
    "weekly": "Member of the Week",
    "monthly": "Member of the Month",
}
COLORS = {
    "daily": 0x5865F2,
    "weekly": 0x57F287,
    "monthly": 0xFEE75C,
    "alltime": 0xEB459E,
}

BOARD_MARKER_RE = re.compile(r"board:(\d+):(daily|weekly|monthly|alltime):(\d+)")


def board_marker(guild_id: int, board_type: str, generation: int) -> str:
    return f"board:{guild_id}:{board_type}:{generation}"


def reset_marker(guild_id: int, period_type: str, period_key: str) -> str:
    return f"reset:{guild_id}:{period_type}:{period_key}"


def parse_board_marker(text: str) -> tuple[int, str, int] | None:
    match = BOARD_MARKER_RE.search(text)
    if match is None:
        return None
    return int(match[1]), match[2], int(match[3])


def safe_name(name: str | None) -> str:
    """Plain-text display name safe to embed: trimmed, markdown and mentions escaped."""
    text = (name or "").strip() or "Unknown user"
    if len(text) > NAME_LIMIT:
        text = text[: NAME_LIMIT - 1] + "…"
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def plural(count: int, word: str = "message") -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def entry_line(entry: RankEntry) -> str:
    left = " (left)" if entry.left else ""
    return f"**{entry.rank}.** <@{entry.user_id}> · {safe_name(entry.name)}{left} — **{plural(entry.count)}**"


@dataclass(frozen=True)
class PreviousWinner:
    period_key: str
    user_id: int | None
    name: str | None
    count: int | None
    left: bool = False

    @property
    def empty(self) -> bool:
        return self.user_id is None


def format_previous(previous: PreviousWinner | None) -> str:
    if previous is None:
        return "None yet"
    if previous.empty:
        return f"No eligible winner\n({previous.period_key})"
    left = " (left)" if previous.left else ""
    return (
        f"<@{previous.user_id}> · {safe_name(previous.name)}{left} — {plural(previous.count or 0)}\n"
        f"({previous.period_key})"
    )


def build_board_embed(
    *,
    guild_id: int,
    board_type: str,
    generation: int,
    entries: Sequence[RankEntry],
    previous: PreviousWinner | None,
    next_reset: datetime | None,
    banner_url: str | None,
    now: datetime,
) -> discord.Embed:
    embed = discord.Embed(title=BOARD_TITLES[board_type], color=COLORS[board_type], timestamp=now)
    top = list(entries[:TOP_SHOWN])
    if top:
        embed.description = "\n".join(entry_line(e) for e in top)
    elif board_type == ALLTIME:
        embed.description = "No messages counted yet."
    else:
        embed.description = "No messages yet this period."
    if board_type != ALLTIME:
        embed.add_field(name="Previous winner", value=format_previous(previous), inline=True)
        if next_reset is not None:
            ts = unix_timestamp(next_reset)
            embed.add_field(name="Next reset", value=f"<t:{ts}:R>\n<t:{ts}:f>", inline=True)
    if banner_url:
        embed.set_image(url=banner_url)
    # The footer carries the machine-readable recovery marker next to the
    # visible "last updated" label; the embed timestamp renders the time.
    embed.set_footer(text=f"Last updated · {board_marker(guild_id, board_type, generation)}")
    return embed


def build_page_embed(
    *,
    board_type: str,
    entries: Sequence[RankEntry],
    page: int,
    pages: int,
    now: datetime,
) -> discord.Embed:
    embed = discord.Embed(color=COLORS[board_type], timestamp=now)
    if entries:
        embed.title = f"{BOARD_TITLES[board_type]} — ranks {entries[0].rank}–{entries[-1].rank}"
        embed.description = "\n".join(entry_line(e) for e in entries)
    else:
        embed.title = BOARD_TITLES[board_type]
        embed.description = "Nobody is ranked beyond the top 10 yet."
    embed.set_footer(text=f"Page {page}/{max(pages, 1)} · ranks 11–100")
    return embed


def period_label(period_type: str, period_key: str, is_test: bool) -> str:
    if is_test:
        return "the current period so far"
    if period_type == "daily":
        return f"{period_key} (UTC)"
    if period_type == "weekly":
        return f"ISO week {period_key}"
    return period_key


def build_announcement(
    *,
    guild_id: int,
    period_type: str,
    period_key: str,
    winner_id: int,
    winner_name: str | None,
    count: int,
    is_test: bool,
) -> tuple[str, discord.Embed]:
    title = WINNER_TITLES[period_type]
    prefix = "[TEST] " if is_test else ""
    content = f"{prefix}**{title}:** <@{winner_id}> — {plural(count)}"
    embed = discord.Embed(
        title=f"{prefix}{title}",
        color=COLORS[period_type],
        description=(
            f"<@{winner_id}> · {safe_name(winner_name)} sent **{plural(count)}** during "
            f"{period_label(period_type, period_key, is_test)}."
        ),
    )
    if is_test:
        embed.add_field(
            name="Forced test reset",
            value="Triggered with /forcereset. This is a test of the reset flow, not an official result.",
            inline=False,
        )
    embed.set_footer(text=reset_marker(guild_id, period_type, period_key))
    return content, embed


STATS_LABELS = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "alltime": "All-Time"}


def build_stats_embed(*, user_id: int, name: str, standings: Mapping[str, Standing], now: datetime) -> discord.Embed:
    lines = []
    for board_type, label in STATS_LABELS.items():
        standing = standings[board_type]
        if standing.rank is None:
            lines.append(f"**{label}:** {plural(standing.count)} — unranked")
        else:
            lines.append(f"**{label}:** {plural(standing.count)} — rank #{standing.rank:,}")
    embed = discord.Embed(
        title=f"Stats for {safe_name(name)}",
        description=f"<@{user_id}>\n\n" + "\n".join(lines),
        color=0x5865F2,
        timestamp=now,
    )
    embed.set_footer(text="Counts may lag a few seconds behind live messages")
    return embed
