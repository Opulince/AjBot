"""Board rendering, in-place edits, single-board recovery and crash-safe reposting."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from bot.clock import UTC
from bot.commands.admin import setup_guild
from bot.db.repo import RepostStatus
from bot.discord_ops import OpBadRequest, OpForbidden, OpNetwork, OpRateLimited, OpTransient
from bot.periods import BOARD_ORDER, unix_timestamp
from tests.conftest import CHANNEL, GUILD, ROLE_DAILY, ROLE_MONTHLY, ROLE_WEEKLY, T0, install
from tests.fakes import App, FakeClock, FakeDiscord, SimulatedCrash, make_config


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def world(tmp_path: Path) -> tuple[Any, FakeDiscord, FakeClock]:
    clock = FakeClock(T0)
    fake = FakeDiscord(clock)
    fake.add_guild(GUILD)
    fake.add_channel(CHANNEL)
    for role, pos in ((ROLE_DAILY, 10), (ROLE_WEEKLY, 11), (ROLE_MONTHLY, 12)):
        fake.create_role(GUILD, role, position=pos)
    for user in (11, 12, 13):
        fake.add_member(GUILD, user)
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_config(tmp_path), fake, clock


async def update(app: App) -> None:
    async with app.services.locks.get(GUILD):
        await app.boards.update_guild(GUILD)


async def assert_four_boards(app: App, fake: FakeDiscord, channel: int = CHANNEL) -> dict[str, int]:
    msgs = fake.board_messages(channel)
    assert sorted(m.board[1] for m in msgs) == sorted(BOARD_ORDER), [m.board for m in msgs]
    boards = await app.repo.get_board_messages(GUILD)
    assert {b.message_id for b in boards.values()} == {m.id for m in msgs}
    assert all(b.channel_id == channel for b in boards.values())
    assert await app.repo.get_active_repost(GUILD) is None
    return {t: boards[t].message_id for t in BOARD_ORDER}


# ----------------------------------------------------------------- setup


async def test_setup_posts_four_boards_in_order(app: App, fake: FakeDiscord) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    assert [ids[t] for t in BOARD_ORDER] == sorted(ids.values())
    msgs = fake.board_messages(CHANNEL)
    assert [m.board for m in msgs] == [(GUILD, t, 1) for t in BOARD_ORDER]
    for m in msgs:
        assert m.components == [f"lb:more:{m.board[1]}"]
        assert m.embed is not None and m.embed["footer"]["text"].startswith("Last updated · board:")
    cfg = await app.repo.get_guild_config(GUILD)
    assert cfg is not None and cfg.installed_at == T0


async def test_rerunning_setup_does_not_duplicate_and_keeps_installed_at(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    clock.advance(days=3)
    message = await setup_guild(app.services, GUILD, CHANNEL)
    assert "unchanged" in message
    assert await assert_four_boards(app, fake) == ids
    assert fake.count("send_message") == 4
    cfg = await app.repo.get_guild_config(GUILD)
    assert cfg is not None and cfg.installed_at == T0


async def test_setup_in_new_channel_moves_boards(app: App, fake: FakeDiscord) -> None:
    await install(app)
    fake.add_channel(200)
    await setup_guild(app.services, GUILD, 200)
    await assert_four_boards(app, fake, channel=200)
    assert fake.board_messages(CHANNEL) == []


# --------------------------------------------------------- normal updates


async def test_existing_messages_are_edited_not_recreated(app: App, fake: FakeDiscord) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    sends = fake.count("send_message")
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3, name="Alice")
    await update(app)
    await update(app)
    assert fake.count("send_message") == sends
    assert fake.count("delete_message") == 0
    assert await assert_four_boards(app, fake) == ids
    daily = next(m for m in fake.board_messages(CHANNEL) if m.board[1] == "daily")
    assert daily.edits >= 2 and "Alice" in daily.embed["description"]


async def test_missing_board_is_recreated_alone(app: App, fake: FakeDiscord) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    fake.channels[CHANNEL] = [m for m in fake.channels[CHANNEL] if m.id != ids["weekly"]]
    await update(app)
    new_ids = await assert_four_boards(app, fake)
    assert new_ids["weekly"] != ids["weekly"]
    assert {t: new_ids[t] for t in ("daily", "monthly", "alltime")} == {t: ids[t] for t in ("daily", "monthly", "alltime")}
    assert fake.count("send_message") == 5  # four from setup, one replacement
    boards = await app.repo.get_board_messages(GUILD)
    assert boards["weekly"].generation == 1


async def test_recreation_adopts_orphan_from_crashed_attempt(tmp_path: Path) -> None:
    config, fake, clock = world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    ids = await assert_four_boards(app, fake)
    fake.channels[CHANNEL] = [m for m in fake.channels[CHANNEL] if m.id != ids["daily"]]
    fake.crash_after("send_message", 1)  # replacement posted, ID never stored
    with pytest.raises(SimulatedCrash):
        await update(app)
    await app.crash()
    fake.clear_injections()
    app = await App.start(config, fake, clock)
    await update(app)
    await assert_four_boards(app, fake)  # the orphan was adopted, no duplicate
    assert fake.count("send_message") == 5
    await app.shutdown()


@pytest.mark.parametrize(
    "error",
    [OpForbidden("Missing Access"), OpRateLimited("429"), OpTransient("502"), OpNetwork("reset")],
    ids=["forbidden", "ratelimit", "5xx", "network"],
)
async def test_non_notfound_edit_failures_never_repost(app: App, fake: FakeDiscord, error: Exception) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    fake.fail_next("edit_message", error)
    await update(app)
    assert fake.count("send_message") == 4
    assert await assert_four_boards(app, fake) == ids


async def test_rejected_banner_falls_back_to_no_image(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await app.repo.set_banner(GUILD, "https://cdn.example.com/banner.png", app.services.clock.now())
    fake.fail_next("edit_message", OpBadRequest("Invalid Form Body: embeds.0.image.url"))
    await update(app)
    daily = next(m for m in fake.board_messages(CHANNEL) if m.board[1] == "daily")
    assert "image" not in daily.embed  # first board fell back
    weekly = next(m for m in fake.board_messages(CHANNEL) if m.board[1] == "weekly")
    assert weekly.embed["image"]["url"] == "https://cdn.example.com/banner.png"


async def test_board_embed_contents(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3, name="Alice*Star")
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 1, name="Bob")
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()  # produces a previous daily winner
    fake.remove_member(GUILD, 12)
    await app.repo.mark_member_left(GUILD, 12, clock.now())
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13, 30), 1, name="Bob")  # stale: keeps (left)
    await app.repo.set_banner(GUILD, "https://cdn.example.com/b.png", clock.now())
    await update(app)
    by_type = {m.board[1]: m.embed for m in fake.board_messages(CHANNEL)}
    daily = by_type["daily"]
    assert daily["title"] == "Daily Leaderboard"
    assert daily["image"]["url"] == "https://cdn.example.com/b.png"
    fields = {f["name"]: f["value"] for f in daily["fields"]}
    assert "<@11>" in fields["Previous winner"] and "2026-03-10" in fields["Previous winner"]
    ts = unix_timestamp(dt(2026, 3, 12))
    assert f"<t:{ts}:R>" in fields["Next reset"]
    assert daily["footer"]["text"].startswith("Last updated · board:1:daily:")
    assert "timestamp" in daily
    alltime = by_type["alltime"]
    assert "Next reset" not in {f["name"] for f in alltime.get("fields", [])}
    assert "<@11> · Alice\\*Star — **3 messages**" in alltime["description"]  # mention + escaped plain name
    assert "<@12> · Bob (left)" in alltime["description"]


async def test_membership_audit_marks_departed_top_users(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 13, dt(2026, 3, 10, 13), 2, name="Cara")
    clock.set(dt(2026, 3, 10, 14))
    fake.remove_member(GUILD, 13)
    await update(app)
    member = await app.repo.get_member(GUILD, 13)
    assert member is not None and member.left_at is not None
    alltime = next(m for m in fake.board_messages(CHANNEL) if m.board[1] == "alltime")
    assert "Cara (left)" in alltime.embed["description"]
    # A member who came back is shown normally again once the audit sees them.
    fake.add_member(GUILD, 13, "Cara")
    clock.advance(hours=7)  # past the audit TTL
    await update(app)
    assert (await app.repo.get_member(GUILD, 13)).left_at is None  # type: ignore[union-attr]
    # Transient errors never mark anyone as departed.
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 1)
    fake.remove_member(GUILD, 11)
    fake.fail_next("fetch_member", OpNetwork("timeout"))
    await update(app)
    assert (await app.repo.get_member(GUILD, 11)).left_at is None  # type: ignore[union-attr]


# ------------------------------------------------------------- reposting


async def test_announcement_sits_above_all_boards_after_reset(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 1)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    ids = await assert_four_boards(app, fake)
    announcement = fake.announcements(CHANNEL)[0]
    assert announcement.id < min(ids.values())
    assert [m.id for m in fake.messages(CHANNEL)] == [announcement.id] + [ids[t] for t in BOARD_ORDER]


async def _start_repost(app: App) -> None:
    async with app.services.locks.get(GUILD):
        await app.boards.request_repost(GUILD, "test")


async def _process(app: App) -> bool:
    async with app.services.locks.get(GUILD):
        return await app.boards.process_repost(GUILD)


@pytest.mark.parametrize("mode", ["after", "before"])
@pytest.mark.parametrize("k", range(1, 9))  # 4 sends + 4 deletes
async def test_repost_crash_at_any_point_converges(tmp_path: Path, k: int, mode: str) -> None:
    config, fake, clock = world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    old = await assert_four_boards(app, fake)
    clock.advance(minutes=1)
    await _start_repost(app)
    (fake.crash_after_mutation if mode == "after" else fake.crash_before_mutation)(k)
    with pytest.raises(SimulatedCrash):
        await _process(app)
    # Never zero functioning boards: either the old set or the new set is complete.
    remaining = {m.board[1] for m in fake.board_messages(CHANNEL)}
    assert remaining == set(BOARD_ORDER)
    await app.crash()
    fake.clear_injections()
    app = await App.start(config, fake, clock)
    assert await _process(app)
    new = await assert_four_boards(app, fake)
    assert not set(old.values()) & set(new.values())
    assert [new[t] for t in BOARD_ORDER] == sorted(new.values())
    assert {m.board[2] for m in fake.board_messages(CHANNEL)} == {2}
    await app.shutdown()


async def test_recovery_from_partially_persisted_ids(tmp_path: Path) -> None:
    config, fake, clock = world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await _start_repost(app)
    fake.crash_before("send_message", 3)  # two boards posted and persisted, third never sent
    with pytest.raises(SimulatedCrash):
        await _process(app)
    op = await app.repo.get_active_repost(GUILD)
    assert op is not None and sorted(op.new_ids) == ["daily", "weekly"] and op.status is RepostStatus.POSTING
    await app.crash()
    fake.clear_injections()
    app = await App.start(config, fake, clock)
    sends = fake.count("send_message")
    assert await _process(app)
    assert fake.count("send_message") - sends == 2  # only the missing boards
    await assert_four_boards(app, fake)
    await app.shutdown()


async def test_recovery_by_scanning_generation_markers(tmp_path: Path) -> None:
    config, fake, clock = world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await _start_repost(app)
    fake.crash_after("send_message", 3)  # third board posted but its ID was never stored
    with pytest.raises(SimulatedCrash):
        await _process(app)
    op = await app.repo.get_active_repost(GUILD)
    assert op is not None and sorted(op.new_ids) == ["daily", "weekly"]
    await app.crash()
    fake.clear_injections()
    app = await App.start(config, fake, clock)
    sends = fake.count("send_message")
    assert await _process(app)
    assert fake.count("send_message") - sends == 1  # monthly adopted, only all-time posted
    ids = await assert_four_boards(app, fake)
    assert [ids[t] for t in BOARD_ORDER] == sorted(ids.values())
    await app.shutdown()


async def test_delete_notfound_is_success(app: App, fake: FakeDiscord) -> None:
    await install(app)
    old = await assert_four_boards(app, fake)
    fake.channels[CHANNEL] = [m for m in fake.channels[CHANNEL] if m.id != old["daily"]]  # someone deleted it
    await _start_repost(app)
    assert await _process(app)
    await assert_four_boards(app, fake)


async def test_repost_retries_transient_failures_without_losing_boards(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await _start_repost(app)
    fake.fail_next("send_message", OpTransient("502"))
    assert not await _process(app)
    op = await app.repo.get_active_repost(GUILD)
    assert op is not None and op.attempts == 1 and "transient" in (op.last_error or "")
    assert len(fake.board_messages(CHANNEL)) == 4  # old boards untouched
    assert await _process(app)
    await assert_four_boards(app, fake)


async def test_normal_update_defers_to_active_repost(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await _start_repost(app)
    await update(app)  # finishes the repost instead of editing stale boards
    await assert_four_boards(app, fake)
    assert {m.board[2] for m in fake.board_messages(CHANNEL)} == {2}


async def test_only_one_active_repost(app: App) -> None:
    await install(app)
    now = app.services.clock.now()
    op1, created1 = await app.repo.create_repost(GUILD, CHANNEL, "a", now)
    op2, created2 = await app.repo.create_repost(GUILD, CHANNEL, "b", now)
    assert created1 and not created2 and op1.generation == op2.generation


async def test_forbidden_delete_gives_up_after_bounded_attempts(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await _start_repost(app)
    for _ in range(4):
        fake.fail_next("delete_message", OpForbidden("Missing Access"))
        assert not await _process(app)
    fake.fail_next("delete_message", OpForbidden("Missing Access"))
    assert await _process(app)  # fifth attempt: give up on that message, finish the rest
    assert await app.repo.get_active_repost(GUILD) is None
    assert len(fake.board_messages(CHANNEL)) == 5  # one undeletable old board remains
    boards = await app.repo.get_board_messages(GUILD)
    assert all(b.generation == 2 for b in boards.values())
