"""Persistent board buttons and ephemeral pagination.

Both component types are :class:`discord.ui.DynamicItem` subclasses, dispatched
by matching the custom ID against a regex template. They are registered with
``Client.add_dynamic_items`` during startup, so buttons on board messages posted
by an earlier process keep working after a restart without any per-message
state.

Custom IDs:

* ``lb:more:{board_type}`` — the "Show 11–100" button on each board
* ``lb:pg:{board_type}:{page}`` — Prev / Next on the ephemeral page (1-based page)

Every click recomputes the ranking from the database; nothing is cached.
Ephemeral messages only live as long as Discord keeps them; once a page can no
longer be edited the failure is logged and ignored.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any, Awaitable, Sequence

import discord
from discord import ui

from bot.ranking import TOP_LIMIT, RankEntry
from bot.ui.embeds import TOP_SHOWN, build_page_embed

log = logging.getLogger(__name__)

PAGE_SIZE = 15
MAX_PAGES = math.ceil((TOP_LIMIT - TOP_SHOWN) / PAGE_SIZE)  # 6 pages: ranks 11-100
BOARD_PATTERN = "daily|weekly|monthly|alltime"
MORE_TEMPLATE = rf"lb:more:(?P<board>{BOARD_PATTERN})"
PAGE_TEMPLATE = rf"lb:pg:(?P<board>{BOARD_PATTERN}):(?P<page>[0-9]{{1,3}})"
_MORE_RE = re.compile(MORE_TEMPLATE)
_PAGE_RE = re.compile(PAGE_TEMPLATE)
SHOW_MORE_LABEL = "Show 11–100"


def more_custom_id(board_type: str) -> str:
    return f"lb:more:{board_type}"


def page_custom_id(board_type: str, page: int) -> str:
    return f"lb:pg:{board_type}:{page}"


def parse_more_custom_id(custom_id: str) -> str | None:
    match = _MORE_RE.fullmatch(custom_id or "")
    return match["board"] if match else None


def parse_page_custom_id(custom_id: str) -> tuple[str, int] | None:
    match = _PAGE_RE.fullmatch(custom_id or "")
    if match is None:
        return None
    page = int(match["page"])
    if not 1 <= page <= MAX_PAGES:
        return None
    return match["board"], page


def page_count(total_ranked: int) -> int:
    """Number of 15-entry pages needed for ranks 11–100."""
    beyond_top = max(0, min(total_ranked, TOP_LIMIT) - TOP_SHOWN)
    return math.ceil(beyond_top / PAGE_SIZE)


def clamp_page(page: int, pages: int) -> int:
    return max(1, min(page, max(pages, 1)))


def page_entries(entries: Sequence[RankEntry], page: int) -> list[RankEntry]:
    start = TOP_SHOWN + (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, TOP_LIMIT)
    return list(entries[start:end])


def build_page_view(board_type: str, page: int, pages: int) -> ui.View | None:
    """Prev/Next buttons, or ``None`` when there is only one page.

    A disabled button keeps a valid, unique custom ID pointing at the current
    page; with a single page both would collide, so navigation is omitted.
    """
    if pages <= 1:
        return None
    view = ui.View(timeout=None)
    prev_target = page - 1 if page > 1 else page
    next_target = page + 1 if page < pages else page
    view.add_item(PageButton(board_type, prev_target, label="Prev", disabled=page <= 1))
    view.add_item(PageButton(board_type, next_target, label="Next", disabled=page >= pages))
    return view


def render_page(
    board_type: str, entries: Sequence[RankEntry], page: int, now: datetime
) -> tuple[discord.Embed, ui.View | None, int]:
    pages = page_count(len(entries))
    page = clamp_page(page, pages)
    embed = build_page_embed(
        board_type=board_type,
        entries=page_entries(entries, page) if pages else [],
        page=page,
        pages=pages,
        now=now,
    )
    return embed, build_page_view(board_type, page, pages), page


def board_view(board_type: str) -> ui.View:
    """The persistent view attached to each board message."""
    view = ui.View(timeout=None)
    view.add_item(BoardMoreButton(board_type))
    return view


async def _respond(action: Awaitable[Any], context: str) -> None:
    try:
        await action
    except discord.NotFound:
        # 10062 Unknown interaction / 10008 Unknown message: the ephemeral
        # message or interaction token has expired.
        log.info("Interaction for %s expired or its message is gone; ignoring", context)
    except discord.InteractionResponded:
        log.debug("Interaction for %s was already answered", context)
    except discord.HTTPException as exc:
        log.warning("Failed to answer interaction for %s: %s", context, exc)


async def _load_entries(interaction: Any, board_type: str) -> tuple[list[RankEntry], datetime] | None:
    services = getattr(interaction.client, "services", None)
    guild_id = getattr(interaction, "guild_id", None)
    if services is None or guild_id is None:
        return None
    now = services.clock.now()
    entries = await services.ranker.board_ranking(guild_id, board_type, now, limit=TOP_LIMIT)
    return entries, now


async def handle_show_more(interaction: Any, board_type: str) -> None:
    context = f"lb:more:{board_type}"
    try:
        loaded = await _load_entries(interaction, board_type)
    except Exception:
        log.exception("Failed to load %s leaderboard for pagination", board_type)
        loaded = None
        error = "Couldn't load the leaderboard right now. Please try again in a moment."
    else:
        error = "This button only works inside the server."
    if loaded is None:
        await _respond(interaction.response.send_message(error, ephemeral=True), context)
        return
    entries, now = loaded
    embed, view, _ = render_page(board_type, entries, 1, now)
    kwargs: dict[str, Any] = {"embed": embed, "ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    await _respond(interaction.response.send_message(**kwargs), context)


async def handle_page(interaction: Any, board_type: str, page: int) -> None:
    context = f"lb:pg:{board_type}:{page}"
    try:
        loaded = await _load_entries(interaction, board_type)
    except Exception:
        log.exception("Failed to load %s leaderboard for pagination", board_type)
        loaded = None
    if loaded is None:
        await _respond(
            interaction.response.send_message(
                "Couldn't load the leaderboard right now. Please try again in a moment.", ephemeral=True
            ),
            context,
        )
        return
    entries, now = loaded
    embed, view, _ = render_page(board_type, entries, page, now)
    # view=None clears stale navigation when the page set shrank (e.g. after a reset).
    await _respond(interaction.response.edit_message(embed=embed, view=view), context)


class BoardMoreButton(ui.DynamicItem[ui.Button], template=MORE_TEMPLATE):
    def __init__(self, board_type: str) -> None:
        super().__init__(
            ui.Button(
                label=SHOW_MORE_LABEL,
                style=discord.ButtonStyle.secondary,
                custom_id=more_custom_id(board_type),
            )
        )
        self.board_type = board_type

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
    ) -> "BoardMoreButton":
        return cls(match["board"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_show_more(interaction, self.board_type)


class PageButton(ui.DynamicItem[ui.Button], template=PAGE_TEMPLATE):
    def __init__(self, board_type: str, page: int, *, label: str = "Page", disabled: bool = False) -> None:
        super().__init__(
            ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=page_custom_id(board_type, page),
                disabled=disabled,
            )
        )
        self.board_type = board_type
        self.page = page

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
    ) -> "PageButton":
        # Out-of-range pages are clamped when rendering instead of raising, so a
        # tampered or stale custom ID still gets a sensible answer.
        page = int(match["page"])
        label = getattr(item, "label", None) or "Page"
        return cls(match["board"], page, label=label, disabled=False)

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_page(interaction, self.board_type, self.page)


def register_persistent_items(client: discord.Client) -> None:
    """Register DynamicItem classes so existing board buttons work after restarts."""
    client.add_dynamic_items(BoardMoreButton, PageButton)
