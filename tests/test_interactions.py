"""Persistent buttons, pagination and DynamicItem dispatch after a restart.

The dispatch tests go through discord.py's real ``ViewStore.dispatch_view``: a
brand-new client (as after a restart) registers the DynamicItem classes and must
route a click on an old board message's button to the right callback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.clock import UTC
from bot.ui.views import (
    MAX_PAGES,
    PAGE_SIZE,
    BoardMoreButton,
    PageButton,
    board_view,
    build_page_view,
    clamp_page,
    handle_page,
    handle_show_more,
    page_count,
    page_entries,
    parse_more_custom_id,
    parse_page_custom_id,
    register_persistent_items,
    render_page,
)
from bot.ranking import RankEntry
from tests.conftest import CHANNEL, GUILD, install
from tests.fakes import App, FakeDiscord


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class FakeResponse:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.fail = fail

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.edited.append(kwargs)

    def is_done(self) -> bool:
        return bool(self.sent or self.edited)


def fake_interaction(client: Any, *, guild_id: int | None = GUILD, message: Any = None, custom_id: str = "",
                     fail: BaseException | None = None) -> Any:
    return SimpleNamespace(
        client=client,
        guild_id=guild_id,
        response=FakeResponse(fail),
        message=message,
        data={"custom_id": custom_id, "component_type": 2},
        user=SimpleNamespace(id=11),
    )


def entries(n: int) -> list[RankEntry]:
    return [RankEntry(i, 1000 + i, 500 - i, 1, f"user{i}", None, False) for i in range(1, n + 1)]


def not_found(code: int = 10062, message: str = "Unknown interaction") -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": code, "message": message})


async def populate(app: App, users: int) -> None:
    for i in range(users):
        for _ in range(users - i):  # distinct counts so ranks are obvious
            app.record(GUILD, 2000 + i, dt(2026, 3, 10, 12, 30))
    assert await app.buffer.flush()


# --------------------------------------------------------------- custom IDs


def test_custom_id_parsing_and_validation() -> None:
    assert parse_more_custom_id("lb:more:daily") == "daily"
    assert parse_more_custom_id("lb:more:alltime") == "alltime"
    for bad in ("lb:more:yearly", "lb:more:daily:1", "xlb:more:daily", "lb:more:", "", "lb:more:Daily"):
        assert parse_more_custom_id(bad) is None
    assert parse_page_custom_id("lb:pg:weekly:3") == ("weekly", 3)
    assert parse_page_custom_id(f"lb:pg:monthly:{MAX_PAGES}") == ("monthly", MAX_PAGES)
    for bad in ("lb:pg:weekly:0", f"lb:pg:weekly:{MAX_PAGES + 1}", "lb:pg:weekly:-1", "lb:pg:weekly:x",
                "lb:pg:weekly:99999", "lb:pg:hourly:1", "lb:pg:daily:1:extra", "lb:pg:daily"):
        assert parse_page_custom_id(bad) is None


def test_page_calculation() -> None:
    assert MAX_PAGES == 6 and PAGE_SIZE == 15
    assert [page_count(n) for n in (0, 1, 10, 11, 25, 26, 40, 41, 99, 100, 150)] == [0, 0, 0, 1, 1, 2, 2, 3, 6, 6, 6]
    ranked = entries(100)
    assert [e.rank for e in page_entries(ranked, 1)] == list(range(11, 26))
    assert [e.rank for e in page_entries(ranked, 6)] == list(range(86, 101))
    assert [e.rank for e in page_entries(entries(130), 6)][-1] == 100  # never beyond rank 100
    assert clamp_page(9, 3) == 3 and clamp_page(0, 3) == 1 and clamp_page(4, 0) == 1


async def test_prev_next_boundaries() -> None:
    assert build_page_view("daily", 1, 1) is None  # single page: no navigation
    first = build_page_view("daily", 1, 3)
    assert first is not None
    prev, nxt = first.children
    assert (prev.custom_id, prev.item.disabled) == ("lb:pg:daily:1", True)  # type: ignore[attr-defined]
    assert (nxt.custom_id, nxt.item.disabled) == ("lb:pg:daily:2", False)  # type: ignore[attr-defined]
    middle = build_page_view("daily", 2, 3)
    assert [c.custom_id for c in middle.children] == ["lb:pg:daily:1", "lb:pg:daily:3"]  # type: ignore[union-attr,attr-defined]
    assert not any(c.item.disabled for c in middle.children)  # type: ignore[union-attr,attr-defined]
    last = build_page_view("daily", 3, 3)
    prev, nxt = last.children  # type: ignore[union-attr]
    assert (prev.custom_id, prev.item.disabled) == ("lb:pg:daily:2", False)  # type: ignore[attr-defined]
    assert (nxt.custom_id, nxt.item.disabled) == ("lb:pg:daily:3", True)  # type: ignore[attr-defined]
    for page in range(1, 4):
        ids = [c.custom_id for c in build_page_view("daily", page, 3).children]  # type: ignore[union-attr,attr-defined]
        assert len(ids) == len(set(ids))  # custom IDs unique within a message


async def test_render_page_clamps_out_of_range() -> None:
    embed, view, page = render_page("weekly", entries(30), 6, dt(2026, 3, 10))
    assert page == 2
    assert "ranks 26–30" in (embed.title or "")
    assert view is not None


# ---------------------------------------------------------------- handlers


async def test_show_more_with_short_leaderboard(app: App) -> None:
    await populate(app, 5)
    interaction = fake_interaction(SimpleNamespace(services=app.services))
    await handle_show_more(interaction, "daily")
    (sent,) = interaction.response.sent
    assert sent["ephemeral"] is True
    assert "Nobody is ranked beyond the top 10" in sent["embed"].description
    assert "view" not in sent


async def test_show_more_with_empty_leaderboard(app: App) -> None:
    interaction = fake_interaction(SimpleNamespace(services=app.services))
    await handle_show_more(interaction, "alltime")
    assert interaction.response.sent[0]["ephemeral"] is True


async def test_show_more_first_page_and_navigation(app: App) -> None:
    await populate(app, 40)
    interaction = fake_interaction(SimpleNamespace(services=app.services))
    await handle_show_more(interaction, "daily")
    (sent,) = interaction.response.sent
    assert sent["ephemeral"] is True
    lines = sent["embed"].description.splitlines()
    assert len(lines) == 15 and lines[0].startswith("**11.**") and lines[-1].startswith("**25.**")
    assert [c.custom_id for c in sent["view"].children] == ["lb:pg:daily:1", "lb:pg:daily:2"]


async def test_pagination_recomputes_on_every_click(app: App) -> None:
    await populate(app, 30)
    services = SimpleNamespace(services=app.services)
    first = fake_interaction(services)
    await handle_page(first, "daily", 2)
    before = first.response.edited[0]["embed"].description
    # New activity changes the ranking between clicks.
    for _ in range(50):
        app.record(GUILD, 9999, dt(2026, 3, 10, 12, 45))
    assert await app.buffer.flush()
    second = fake_interaction(services)
    await handle_page(second, "daily", 2)
    after = second.response.edited[0]["embed"].description
    assert before != after


async def test_page_after_period_reset_clears_navigation(app: App, clock: Any) -> None:
    await populate(app, 30)
    clock.set(dt(2026, 3, 11, 0, 0, 1))  # the daily board is empty again
    interaction = fake_interaction(SimpleNamespace(services=app.services))
    await handle_page(interaction, "daily", 2)
    (edited,) = interaction.response.edited
    assert edited["view"] is None
    assert "Nobody is ranked beyond the top 10" in edited["embed"].description


@pytest.mark.parametrize("error", [not_found(10062, "Unknown interaction"), not_found(10008, "Unknown Message")])
async def test_expired_ephemeral_interaction_is_handled_gracefully(app: App, error: discord.NotFound) -> None:
    await populate(app, 30)
    interaction = fake_interaction(SimpleNamespace(services=app.services), fail=error)
    await handle_page(interaction, "daily", 2)  # must not raise
    await handle_show_more(interaction, "daily")


async def test_http_error_while_answering_is_handled(app: App) -> None:
    error = discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom")
    interaction = fake_interaction(SimpleNamespace(services=app.services), fail=error)
    await handle_show_more(interaction, "daily")


async def test_handler_outside_guild(app: App) -> None:
    interaction = fake_interaction(SimpleNamespace(services=app.services), guild_id=None)
    await handle_show_more(interaction, "daily")
    assert "only works inside the server" in interaction.response.sent[0]["content"]


# --------------------------------------------- persistence across restarts


def _message_from(view: discord.ui.View) -> Any:
    components = [discord.components._component_factory(row) for row in view.to_components()]
    return SimpleNamespace(id=12345, components=components, flags=discord.MessageFlags())


async def _dispatch(client: discord.Client, custom_id: str, interaction: Any) -> None:
    store = client._connection._view_store
    store.dispatch_view(2, custom_id, interaction)
    pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("discord-ui-dynamic-item")]
    assert pending, f"no DynamicItem matched {custom_id!r}"
    await asyncio.gather(*pending)


async def test_board_messages_carry_persistent_button_ids(app: App, fake: FakeDiscord) -> None:
    await install(app)
    for msg in fake.board_messages(CHANNEL):
        assert msg.components == [f"lb:more:{msg.board[1]}"]


async def test_dynamic_item_dispatch_after_restart(app: App) -> None:
    await populate(app, 30)
    old_view = board_view("weekly")  # what an earlier process attached to the board
    # A brand-new client, as after a restart: only the DynamicItem registration exists.
    client = discord.Client(intents=discord.Intents.none())
    try:
        client.services = app.services  # type: ignore[attr-defined]
        register_persistent_items(client)
        interaction = fake_interaction(client, message=_message_from(old_view), custom_id="lb:more:weekly")
        await _dispatch(client, "lb:more:weekly", interaction)
        (sent,) = interaction.response.sent
        assert sent["ephemeral"] is True and "ranks 11–25" in sent["embed"].title

        page_view = sent["view"]
        click = fake_interaction(client, message=_message_from(page_view), custom_id="lb:pg:weekly:2")
        await _dispatch(client, "lb:pg:weekly:2", click)
        (edited,) = click.response.edited
        assert "ranks 26–30" in edited["embed"].title
    finally:
        await client.close()


async def test_dynamic_item_with_tampered_page_is_clamped(app: App) -> None:
    await populate(app, 30)
    client = discord.Client(intents=discord.Intents.none())
    try:
        client.services = app.services  # type: ignore[attr-defined]
        register_persistent_items(client)
        view = discord.ui.View(timeout=None)
        view.add_item(PageButton("daily", 99, label="Next"))
        click = fake_interaction(client, message=_message_from(view), custom_id="lb:pg:daily:99")
        await _dispatch(client, "lb:pg:daily:99", click)
        assert "ranks 26–30" in click.response.edited[0]["embed"].title
    finally:
        await client.close()


async def test_unregistered_client_does_not_dispatch(app: App) -> None:
    client = discord.Client(intents=discord.Intents.none())
    try:
        client._connection._view_store.dispatch_view(2, "lb:more:daily", fake_interaction(client))
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("discord-ui-dynamic-item")]
    finally:
        await client.close()


def test_dynamic_item_templates() -> None:
    assert BoardMoreButton.__discord_ui_compiled_template__.fullmatch("lb:more:monthly")
    assert not BoardMoreButton.__discord_ui_compiled_template__.fullmatch("lb:more:monthly2")
    assert PageButton.__discord_ui_compiled_template__.fullmatch("lb:pg:alltime:6")
