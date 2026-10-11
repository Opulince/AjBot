"""Embeds for leaderboards, pagination pages, announcements and stats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

import discord

from bot.periods import ALLTIME, period_containing, unix_timestamp
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
PREVIOUS_LABELS = {
    "daily": "Previous Member of the Day",
    "weekly": "Previous Member of the Week",
    "monthly": "Previous Member of the Month",
}
# One accent per board, all from the same tonal family so they read as a set.
COLORS = {
    "daily": 0xF97316,  # sunrise orange
    "weekly": 0x3B82F6,  # blue
    "monthly": 0x8B5CF6,  # violet
    "alltime": 0xEAB308,  # hall-of-fame gold
}
BOARD_ICONS = {"daily": "☀️", "weekly": "📅", "monthly": "🌙", "alltime": "👑"}
BOARD_AUTHOR = "Community Activity Championship"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
PERIOD_NOUNS = {"daily": "day", "weekly": "week", "monthly": "month"}
EMPTY_WHEN = {"daily": "today", "weekly": "this week", "monthly": "this month"}
COUNT_EMOJI = "💬"  # shown after each count; a custom emoji works too, e.g. "<:chat:123456789012345678>"

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


def member_label(user_id: int, name: str | None, left: bool) -> str:
    """Mention plus escaped plain-text name (readable even if the mention cannot resolve)."""
    return f"<@{user_id}> · {safe_name(name)}{' (left)' if left else ''}"


def entry_line(entry: RankEntry) -> str:
    """Row used by the ranks 11–100 pages."""
    return f"**{entry.rank}.** {member_label(entry.user_id, entry.name, entry.left)} — **{plural(entry.count)}**"


def board_line(entry: RankEntry) -> str:
    """Row on a board: medal and bold count for the podium, a compact rank badge below it."""
    who = member_label(entry.user_id, entry.name, entry.left)
    medal = MEDALS.get(entry.rank)
    if medal is not None:
        return f"{medal} {who} — **{plural(entry.count)}** {COUNT_EMOJI}"
    return f"`#{entry.rank}` {who} — {plural(entry.count)} {COUNT_EMOJI}"


def scope_line(board_type: str, now: datetime) -> str:
    """What the board is counting right now.

    Period dates are spelled out as plain UTC text: a Discord timestamp would
    render a UTC midnight as the previous day in western time zones.
    """
    if board_type == ALLTIME:
        return "Standings across **all time** · never resets"
    period = period_containing(board_type, now)
    first = period.start
    last = period.end - timedelta(days=1)
    if board_type == "daily":
        span = f"{first:%a} {first.day} {first:%b %Y}"
    elif board_type == "weekly":
        if first.year != last.year:
            span = f"{first.day} {first:%b %Y} – {last.day} {last:%b %Y}"
        elif first.month != last.month:
            span = f"{first.day} {first:%b} – {last.day} {last:%b %Y}"
        else:
            span = f"{first.day}–{last.day} {last:%b %Y}"
    else:
        span = f"{first:%B %Y}"
    return f"Standings for **{span}** (UTC)"


def board_description(board_type: str, entries: Sequence[RankEntry], now: datetime) -> str:
    top = list(entries[:TOP_SHOWN])
    if not top:
        when = EMPTY_WHEN.get(board_type)
        suffix = f" {when}" if when else ""
        return f"{scope_line(board_type, now)}\n\n*The podium is open — no messages counted yet{suffix}.*"
    podium = [board_line(e) for e in top if e.rank in MEDALS]
    rest = [board_line(e) for e in top if e.rank not in MEDALS]
    blocks = [scope_line(board_type, now), "\n".join(podium), "\n".join(rest)]
    return "\n\n".join(block for block in blocks if block)


@dataclass(frozen=True)
class PreviousWinner:
    """Outcome of the period immediately before the current one.

    ``pending`` means that period has ended but its result is not decided yet;
    no winner is shown for it in the meantime.
    """

    period_key: str
    user_id: int | None
    name: str | None
    count: int | None
    left: bool = False
    pending: bool = False

    @property
    def empty(self) -> bool:
        return self.user_id is None


def format_previous(board_type: str, previous: PreviousWinner | None) -> str:
    if previous is None:
        return f"None yet — this is the first {PERIOD_NOUNS[board_type]} being tracked."
    label = period_label(board_type, previous.period_key, is_test=False)
    if previous.pending:
        return f"*Tallying results…*\n{label}"
    if previous.empty:
        return f"No eligible winner\n{label}"
    assert previous.user_id is not None
    return (
        f"🏆 {member_label(previous.user_id, previous.name, previous.left)}\n"
        f"**{plural(previous.count or 0)}** · {label}"
    )


def board_link(guild_id: int, board_type: str, generation: int) -> str:
    """Author-line link carrying the machine-readable recovery marker.

    Recovery reads it back from the embed (see ``HistoryMessage.marker_texts``),
    so no visible footer is needed. It points at the server itself, so a click
    just opens the server. Boards posted before this change keep the marker in
    their footer, which recovery still reads.
    """
    return f"https://discord.com/channels/{guild_id}#{board_marker(guild_id, board_type, generation)}"


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
    embed = discord.Embed(
        title=f"{BOARD_ICONS[board_type]} {BOARD_TITLES[board_type]}",
        description=board_description(board_type, entries, now),
        color=COLORS[board_type],
        timestamp=now,
    )
    embed.set_author(name=BOARD_AUTHOR, url=board_link(guild_id, board_type, generation))
    if board_type != ALLTIME:
        embed.add_field(name=PREVIOUS_LABELS[board_type], value=format_previous(board_type, previous), inline=True)
        if next_reset is not None:
            ts = unix_timestamp(next_reset)
            embed.add_field(name="Next reset", value=f"<t:{ts}:R>\n<t:{ts}:f>", inline=True)
    if banner_url:
        embed.set_image(url=banner_url)
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
