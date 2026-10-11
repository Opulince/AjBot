"""Pure rendering of the leaderboard embeds: layout, labels, empty states, limits and the recovery marker."""

from __future__ import annotations

from datetime import datetime

import discord
import pytest

from bot.clock import UTC
from bot.discord_ops import HistoryMessage
from bot.periods import BOARD_ORDER, unix_timestamp
from bot.ranking import RankEntry
from bot.ui.embeds import (
    BOARD_TITLES,
    COLORS,
    PreviousWinner,
    board_marker,
    build_announcement,
    build_board_embed,
    build_page_embed,
    entry_line,
    parse_board_marker,
)

GUILD = 1
NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)  # Tuesday, ISO week 2026-W11
NEXT_RESET = datetime(2026, 3, 11, tzinfo=UTC)
LABELS = {
    "daily": "Previous Member of the Day",
    "weekly": "Previous Member of the Week",
    "monthly": "Previous Member of the Month",
}
ICONS = {"daily": "☀️", "weekly": "📅", "monthly": "🌙", "alltime": "👑"}


def entry(rank: int, *, user_id: int | None = None, count: int | None = None, name: str | None = None,
          left: bool = False) -> RankEntry:
    return RankEntry(
        rank=rank,
        user_id=user_id if user_id is not None else 100 + rank,
        count=count if count is not None else 100 - rank,
        milestone_id=rank,
        display_name=name if name is not None else f"User{rank}",
        username=None,
        left=left,
    )


def render(board_type: str, entries: list[RankEntry] | None = None, *, previous: PreviousWinner | None = None,
           next_reset: datetime | None = NEXT_RESET, banner: str | None = None, now: datetime = NOW,
           generation: int = 7, guild_id: int = GUILD) -> discord.Embed:
    return build_board_embed(
        guild_id=guild_id,
        board_type=board_type,
        generation=generation,
        entries=entries or [],
        previous=previous,
        next_reset=next_reset,
        banner_url=banner,
        now=now,
    )


def fields(embed: discord.Embed) -> dict[str, str]:
    return {f.name or "": f.value or "" for f in embed.fields}


# ---------------------------------------------------------------- identity


@pytest.mark.parametrize("board_type", BOARD_ORDER)
def test_each_board_has_a_distinct_title_accent_and_shared_author(board_type: str) -> None:
    embed = render(board_type)
    assert embed.title == f"{ICONS[board_type]} {BOARD_TITLES[board_type]}"
    assert embed.colour is not None and embed.colour.value == COLORS[board_type]
    assert embed.author.name == "Community Activity Championship"
    assert len(set(COLORS.values())) == len(BOARD_ORDER)


@pytest.mark.parametrize("board_type", ["daily", "weekly", "monthly"])
def test_period_boards_use_their_exact_previous_winner_label(board_type: str) -> None:
    embed = render(board_type, previous=PreviousWinner("2026-03-09", 11, "Alice", 4))
    assert [f.name for f in embed.fields] == [LABELS[board_type], "Next reset"]
    assert all(f.inline for f in embed.fields)
    assert "Previous winner" not in str(embed.to_dict())


def test_alltime_board_has_no_previous_winner_or_reset_countdown() -> None:
    # Even if a caller passed period data, the all-time board must not show it.
    embed = render("alltime", [entry(1)], previous=PreviousWinner("2026-03-09", 11, "Alice", 4), next_reset=NEXT_RESET)
    assert embed.fields == []
    assert "<t:" not in (embed.description or "")
    assert (embed.description or "").startswith("Standings across **all time** · never resets")


# ------------------------------------------------------------------ rows


def test_podium_gets_medals_and_ranks_four_to_ten_stay_compact() -> None:
    embed = render("daily", [entry(r) for r in range(1, 13)])
    blocks = (embed.description or "").split("\n\n")
    assert blocks[0] == "Standings for **Tue 10 Mar 2026** (UTC)"
    assert blocks[1].split("\n") == [
        "🥇 <@101> · User1 — **99 messages** 💬",
        "🥈 <@102> · User2 — **98 messages** 💬",
        "🥉 <@103> · User3 — **97 messages** 💬",
    ]
    rest = blocks[2].split("\n")
    assert rest[0] == "`#4` <@104> · User4 — 96 messages 💬"
    assert rest[-1] == "`#10` <@110> · User10 — 90 messages 💬"
    assert len(rest) == 7 and len(blocks) == 3
    assert "<@111>" not in (embed.description or "") and "<@112>" not in (embed.description or "")  # top 10 only


def test_short_board_has_no_empty_compact_block() -> None:
    embed = render("weekly", [entry(1), entry(2)])
    blocks = (embed.description or "").split("\n\n")
    assert len(blocks) == 2 and blocks[1].count("\n") == 1
    assert "`#" not in (embed.description or "")


def test_counts_use_thousands_separators_and_singular() -> None:
    embed = render("alltime", [entry(1, count=12_345), entry(4, count=1)])
    desc = embed.description or ""
    assert "**12,345 messages** 💬" in desc
    assert desc.endswith("— 1 message 💬")


def test_departed_members_keep_their_label_on_board_rows() -> None:
    embed = render("monthly", [entry(1, name="Alice", left=True), entry(5, name="Bob", left=True)])
    desc = embed.description or ""
    assert "🥇 <@101> · Alice (left) — **99 messages** 💬" in desc
    assert "`#5` <@105> · Bob (left) — 95 messages 💬" in desc


def test_names_are_escaped_and_truncated_with_the_shared_helper() -> None:
    hostile = "@everyone **bold** `code` " + "x" * 40
    embed = render("daily", [entry(1, name=hostile)])
    desc = embed.description or ""
    assert "@everyone" not in desc and "@\u200beveryone" in desc  # mention neutralised
    assert "\\*\\*bold\\*\\*" in desc and "\\`code\\`" in desc  # markdown escaped
    assert "…" in desc and "x" * 20 not in desc  # truncated to the name limit


# ----------------------------------------------------------- empty states


@pytest.mark.parametrize(
    ("board_type", "scope", "when"),
    [
        ("daily", "Standings for **Tue 10 Mar 2026** (UTC)", " today"),
        ("weekly", "Standings for **9–15 Mar 2026** (UTC)", " this week"),
        ("monthly", "Standings for **March 2026** (UTC)", " this month"),
        ("alltime", "Standings across **all time** · never resets", ""),
    ],
)
def test_empty_boards_render_a_deliberate_state(board_type: str, scope: str, when: str) -> None:
    embed = render(board_type, [])
    assert embed.description == f"{scope}\n\n*The podium is open — no messages counted yet{when}.*"


@pytest.mark.parametrize(
    ("now", "scope"),
    [
        (datetime(2026, 4, 1, 9, tzinfo=UTC), "**30 Mar – 5 Apr 2026**"),  # week spanning two months
        (datetime(2025, 12, 31, 9, tzinfo=UTC), "**29 Dec 2025 – 4 Jan 2026**"),  # ISO week 2026-W01
    ],
)
def test_weekly_scope_spells_out_month_and_year_changes(now: datetime, scope: str) -> None:
    assert scope in (render("weekly", now=now).description or "")


# -------------------------------------------------------- previous field


@pytest.mark.parametrize(
    ("board_type", "key", "label"),
    [("daily", "2026-03-09", "2026-03-09 (UTC)"), ("weekly", "2026-W10", "ISO week 2026-W10"), ("monthly", "2026-02", "2026-02")],
)
def test_previous_winner_states(board_type: str, key: str, label: str) -> None:
    def value(previous: PreviousWinner | None) -> str:
        return fields(render(board_type, previous=previous))[LABELS[board_type]]

    assert value(PreviousWinner(key, 11, "Alice", 1234)) == f"🏆 <@11> · Alice\n**1,234 messages** · {label}"
    assert value(PreviousWinner(key, 11, "Alice", 1, left=True)) == f"🏆 <@11> · Alice (left)\n**1 message** · {label}"
    assert value(PreviousWinner(key, 11, None, 2)).startswith("🏆 <@11> · Unknown user\n")
    assert value(PreviousWinner(key, None, None, None)) == f"No eligible winner\n{label}"
    assert value(PreviousWinner(key, None, None, None, pending=True)) == f"*Tallying results…*\n{label}"
    noun = {"daily": "day", "weekly": "week", "monthly": "month"}[board_type]
    assert value(None) == f"None yet — this is the first {noun} being tracked."


def test_reset_field_uses_relative_and_absolute_discord_timestamps() -> None:
    embed = render("daily")
    ts = unix_timestamp(NEXT_RESET)
    assert fields(embed)["Next reset"] == f"<t:{ts}:R>\n<t:{ts}:f>"
    assert embed.timestamp == NOW
    assert [f.name for f in render("daily", next_reset=None).fields] == [LABELS["daily"]]


# ---------------------------------------------------- footer, banner, limits


@pytest.mark.parametrize("board_type", BOARD_ORDER)
def test_recovery_marker_lives_in_the_author_link_not_a_visible_footer(board_type: str) -> None:
    embed = render(board_type, generation=7)
    assert embed.footer.text is None  # nothing visible below the board but the timestamp
    assert embed.timestamp == NOW
    link = embed.author.url or ""
    assert link == f"https://discord.com/channels/{GUILD}#board:{GUILD}:{board_type}:7"
    assert parse_board_marker(link) == (GUILD, board_type, 7)
    msg = HistoryMessage(1, 2, "", (), NOW, (link,))
    assert msg.contains_marker(board_marker(GUILD, board_type, 7))
    assert not msg.contains_marker(board_marker(GUILD, board_type, 70))  # no prefix matches
    longer = HistoryMessage(1, 2, "", (), NOW, (render(board_type, generation=70).author.url or "",))
    assert not longer.contains_marker(board_marker(GUILD, board_type, 7))
    # Discord may percent-encode the link; the marker is still found.
    encoded = HistoryMessage(1, 2, "", (), NOW, (link.replace(":", "%3A").replace("https%3A", "https:"),))
    assert encoded.contains_marker(board_marker(GUILD, board_type, 7))


def test_boards_posted_with_the_old_footer_marker_are_still_recognised() -> None:
    old = HistoryMessage(1, 2, "", ("Last updated · board:1:daily:3",), NOW)
    assert old.contains_marker(board_marker(1, "daily", 3))


def test_banner_is_optional() -> None:
    assert render("daily", banner="https://cdn.example.com/b.png").image.url == "https://cdn.example.com/b.png"
    assert render("daily").image.url is None


@pytest.mark.parametrize("board_type", BOARD_ORDER)
def test_worst_case_board_stays_within_discord_limits(board_type: str) -> None:
    hostile = "*_~`|>@" * 10
    big_id = 2**63 - 1
    entries = [entry(r, user_id=big_id - r, count=999_999_999 - r, name=hostile, left=True) for r in range(1, 11)]
    previous = PreviousWinner("2026-W10" if board_type == "weekly" else "2026-03-09", big_id, hostile, 999_999_999, left=True)
    embed = render(board_type, entries, previous=previous, banner="https://cdn.example.com/b.png",
                   generation=2**31, guild_id=big_id)
    assert len(embed) <= 6000
    assert len(embed.title or "") <= 256 and len(embed.author.name or "") <= 256
    assert len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25
    assert all(len(f.name or "") <= 256 and len(f.value or "") <= 1024 for f in embed.fields)
    assert len(embed.author.url or "") <= 2048
    assert parse_board_marker(embed.author.url or "") == (big_id, board_type, 2**31)


# ------------------------------------------- other embeds are left as they were


def test_pages_and_announcements_keep_their_format() -> None:
    row = entry(11, user_id=42, count=5, name="Alice", left=True)
    assert entry_line(row) == "**11.** <@42> · Alice (left) — **5 messages**"
    page = build_page_embed(board_type="daily", entries=[row], page=1, pages=1, now=NOW)
    assert page.title == "Daily Leaderboard — ranks 11–11"
    assert page.footer.text == "Page 1/1 · ranks 11–100"
    content, announcement = build_announcement(
        guild_id=GUILD, period_type="daily", period_key="2026-03-09", winner_id=42, winner_name="Alice",
        count=3, is_test=False,
    )
    assert content == "**Member of the Day:** <@42> — 3 messages"
    assert announcement.title == "Member of the Day"
    assert announcement.footer.text == "reset:1:daily:2026-03-09"
