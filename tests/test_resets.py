"""Reset state machine, crash recovery, role reconciliation and missed-reset catch-up.

Crashes are injected with ``SimulatedCrash`` (a BaseException) from the fake
Discord layer, either right *after* an operation took effect (Discord did it,
SQLite never heard) or right *before* it. Recovery always starts a brand-new
application instance over the same database file and Discord state.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bot.clock import UTC
from bot.db.repo import RunStatus
from bot.discord_ops import OpForbidden, OpNetwork, OpRateLimited, OpTransient
from bot.periods import BOARD_ORDER
from bot.ui.embeds import reset_marker
from tests.conftest import CHANNEL, GUILD, ROLE_DAILY, ROLE_MONTHLY, ROLE_WEEKLY, T0, install
from tests.fakes import App, FakeClock, FakeDiscord, SimulatedCrash, make_config


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def build_world(tmp_path: Path) -> tuple[Any, FakeDiscord, FakeClock]:
    clock = FakeClock(T0)
    fake = FakeDiscord(clock)
    fake.add_guild(GUILD, bot_top_position=100)
    fake.add_channel(CHANNEL)
    fake.create_role(GUILD, ROLE_DAILY, position=10)
    fake.create_role(GUILD, ROLE_WEEKLY, position=11)
    fake.create_role(GUILD, ROLE_MONTHLY, position=12)
    for user in (11, 12, 13, 14, 15):
        fake.add_member(GUILD, user)
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_config(tmp_path), fake, clock


async def restart(app: App, config: Any, fake: FakeDiscord, clock: FakeClock) -> App:
    if app.services.db.connected:
        await app.crash()
    fake.clear_injections()
    return await App.start(config, fake, clock)


async def settle(app: App, clock: FakeClock, ticks: int = 6, step: timedelta = timedelta(minutes=1)) -> None:
    for _ in range(ticks):
        await app.runner.tick()
        clock.advance(seconds=step.total_seconds())


async def tick_with_crash(app: App, config: Any, fake: FakeDiscord, clock: FakeClock) -> tuple[App, bool]:
    try:
        await app.runner.tick()
    except SimulatedCrash:
        return await restart(app, config, fake, clock), True
    return app, False


async def run_of(app: App, ptype: str, key: str) -> Any:
    run = await app.repo.get_run_by_key(GUILD, ptype, key)
    assert run is not None, f"missing run {ptype}:{key}"
    return run


async def assert_converged(app: App, fake: FakeDiscord) -> None:
    """Invariants that must hold after recovery, whatever crashed where."""
    runs = await app.repo.list_runs(GUILD)
    keys = [(r.period_type, r.period_key) for r in runs]
    assert len(keys) == len(set(keys)), "duplicate logical runs"
    assert all(r.terminal for r in runs), [(r.period_key, r.status) for r in runs if not r.terminal]
    # Exactly one announcement per announced run, and its ID is the one recorded.
    footers = Counter(m.footer for m in fake.announcements(CHANNEL))
    assert all(n == 1 for n in footers.values()), footers
    announced = [r for r in runs if r.announcement_message_id is not None]
    assert len(announced) == len(footers)
    for r in announced:
        msgs = [m for m in fake.announcements(CHANNEL) if m.footer == reset_marker(GUILD, r.period_type.value, r.period_key)]
        assert [m.id for m in msgs] == [r.announcement_message_id]
    # Exactly four boards, matching board_messages, below the latest announcement.
    boards = await app.repo.get_board_messages(GUILD)
    msgs = fake.board_messages(CHANNEL)
    assert sorted(m.board[1] for m in msgs) == sorted(BOARD_ORDER), [m.board for m in msgs]
    assert {b.message_id for b in boards.values()} == {m.id for m in msgs}
    ids = [boards[t].message_id for t in BOARD_ORDER]
    assert ids == sorted(ids), "boards out of order"
    if announced:
        assert max(r.announcement_message_id for r in announced) < min(ids)
    assert await app.repo.get_active_repost(GUILD) is None


# ------------------------------------------------------------------ basic flow


async def test_daily_reset_full_flow(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    old_boards = {b.message_id for b in (await app.repo.get_board_messages(GUILD)).values()}
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 4)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 2, 1))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE
    assert (run.winner_user_id, run.winner_count) == (11, 4)
    assert run.period_start == dt(2026, 3, 10) and run.period_end == dt(2026, 3, 11)
    anns = fake.announcements(CHANNEL)
    assert len(anns) == 1 and "Member of the Day" in anns[0].content and "<@11>" in anns[0].content
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}
    holder = await app.repo.get_role_holder(GUILD, "daily")
    assert holder is not None and (holder.user_id, holder.since_key) == (11, "2026-03-10")
    new_boards = {b.message_id for b in (await app.repo.get_board_messages(GUILD)).values()}
    assert not (old_boards & new_boards)
    assert not any(m.id in old_boards for m in fake.messages(CHANNEL))
    await assert_converged(app, fake)


async def test_not_processed_before_grace_period(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 1)
    clock.set(dt(2026, 3, 11, 0, 1, 59))  # grace is 120 s
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.PENDING
    assert fake.announcements(CHANNEL) == []
    clock.set(dt(2026, 3, 11, 0, 2, 0))
    await app.runner.tick()
    assert (await run_of(app, "daily", "2026-03-10")).status is RunStatus.DONE


async def test_flush_failure_leaves_run_pending_and_never_uses_memory_counts(
    app: App, fake: FakeDiscord, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)  # persisted
    for i in range(5):  # buffered only: user 12 is the true winner
        app.record(GUILD, 12, dt(2026, 3, 10, 20, 0, i))
    real = app.repo.apply_count_batch

    async def broken(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(app.repo, "apply_count_batch", broken)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.PENDING and run.attempts == 1
    assert "flush" in (run.last_error or "")
    assert run.next_attempt_at is not None
    assert fake.announcements(CHANNEL) == []

    monkeypatch.setattr(app.repo, "apply_count_batch", real)
    clock.set(run.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and (run.winner_user_id, run.winner_count) == (12, 5)


async def test_empty_period_is_skipped_without_announcement_or_role_change(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 1)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}
    announcements_before = len(fake.announcements(CHANNEL))
    edits_before = sum(m.edits for m in fake.board_messages(CHANNEL))
    clock.set(dt(2026, 3, 12, 0, 3))  # nobody spoke on the 11th
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-11")
    assert run.status is RunStatus.SKIPPED_EMPTY and run.winner_user_id is None
    assert len(fake.announcements(CHANNEL)) == announcements_before
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}
    async with app.services.locks.get(GUILD):
        await app.boards.update_guild(GUILD)  # board updates continue normally
    assert sum(m.edits for m in fake.board_messages(CHANNEL)) > edits_before


async def test_winner_frozen_across_restart(config: Any, fake: FakeDiscord, clock: FakeClock) -> None:
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 1)
    clock.set(dt(2026, 3, 11, 0, 3))
    fake.crash_before("send_message")  # crash right after the winner is computed
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    assert (await run_of(app, "daily", "2026-03-10")).status is RunStatus.COMPUTED
    # Late-arriving counts for that day would change the ranking now...
    await app.messages(GUILD, 12, dt(2026, 3, 10, 23, 50), 10)
    await settle(app, clock)
    run = await run_of(app, "daily", "2026-03-10")
    # ...but the frozen winner is never recomputed.
    assert (run.winner_user_id, run.winner_count, run.status) == (11, 3, RunStatus.DONE)
    await assert_converged(app, fake)
    await app.shutdown()


# ---------------------------------------------------------- targeted crashes


async def _setup_two_day_world(tmp_path: Path) -> tuple[App, Any, FakeDiscord, FakeClock]:
    """Day 1 (Mar 10) completed with winner 11; day 2 (Mar 11) data has winner 12."""
    config, fake, clock = build_world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    assert (await run_of(app, "daily", "2026-03-10")).status is RunStatus.DONE
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 5)
    await app.messages(GUILD, 11, dt(2026, 3, 11, 13), 1)
    clock.set(dt(2026, 3, 12, 0, 3))
    return app, config, fake, clock


async def _assert_day2_final(app: App, fake: FakeDiscord) -> None:
    run = await run_of(app, "daily", "2026-03-11")
    assert run.status is RunStatus.DONE and (run.winner_user_id, run.winner_count) == (12, 5)
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}
    holder = await app.repo.get_role_holder(GUILD, "daily")
    assert holder is not None and holder.user_id == 12
    await assert_converged(app, fake)


async def test_crash_after_announcement_sent_before_id_committed(tmp_path: Path) -> None:
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    fake.crash_after("send_message", 1)  # Discord accepted the announcement, SQLite never heard
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    run = await run_of(app, "daily", "2026-03-11")
    assert run.status is RunStatus.COMPUTED and run.announce_started_at is not None
    assert run.announcement_message_id is None
    posted = [m for m in fake.announcements(CHANNEL) if "2026-03-11" in m.footer]
    assert len(posted) == 1
    await settle(app, clock)
    run = await run_of(app, "daily", "2026-03-11")
    assert run.announcement_message_id == posted[0].id  # adopted, not reposted
    await _assert_day2_final(app, fake)
    await app.shutdown()


async def test_crash_after_old_role_removed(tmp_path: Path) -> None:
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    fake.crash_after("remove_role", 1)
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}  # Discord already moved the role
    holder = await app.repo.get_role_holder(GUILD, "daily")
    assert holder is not None and holder.user_id == 11  # DB not yet updated
    await settle(app, clock)
    await _assert_day2_final(app, fake)
    await app.shutdown()


async def test_crash_after_new_role_assigned(tmp_path: Path) -> None:
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    fake.crash_after("add_role", 1)
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11, 12}  # temporary overlap, never zero holders
    await settle(app, clock)
    await _assert_day2_final(app, fake)
    await app.shutdown()


async def test_crash_before_role_holders_updated(tmp_path: Path) -> None:
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    machine = app.services.machine
    # Drive the run to ANNOUNCED explicitly, then crash during the verification fetches.
    await app.runner.ensure_runs(GUILD)
    run = await run_of(app, "daily", "2026-03-11")
    run = await machine.compute(run)
    run = await machine.announce(run, public=True)
    assert run.status is RunStatus.ANNOUNCED
    # fetch winner, fetch previous, [add/remove], verify winner, verify previous -> crash at the last fetch
    fake.crash_after("fetch_member", 4)
    with pytest.raises(SimulatedCrash):
        await machine.apply_roles(run)
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}
    assert (await app.repo.get_role_holder(GUILD, "daily")).user_id == 11  # type: ignore[union-attr]
    adds, removes = fake.count("add_role"), fake.count("remove_role")
    app = await restart(app, config, fake, clock)
    await settle(app, clock)
    # Recovery verified the existing Discord state: no further role churn.
    assert (fake.count("add_role"), fake.count("remove_role")) == (adds, removes)
    await _assert_day2_final(app, fake)
    await app.shutdown()


async def test_crash_during_board_repost(tmp_path: Path) -> None:
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    # Mutations this tick: announcement, add_role, remove_role, then the repost sends.
    fake.crash_after_mutation(5)  # after the 2nd replacement board is posted
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    op = await app.repo.get_active_repost(GUILD)
    assert op is not None and len(op.new_ids) == 1  # second board posted but never recorded
    await settle(app, clock)
    await _assert_day2_final(app, fake)
    await app.shutdown()


# announcement, add_role, remove_role, 4 board sends, 4 old-board deletes
RESET_MUTATIONS = 11


async def test_reset_tick_performs_expected_mutations(tmp_path: Path) -> None:
    """Guards the crash matrix below: it must cover every mutation of the reset tick."""
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    before = sum(fake.count(op) for op in ("send_message", "edit_message", "delete_message", "add_role", "remove_role"))
    await app.runner.tick()
    after = sum(fake.count(op) for op in ("send_message", "edit_message", "delete_message", "add_role", "remove_role"))
    assert after - before == RESET_MUTATIONS
    await _assert_day2_final(app, fake)
    await app.shutdown()


@pytest.mark.parametrize("double", [False, True])
@pytest.mark.parametrize("mode", ["after", "before"])
@pytest.mark.parametrize("k", range(1, RESET_MUTATIONS + 1))
async def test_crash_at_every_mutation_converges(tmp_path: Path, k: int, mode: str, double: bool) -> None:
    """Crash at the k-th Discord mutation of a reset tick (optionally twice); recovery converges."""
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    (fake.crash_after_mutation if mode == "after" else fake.crash_before_mutation)(k)
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    if double:
        (fake.crash_after_mutation if mode == "after" else fake.crash_before_mutation)(1)
        app, _ = await tick_with_crash(app, config, fake, clock)
    await settle(app, clock)
    await _assert_day2_final(app, fake)
    day1 = await run_of(app, "daily", "2026-03-10")
    assert (day1.winner_user_id, day1.status) == (11, RunStatus.DONE)
    await app.shutdown()


async def test_repeated_restarts_until_done(tmp_path: Path) -> None:
    """Crash after every single mutation, restarting each time, until the reset completes."""
    app, config, fake, clock = await _setup_two_day_world(tmp_path)
    crashes = 0
    for _ in range(40):
        fake.crash_after_mutation(1)
        app, crashed = await tick_with_crash(app, config, fake, clock)
        if not crashed:
            break
        crashes += 1
    assert crashes >= 8
    await settle(app, clock)
    await _assert_day2_final(app, fake)
    await app.shutdown()


# ------------------------------------------------------------------------ roles


async def test_same_winner_consecutive_periods_no_churn(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    await app.messages(GUILD, 11, dt(2026, 3, 11, 13), 2)
    clock.set(dt(2026, 3, 12, 0, 3))
    await app.runner.tick()
    assert fake.count("add_role") == 1 and fake.count("remove_role") == 0
    holder = await app.repo.get_role_holder(GUILD, "daily")
    assert holder is not None and (holder.user_id, holder.since_key) == (11, "2026-03-10")
    assert (await run_of(app, "daily", "2026-03-11")).status is RunStatus.DONE


async def test_previous_holder_left_skips_removal(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    fake.remove_member(GUILD, 11)
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 2)
    clock.set(dt(2026, 3, 12, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-11")
    assert run.status is RunStatus.DONE
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}
    assert fake.count("remove_role") == 0
    assert (await app.repo.get_role_holder(GUILD, "daily")).user_id == 12  # type: ignore[union-attr]


async def test_winner_left_before_role_assignment(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    clock.set(dt(2026, 3, 12, 0, 3))
    machine = app.services.machine
    await app.runner.ensure_runs(GUILD)
    run = await run_of(app, "daily", "2026-03-11")
    run = await machine.compute(run)
    run = await machine.announce(run, public=True)
    fake.remove_member(GUILD, 12)  # winner leaves between announcement and roles
    run = await machine.apply_roles(run)
    assert run.status is RunStatus.ROLES_DONE
    assert "winner left" in (run.last_error or "")
    assert fake.holders_of(GUILD, ROLE_DAILY) == set()  # previous holder no longer the latest winner
    assert await app.repo.get_role_holder(GUILD, "daily") is None
    await settle(app, clock)
    assert (await run_of(app, "daily", "2026-03-11")).status is RunStatus.DONE


async def test_role_hierarchy_error_retries_with_backoff_without_blocking_others(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    fake.guilds[GUILD].roles[ROLE_DAILY].position = 150  # above the bot's top role
    await app.messages(GUILD, 11, dt(2026, 3, 15, 13), 2)  # Sunday
    clock.set(dt(2026, 3, 16, 0, 3))  # Monday: daily and weekly both due
    await app.runner.tick()
    daily = await run_of(app, "daily", "2026-03-15")
    weekly = await run_of(app, "weekly", "2026-W11")
    assert daily.status is RunStatus.ANNOUNCED
    assert daily.attempts == 1 and "forbidden" in (daily.last_error or "")
    assert daily.next_attempt_at is not None and daily.next_attempt_at > clock.now()
    assert weekly.status is RunStatus.DONE and fake.holders_of(GUILD, ROLE_WEEKLY) == {11}
    assert fake.holders_of(GUILD, ROLE_DAILY) == set()
    # Still failing on the next attempt: attempts increase, never marked complete.
    clock.set(daily.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    daily = await run_of(app, "daily", "2026-03-15")
    assert daily.status is RunStatus.ANNOUNCED and daily.attempts == 2
    # Boards keep updating meanwhile.
    edits = sum(m.edits for m in fake.board_messages(CHANNEL))
    async with app.services.locks.get(GUILD):
        await app.boards.update_guild(GUILD)
    assert sum(m.edits for m in fake.board_messages(CHANNEL)) > edits
    # Fix the hierarchy: the next retry succeeds.
    fake.guilds[GUILD].roles[ROLE_DAILY].position = 10
    clock.set(daily.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    daily = await run_of(app, "daily", "2026-03-15")
    assert daily.status is RunStatus.DONE and fake.holders_of(GUILD, ROLE_DAILY) == {11}


async def test_forbidden_from_discord_is_recorded_and_retried(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    fake.fail_next("add_role", OpForbidden("403 Forbidden (error code: 50013): Missing Permissions", code=50013))
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.ANNOUNCED and "forbidden" in (run.last_error or "")
    assert await app.repo.get_role_holder(GUILD, "daily") is None
    clock.set(run.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and fake.holders_of(GUILD, ROLE_DAILY) == {11}


async def test_transient_network_failures_retry_and_eventually_succeed(
    app: App, fake: FakeDiscord, clock: FakeClock
) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    # Three network errors exhaust the in-call retries once; the run backs off.
    fake.fail_next("add_role", OpNetwork("reset"), OpNetwork("reset"), OpNetwork("reset"))
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.ANNOUNCED and "network" in (run.last_error or "")
    # A 5xx and a rate limit on the next attempt are absorbed by in-call retries.
    fake.fail_next("add_role", OpTransient("503"), OpRateLimited("429", retry_after=0.01))
    clock.set(run.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and fake.holders_of(GUILD, ROLE_DAILY) == {11}


async def test_unknown_membership_never_disqualifies_winner(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 5)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 13), 1)
    fake.fail_next("fetch_member", OpNetwork("timeout"), OpNetwork("timeout"), OpNetwork("timeout"))
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.PENDING and run.winner_user_id is None
    member = await app.repo.get_member(GUILD, 11)
    assert member is not None and member.left_at is None
    clock.set(run.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    assert (await run_of(app, "daily", "2026-03-10")).winner_user_id == 11


async def test_db_diverging_from_discord_is_reconciled(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    # An admin manually moved the role: DB says 11, Discord says 12.
    fake.guilds[GUILD].members[11].roles.discard(ROLE_DAILY)
    fake.guilds[GUILD].members[12].roles.add(ROLE_DAILY)
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    adds, removes = fake.count("add_role"), fake.count("remove_role")
    clock.set(dt(2026, 3, 12, 0, 3))
    await app.runner.tick()
    assert (fake.count("add_role"), fake.count("remove_role")) == (adds, removes)  # nothing to change
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}
    assert (await app.repo.get_role_holder(GUILD, "daily")).user_id == 12  # type: ignore[union-attr]


async def test_holder_who_lost_role_gets_it_back(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    fake.guilds[GUILD].members[11].roles.discard(ROLE_DAILY)
    await app.messages(GUILD, 11, dt(2026, 3, 11, 13), 2)
    clock.set(dt(2026, 3, 12, 0, 3))
    await app.runner.tick()
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}
    assert fake.count("add_role") == 2


async def test_no_role_configured_completes_without_role_operations(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app, roles=False)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and run.announcement_message_id is not None
    assert fake.count("add_role") == 0


async def test_deleted_reward_role_does_not_block(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    del fake.guilds[GUILD].roles[ROLE_DAILY]
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and "no longer exists" in (run.last_error or "")


# --------------------------------------------------------------------- catch-up


async def test_catch_up_chronological_with_historical_winners(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    await app.messages(GUILD, 13, dt(2026, 3, 12, 13), 3)
    await app.messages(GUILD, 11, dt(2026, 3, 13, 0, 0, 30), 9)  # current-period data must not leak back
    reposts_before = await _repost_count(app)
    clock.set(dt(2026, 3, 13, 0, 5))  # offline across three boundaries
    await app.runner.tick()
    winners = {r.period_key: r.winner_user_id for r in await app.repo.list_runs(GUILD, "daily")}
    assert winners == {"2026-03-10": 11, "2026-03-11": 12, "2026-03-12": 13}
    statuses = {r.period_key: r.status for r in await app.repo.list_runs(GUILD, "daily")}
    assert statuses == {
        "2026-03-10": RunStatus.ROLES_SUPERSEDED,
        "2026-03-11": RunStatus.ROLES_SUPERSEDED,
        "2026-03-12": RunStatus.DONE,
    }
    anns = fake.announcements(CHANNEL)
    assert [a.footer for a in anns] == [reset_marker(GUILD, "daily", "2026-03-12")]
    assert fake.holders_of(GUILD, ROLE_DAILY) == {13}
    assert fake.count("add_role") == 1  # superseded runs never touched the role
    assert await _repost_count(app) - reposts_before == 1  # one repost for the whole catch-up
    await assert_converged(app, fake)


async def _repost_count(app: App) -> int:
    async with app.services.db.read() as conn:
        async with conn.execute("SELECT COUNT(*) FROM board_repost WHERE guild_id = ?", (GUILD,)) as cur:
            row = await cur.fetchone()
    return int(row[0])


async def test_catch_up_daily_weekly_monthly_overlap(config: Any, fake: FakeDiscord) -> None:
    clock = FakeClock(dt(2026, 3, 25, 12))  # Wednesday of 2026-W13
    fake.clock = clock
    app = await App.start(config, fake, clock)
    await install(app)
    # Daily champions differ from the weekly / monthly champions.
    await app.messages(GUILD, 11, dt(2026, 3, 25, 13), 6)   # W13, March
    await app.messages(GUILD, 12, dt(2026, 3, 26, 13), 5)   # W13, March
    await app.messages(GUILD, 12, dt(2026, 3, 27, 13), 5)   # W13, March -> 12 wins W13 (10 vs 6)
    await app.messages(GUILD, 13, dt(2026, 3, 31, 13), 4)   # W14, March
    await app.messages(GUILD, 14, dt(2026, 4, 1, 13), 7)    # W14, April
    await app.messages(GUILD, 13, dt(2026, 4, 5, 13), 4)    # W14 (Sunday) -> 13 wins W14 with 8 vs 7
    await app.messages(GUILD, 15, dt(2026, 4, 6, 13), 2)    # Monday of W15, April
    clock.set(dt(2026, 4, 7, 0, 5))
    await app.runner.tick()

    weekly = {r.period_key: (r.winner_user_id, r.winner_count, r.status) for r in await app.repo.list_runs(GUILD, "weekly")}
    assert weekly == {
        "2026-W13": (12, 10, RunStatus.ROLES_SUPERSEDED),
        "2026-W14": (13, 8, RunStatus.DONE),
    }
    monthly = {r.period_key: (r.winner_user_id, r.winner_count, r.status) for r in await app.repo.list_runs(GUILD, "monthly")}
    assert monthly == {"2026-03": (12, 10, RunStatus.DONE)}
    daily = {r.period_key: r for r in await app.repo.list_runs(GUILD, "daily")}
    assert len(daily) == 13  # Mar 25 .. Apr 6
    assert daily["2026-03-25"].winner_user_id == 11
    assert daily["2026-04-01"].winner_user_id == 14
    assert daily["2026-04-06"].winner_user_id == 15 and daily["2026-04-06"].status is RunStatus.DONE
    assert daily["2026-03-28"].status is RunStatus.SKIPPED_EMPTY
    # Exactly the latest of each type was announced.
    assert sorted(a.footer for a in fake.announcements(CHANNEL)) == sorted([
        reset_marker(GUILD, "daily", "2026-04-06"),
        reset_marker(GUILD, "weekly", "2026-W14"),
        reset_marker(GUILD, "monthly", "2026-03"),
    ])
    assert fake.holders_of(GUILD, ROLE_DAILY) == {15}
    assert fake.holders_of(GUILD, ROLE_WEEKLY) == {13}
    assert fake.holders_of(GUILD, ROLE_MONTHLY) == {12}
    assert fake.count("add_role") == 3
    await assert_converged(app, fake)
    await app.shutdown()


async def test_empty_latest_period_keeps_latest_eligible_winner(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 12, 0, 5))  # Mar 10 has a winner, Mar 11 is empty
    await app.runner.tick()
    runs = {r.period_key: r for r in await app.repo.list_runs(GUILD, "daily")}
    assert runs["2026-03-11"].status is RunStatus.SKIPPED_EMPTY
    assert runs["2026-03-10"].status is RunStatus.DONE
    assert runs["2026-03-10"].announcement_message_id is None  # historical: recorded silently
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}


async def test_interrupted_historical_announcement_is_adopted_not_reposted(tmp_path: Path) -> None:
    config, fake, clock = build_world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 11, 0, 3))
    fake.crash_after("send_message", 1)  # announcement posted, then the process died
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    # The bot stays offline for two more days.
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    await app.messages(GUILD, 13, dt(2026, 3, 12, 13), 3)
    clock.set(dt(2026, 3, 13, 0, 5))
    await settle(app, clock)
    first = await run_of(app, "daily", "2026-03-10")
    posted = [m for m in fake.announcements(CHANNEL) if "2026-03-10" in m.footer]
    assert len(posted) == 1 and first.announcement_message_id == posted[0].id
    assert first.status is RunStatus.ROLES_SUPERSEDED
    assert [m.footer for m in fake.announcements(CHANNEL)] == [
        reset_marker(GUILD, "daily", "2026-03-10"),
        reset_marker(GUILD, "daily", "2026-03-12"),
    ]
    assert fake.holders_of(GUILD, ROLE_DAILY) == {13}
    await assert_converged(app, fake)
    await app.shutdown()


async def test_interrupted_role_work_is_finished_not_superseded(tmp_path: Path) -> None:
    config, fake, clock = build_world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 3)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()  # 11 holds the role
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    clock.set(dt(2026, 3, 12, 0, 3))
    fake.crash_after("add_role", 1)  # 12 got the role, 11 still has it, DB knows nothing
    app, crashed = await tick_with_crash(app, config, fake, clock)
    assert crashed
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11, 12}
    interrupted = await run_of(app, "daily", "2026-03-11")
    assert interrupted.role_started_at is not None
    # Offline for another day; 13 wins Mar 12.
    await app.messages(GUILD, 13, dt(2026, 3, 12, 13), 3)
    clock.set(dt(2026, 3, 13, 0, 5))
    await settle(app, clock)
    interrupted = await run_of(app, "daily", "2026-03-11")
    assert interrupted.status is RunStatus.DONE  # finished, not superseded
    latest = await run_of(app, "daily", "2026-03-12")
    assert latest.status is RunStatus.DONE
    assert fake.holders_of(GUILD, ROLE_DAILY) == {13}  # nobody else keeps a leaked role
    assert (await app.repo.get_role_holder(GUILD, "daily")).user_id == 13  # type: ignore[union-attr]
    await assert_converged(app, fake)
    await app.shutdown()


async def test_catch_up_on_startup_creates_no_duplicates(tmp_path: Path) -> None:
    config, fake, clock = build_world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 1)
    clock.set(dt(2026, 3, 15, 0, 5))
    for _ in range(3):
        await app.runner.tick()
        app = await restart(app, config, fake, clock)
    runs = await app.repo.list_runs(GUILD)
    keys = [(r.period_type, r.period_key) for r in runs]
    assert len(keys) == len(set(keys))
    assert [r.period_key for r in runs if r.period_type.value == "daily"] == [
        "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13", "2026-03-14"
    ]
    await app.shutdown()


# ----------------------------------------------------------------- forced reset


async def test_force_reset_uses_test_key_and_real_flow(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 12, dt(2026, 3, 10, 12, 30), 4)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 12, 30), 2)
    clock.set(dt(2026, 3, 10, 18))
    run = await app.runner.force_reset(GUILD, "daily")
    assert run.is_test and run.period_key.startswith("test-2026-03-10-")
    assert run.status is RunStatus.DONE and (run.winner_user_id, run.winner_count) == (12, 4)
    assert run.period_start == dt(2026, 3, 10) and run.period_end == dt(2026, 3, 10, 18)
    anns = fake.announcements(CHANNEL)
    assert len(anns) == 1 and anns[0].content.startswith("[TEST]")
    assert fake.holders_of(GUILD, ROLE_DAILY) == {12}
    # No production run for the current period was created or touched.
    assert await app.repo.get_run_by_key(GUILD, "daily", "2026-03-10") is None
    # The real reset later runs normally with its own key and the real result.
    await app.messages(GUILD, 11, dt(2026, 3, 10, 19), 5)
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    real = await run_of(app, "daily", "2026-03-10")
    assert not real.is_test and real.status is RunStatus.DONE
    assert (real.winner_user_id, real.winner_count) == (11, 7)
    assert fake.holders_of(GUILD, ROLE_DAILY) == {11}
    await assert_converged(app, fake)


async def test_force_reset_does_not_change_counts(app: App, clock: FakeClock) -> None:
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 12, 30), 3)
    before = await app.repo.aggregate_counts(GUILD)
    clock.set(dt(2026, 3, 10, 13))
    await app.runner.force_reset(GUILD, "weekly")
    assert await app.repo.aggregate_counts(GUILD) == before


async def test_force_reset_crash_is_recovered(tmp_path: Path) -> None:
    config, fake, clock = build_world(tmp_path)
    app = await App.start(config, fake, clock)
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 12, 30), 3)
    clock.set(dt(2026, 3, 10, 13))
    fake.crash_after("send_message", 1)
    with pytest.raises(SimulatedCrash):
        await app.runner.force_reset(GUILD, "monthly")
    app = await restart(app, config, fake, clock)
    await settle(app, clock)
    runs = [r for r in await app.repo.list_runs(GUILD) if r.is_test]
    assert len(runs) == 1 and runs[0].status is RunStatus.DONE
    assert len(fake.announcements(CHANNEL)) == 1
    assert fake.holders_of(GUILD, ROLE_MONTHLY) == {11}
    await assert_converged(app, fake)
    await app.shutdown()


async def test_force_reset_requires_setup(app: App) -> None:
    with pytest.raises(RuntimeError):
        await app.runner.force_reset(GUILD, "daily")


async def test_unique_constraint_prevents_duplicate_runs(app: App) -> None:
    import sqlite3

    await install(app)
    from bot.periods import period_containing

    p = period_containing("daily", dt(2026, 3, 10))
    assert await app.repo.create_runs(GUILD, [p], app.services.clock.now()) == 1
    assert await app.repo.create_runs(GUILD, [p], app.services.clock.now()) == 0
    with pytest.raises(sqlite3.IntegrityError):
        async with app.services.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO reset_runs (guild_id, period_type, period_key, period_start, period_end, status, "
                "created_at, updated_at) VALUES (?, 'daily', '2026-03-10', 'a', 'b', 'PENDING', 'x', 'x')",
                (GUILD,),
            )


async def test_role_switched_by_admin_mid_transition_unblocks(app: App, fake: FakeDiscord, clock: FakeClock) -> None:
    """A run stuck on an unmanageable role follows /setrole instead of blocking later runs forever."""
    await install(app)
    await app.messages(GUILD, 11, dt(2026, 3, 10, 13), 2)
    fake.fail_next("add_role", OpForbidden("Missing Permissions", code=50013))  # role work started, then failed
    clock.set(dt(2026, 3, 11, 0, 3))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.ANNOUNCED and run.role_started_at is not None and run.role_id == ROLE_DAILY
    fake.guilds[GUILD].roles[ROLE_DAILY].position = 150  # the old role is now unmanageable for good
    new_role = 777
    fake.create_role(GUILD, new_role, position=5)
    await app.repo.set_reward_role(GUILD, "daily", new_role, clock.now())
    clock.set(run.next_attempt_at + timedelta(seconds=1))
    await app.runner.tick()
    run = await run_of(app, "daily", "2026-03-10")
    assert run.status is RunStatus.DONE and run.role_id == new_role
    assert fake.holders_of(GUILD, new_role) == {11}
    # The next day's run is not blocked.
    await app.messages(GUILD, 12, dt(2026, 3, 11, 13), 3)
    clock.set(dt(2026, 3, 12, 0, 3))
    await app.runner.tick()
    assert (await run_of(app, "daily", "2026-03-11")).status is RunStatus.DONE
    assert fake.holders_of(GUILD, new_role) == {12}
