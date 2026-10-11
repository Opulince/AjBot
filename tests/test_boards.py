"""Board rendering, in-place edits, single-board recovery and crash-safe reposting."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from bot.clock import UTC
from bot.commands.admin import setup_guild
from bot.db.repo import RepostStatus, RunStatus
from bot.discord_ops import OpBadRequest, OpForbidden, OpNetwork, OpRateLimited, OpTransient
from bot.periods import BOARD_ORDER, parse_period_key, unix_timestamp
from bot.ui.embeds import parse_board_marker
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
        assert m.embed is not None
        assert m.embed["footer"]["text"] == f"1 message = 1 point · board:{GUILD}:{m.board[1]}:1 · Last updated"
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
    boards = await app.repo.get_board_messages(GUILD)
    daily = by_type["daily"]
    assert daily is not None
    assert daily["title"] == "☀️ Daily Leaderboard"
    assert daily["author"]["name"] == "Community Activity Championship"
    assert daily["image"]["url"] == "https://cdn.example.com/b.png"
    fields = {f["name"]: f["value"] for f in daily["fields"]}
    assert list(fields) == ["Previous Member of the Day", "Next reset"]
    assert "<@11>" in fields["Previous Member of the Day"] and "2026-03-10" in fields["Previous Member of the Day"]
    ts = unix_timestamp(dt(2026, 3, 12))
    assert fields["Next reset"] == f"<t:{ts}:R>\n<t:{ts}:f>"
    footer = daily["footer"]["text"]
    assert parse_board_marker(footer) == (GUILD, "daily", boards["daily"].generation)
    assert footer.startswith("1 message = 1 point · board:1:daily:") and footer.endswith(" · Last updated")
    assert daily["timestamp"] == clock.now().isoformat()
    alltime = by_type["alltime"]
    assert alltime is not None and alltime.get("fields", []) == []  # no previous winner, no reset countdown
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
    old = await assert_four_boards(app, fake)
    undeletable = old["alltime"]
    real_delete = fake.delete_message

    async def delete(channel_id: int, message_id: int) -> None:
        if message_id == undeletable:  # e.g. the bot lost access to that message
            raise OpForbidden("Missing Access")
        await real_delete(channel_id, message_id)

    fake.delete_message = delete  # type: ignore[method-assign]
    await _start_repost(app)
    for _ in range(4):
        assert not await _process(app)  # retried a bounded number of times
    assert await _process(app)  # fifth attempt: give up on that message, finish the rest
    assert await app.repo.get_active_repost(GUILD) is None
    remaining = fake.board_messages(CHANNEL)
    assert len(remaining) == 5 and undeletable in {m.id for m in remaining}  # sweep could not remove it either
    boards = await app.repo.get_board_messages(GUILD)
    assert all(b.generation == 2 for b in boards.values())


async def test_repost_stuck_on_deleted_old_channel_is_abandoned_when_channel_moves(app: App, fake: FakeDiscord) -> None:
    await install(app)
    old = await assert_four_boards(app, fake)
    await _start_repost(app)
    fake.crash_before("send_message", 2)  # one new board posted into the old channel
    with pytest.raises(SimulatedCrash):
        await _process(app)
    fake.clear_injections()
    del fake.channels[CHANNEL]  # the old channel is deleted entirely
    assert not await _process(app)  # would retry forever against the missing channel...
    fake.add_channel(300)
    await setup_guild(app.services, GUILD, 300)  # ...until the admin moves the leaderboard
    await assert_four_boards(app, fake, channel=300)
    boards = await app.repo.get_board_messages(GUILD)
    assert not set(old.values()) & {b.message_id for b in boards.values()}


async def test_update_moves_boards_after_channel_change_without_repost(app: App, fake: FakeDiscord) -> None:
    """Crash between saving the new channel and starting its repost: the next update moves the boards."""
    await install(app)
    fake.add_channel(400)
    await app.repo.setup_guild(GUILD, 400, app.services.clock.now())  # config saved, no repost yet
    await update(app)
    await assert_four_boards(app, fake, channel=400)
    assert fake.board_messages(CHANNEL) == []


async def test_audit_uses_observation_time_for_departures(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    """Regression: a message newer than the audit's start must not block recording the departure."""
    await install(app)
    clock.set(dt(2026, 3, 10, 14))
    await app.messages(GUILD, 13, dt(2026, 3, 10, 14, 0, 2), 1, name="Cara")  # seen after the audit starts
    fake.remove_member(GUILD, 13)

    def slow_api(op: str) -> None:
        if op == "fetch_member":
            clock.advance(seconds=5)  # the API answer arrives a few seconds later

    fake.on_call = slow_api
    await update(app)
    member = await app.repo.get_member(GUILD, 13)
    assert member is not None and member.left_at is not None


# ------------------------------------------------- strays, busy channels, banner


async def test_transport_level_duplicate_board_is_swept(app: App, fake: FakeDiscord) -> None:
    """discord.py re-POSTs on 5xx; if the first create succeeded, the untracked copy is removed."""
    await install(app)
    await _start_repost(app)
    fake.duplicate_next_send = True  # the daily board gets created twice
    assert await _process(app)
    await assert_four_boards(app, fake)


async def test_busy_channel_orphan_is_not_left_behind(tmp_path: Path) -> None:
    config, fake, clock = world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    clock.advance(minutes=20)
    for _ in range(250):  # lots of chat right before the repost starts
        fake.add_user_message(CHANNEL)
        clock.advance(seconds=2)
    await _start_repost(app)
    fake.crash_after("send_message", 3)  # monthly posted, never recorded
    with pytest.raises(SimulatedCrash):
        await _process(app)
    await app.crash()
    fake.clear_injections()
    app = await App.start(config, fake, clock)
    assert await _process(app)
    await assert_four_boards(app, fake)
    await app.shutdown()


async def test_single_recreation_falls_back_without_rejected_banner(app: App, fake: FakeDiscord) -> None:
    await install(app)
    await app.repo.set_banner(GUILD, "https://cdn.example.com/broken.png", app.services.clock.now())
    ids = await assert_four_boards(app, fake)
    fake.channels[CHANNEL] = [m for m in fake.channels[CHANNEL] if m.id != ids["weekly"]]
    fake.fail_next("send_message", OpBadRequest("Invalid Form Body: embeds.0.image.url"))
    await update(app)
    new_ids = await assert_four_boards(app, fake)
    weekly = next(m for m in fake.board_messages(CHANNEL) if m.id == new_ids["weekly"])
    assert "image" not in weekly.embed


async def test_single_recreation_sweeps_duplicate(app: App, fake: FakeDiscord) -> None:
    await install(app)
    ids = await assert_four_boards(app, fake)
    fake.channels[CHANNEL] = [m for m in fake.channels[CHANNEL] if m.id != ids["daily"]]
    fake.duplicate_next_send = True
    await update(app)
    await assert_four_boards(app, fake)


async def test_sweep_never_touches_other_users_or_tracked_boards(app: App, fake: FakeDiscord) -> None:
    await install(app)
    chat = fake.add_user_message(CHANNEL, content="board:1:daily:1 looks nice")  # a human quoting a marker
    await _start_repost(app)
    assert await _process(app)
    await assert_four_boards(app, fake)
    assert any(m.id == chat for m in fake.messages(CHANNEL))


# ------------------------------------------------------- previous winners


async def rendered(app: App, fake: FakeDiscord) -> dict[str, dict]:
    """Run a normal board update and return each board's embed by type."""
    await update(app)
    embeds: dict[str, dict] = {}
    for m in fake.board_messages(CHANNEL):
        assert m.board is not None and m.embed is not None
        embeds[m.board[1]] = m.embed
    return embeds


def field(embed: dict, name: str) -> str:
    values = [f["value"] for f in embed.get("fields", []) if f["name"] == name]
    assert len(values) == 1, (name, embed.get("fields"))
    return values[0]


async def test_each_board_shows_its_own_previous_winner_after_missed_boundaries(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)  # Tuesday 2026-03-10, ISO week 2026-W11
    await app.messages(GUILD, 13, dt(2026, 3, 12, 13), 8, name="Cara")   # W11, March -> March champion
    await app.messages(GUILD, 11, dt(2026, 3, 24, 13), 5, name="Alice")  # W13, March -> W13 champion
    await app.messages(GUILD, 12, dt(2026, 3, 31, 13), 3, name="Bob")    # W14, March -> 03-31 champion
    clock.set(dt(2026, 4, 1, 0, 5))  # offline since install: 22 daily, 3 weekly and 1 monthly boundary
    await app.runner.tick()
    weekly_runs = {r.period_key: r.status for r in await app.repo.list_runs(GUILD, "weekly")}
    assert weekly_runs["2026-W12"] is RunStatus.SKIPPED_EMPTY  # an older, empty week sits in between
    boards = await rendered(app, fake)

    daily = field(boards["daily"], "Previous Member of the Day")
    assert daily == "🏆 <@12> · Bob\n**3 messages** · 2026-03-31 (UTC)"
    weekly = field(boards["weekly"], "Previous Member of the Week")
    assert weekly == "🏆 <@11> · Alice\n**5 messages** · ISO week 2026-W13"
    monthly = field(boards["monthly"], "Previous Member of the Month")
    assert monthly == "🏆 <@13> · Cara\n**8 messages** · 2026-03"
    for board_type, label in (
        ("daily", "Previous Member of the Day"),
        ("weekly", "Previous Member of the Week"),
        ("monthly", "Previous Member of the Month"),
    ):
        assert [f["name"] for f in boards[board_type]["fields"]] == [label, "Next reset"]
    assert boards["alltime"].get("fields", []) == []


async def test_previous_winner_shows_the_frozen_count_and_escaped_name(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3, name="Alice*Star")
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    # Late deliveries still land in the closed day's bucket, but the recorded result is final.
    await app.messages(GUILD, 11, dt(2026, 3, 10, 23, 59), 2, name="Alice*Star")
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "🏆 <@11> · Alice\\*Star\n**3 messages** · 2026-03-10 (UTC)"


async def test_departed_previous_winner_is_labelled(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3, name="Alice")
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    fake.remove_member(GUILD, 11)
    await app.repo.mark_member_left(GUILD, 11, clock.now())
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value.startswith("🏆 <@11> · Alice (left)\n")


async def test_first_tracked_period_has_no_previous_result(app: App, fake: FakeDiscord) -> None:
    await install(app)
    boards = await rendered(app, fake)
    assert field(boards["daily"], "Previous Member of the Day") == "None yet — this is the first day being tracked."
    assert field(boards["weekly"], "Previous Member of the Week") == "None yet — this is the first week being tracked."
    assert field(boards["monthly"], "Previous Member of the Month") == "None yet — this is the first month being tracked."


async def test_empty_previous_period_is_shown_instead_of_an_older_winner(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 12, 0, 5))  # Mar 10 has a winner, Mar 11 nobody
    await app.runner.tick()
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "No eligible winner\n2026-03-11 (UTC)"


async def test_undecided_previous_period_never_falls_back_to_an_older_winner(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    """Regression: the board used to show the latest *decided* run, i.e. Mar 10's winner on Mar 12."""
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    clock.set(dt(2026, 3, 12, 0, 1))  # Mar 11 is still inside its grace period
    await app.runner.tick()
    assert (await app.repo.get_run_by_key(GUILD, "daily", "2026-03-11")).status is RunStatus.PENDING  # type: ignore[union-attr]
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "*Tallying results…*\n2026-03-11 (UTC)"
    clock.set(dt(2026, 3, 12, 0, 2))
    await app.runner.tick()
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "No eligible winner\n2026-03-11 (UTC)"


async def test_board_after_downtime_waits_for_catch_up_then_shows_latest_result(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()  # Mar 10: user 11
    await app.messages(GUILD, 13, dt(2026, 3, 11, 13), 2)  # Mar 11: user 13; Mar 12: nobody
    await app.messages(GUILD, 12, dt(2026, 3, 13, 13), 4)  # Mar 13: user 12
    clock.set(dt(2026, 3, 14, 0, 10))  # the bot was down across three boundaries
    # The board loop can win the guild lock before the reset catch-up has created the missed runs.
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "*Tallying results…*\n2026-03-13 (UTC)"
    await app.runner.tick()
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "🏆 <@12> · user12\n**4 messages** · 2026-03-13 (UTC)"


async def test_forced_test_resets_never_become_the_previous_winner(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 12, 30), 4)
    clock.set(dt(2026, 3, 10, 18))
    test_run = await app.runner.force_reset(GUILD, "daily")
    assert test_run.is_test and test_run.winner_user_id == 12
    await app.runner.force_reset(GUILD, "weekly")
    boards = await rendered(app, fake)
    assert field(boards["daily"], "Previous Member of the Day") == "None yet — this is the first day being tracked."
    assert field(boards["weekly"], "Previous Member of the Week") == "None yet — this is the first week being tracked."

    await app.messages(GUILD, 11, dt(2026, 3, 10, 19), 5)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()  # real Mar 10 result: user 11 with 5
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 9)
    clock.set(dt(2026, 3, 11, 18))
    assert (await app.runner.force_reset(GUILD, "daily")).winner_user_id == 12  # a newer test winner
    value = field((await rendered(app, fake))["daily"], "Previous Member of the Day")
    assert value == "🏆 <@11> · user11\n**5 messages** · 2026-03-10 (UTC)"


async def test_previous_result_reads_exactly_one_real_period(app: App, clock: FakeClock) -> None:
    await install(app)
    now = clock.now()
    day = parse_period_key("daily", "2026-03-10")
    await app.repo.create_runs(GUILD, [day], now)
    test_run = await app.repo.create_test_run(GUILD, "daily", "test-2026-03-10-x", day.start, now, now)
    run = await app.repo.previous_result(GUILD, "daily", "2026-03-10")
    assert run is not None and run.period_key == "2026-03-10" and run.status is RunStatus.PENDING and not run.is_test
    assert await app.repo.previous_result(GUILD, "daily", test_run.period_key) is None
    assert await app.repo.previous_result(GUILD, "weekly", "2026-03-10") is None
    assert await app.repo.previous_result(GUILD, "daily", "2026-03-09") is None


async def test_board_posted_with_the_old_footer_is_still_adopted(app: App, fake: FakeDiscord) -> None:
    """Boards posted before this layout change carry ``Last updated · board:…``; recovery must still find them."""
    await install(app)
    ids = await assert_four_boards(app, fake)
    old_style = next(m for m in fake.board_messages(CHANNEL) if m.id == ids["daily"])
    old_style.embed = {"title": "Daily Leaderboard", "footer": {"text": f"Last updated · board:{GUILD}:daily:1"}}
    stale = await app.repo.get_board_messages(GUILD)
    # The tracked ID points at a message that no longer exists; the old-style board is an orphan to adopt.
    await app.repo.set_board_message(GUILD, "daily", CHANNEL, ids["daily"] + 10_000, stale["daily"].generation)
    sends = fake.count("send_message")
    await update(app)
    assert fake.count("send_message") == sends  # adopted, not reposted
    assert (await assert_four_boards(app, fake))["daily"] == ids["daily"]
    await update(app)  # the next normal update edits it in place with the new layout
    adopted = next(m for m in fake.board_messages(CHANNEL) if m.id == ids["daily"])
    assert adopted.embed is not None
    assert adopted.embed["title"] == "☀️ Daily Leaderboard"
    assert adopted.embed["footer"]["text"] == f"1 message = 1 point · board:{GUILD}:daily:1 · Last updated"
