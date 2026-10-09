"""Entry point: startup, setup_hook, background task lifecycle and graceful shutdown.

Run with ``python -m bot.main``. Exit codes: 0 clean shutdown, 1 unexpected
crash, 2 invalid configuration, 3 Discord rejected the token.

Shutdown order (SIGINT / SIGTERM or a fatal error):
1. stop accepting new message work,
2. stop the scheduler and board-update tasks,
3. disconnect from Discord,
4. flush buffered counts (with retries),
5. close the database.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

from bot.boards import BoardService
from bot.clock import Clock, SystemClock
from bot.commands.admin import register_admin_commands
from bot.commands.stats import register_stats_command
from bot.config import Config, ConfigError, load_config
from bot.counting.buffer import CountBuffer
from bot.counting.filter import ChannelResolver, ExclusionCache, MessageCounter
from bot.db.database import Database
from bot.db.repo import Repo
from bot.discord_ops import DiscordOps, DiscordPyOps
from bot.ranking import Ranker
from bot.resets.machine import ResetMachine
from bot.resets.runner import TICK_SECONDS, GuildLocks, ResetRunner
from bot.ui.views import register_persistent_items

log = logging.getLogger("bot")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
SHUTDOWN_TASK_TIMEOUT = 15.0
# Exit codes that a supervisor should not restart on (nothing changes until a human fixes them).
EXIT_CONFIG_ERROR = 2
EXIT_BAD_TOKEN = 3
FINAL_FLUSH_ATTEMPTS = 5


# -------------------------------------------------------------------- logging


class SecretRedactor(logging.Filter):
    """Defence in depth: scrub secrets from any log record that might contain them."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(config: Config) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    redactor = SecretRedactor([config.token])

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(redactor)

    file_handler = RotatingFileHandler(
        config.log_dir / "leaderboard.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(config.log_level)
    root.addHandler(console)
    root.addHandler(file_handler)
    # discord.py's gateway logging is chatty at DEBUG; keep it at INFO unless debugging.
    logging.getLogger("discord").setLevel(logging.INFO if config.log_level != "DEBUG" else logging.DEBUG)


# ------------------------------------------------------------------- services


@dataclass
class Services:
    config: Config
    clock: Clock
    db: Database
    repo: Repo
    ops: DiscordOps
    buffer: CountBuffer
    exclusions: ExclusionCache
    counter: MessageCounter
    ranker: Ranker
    boards: BoardService
    machine: ResetMachine
    runner: ResetRunner
    locks: GuildLocks


def build_services(config: Config, db: Database, ops: DiscordOps, clock: Clock) -> Services:
    """Wire the application together. Tests use this with a fake ``ops`` and clock."""
    repo = Repo(db)
    buffer = CountBuffer(repo, flush_interval=config.flush_interval_seconds)
    exclusions = ExclusionCache(repo)
    counter = MessageCounter(exclusions, ChannelResolver(ops), buffer)
    ranker = Ranker(repo)
    boards = BoardService(repo, ranker, ops, clock)
    machine = ResetMachine(repo, ops, buffer, ranker, clock, grace_seconds=config.reset_grace_seconds)
    locks = GuildLocks()
    runner = ResetRunner(repo, machine, boards, clock, locks)
    return Services(
        config=config,
        clock=clock,
        db=db,
        repo=repo,
        ops=ops,
        buffer=buffer,
        exclusions=exclusions,
        counter=counter,
        ranker=ranker,
        boards=boards,
        machine=machine,
        runner=runner,
        locks=locks,
    )


def make_intents() -> discord.Intents:
    """Only the two non-privileged intents the bot needs."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    return intents


# ------------------------------------------------------------------------ bot


class LeaderboardBot(discord.Client):
    def __init__(self, config: Config, *, clock: Clock | None = None) -> None:
        # No voice features are used; skip discord.py's "voice will NOT be supported" warnings.
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        super().__init__(
            intents=make_intents(),
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,  # no message cache needed
        )
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.db_path)
        self.services: Services | None = None
        self._stop = asyncio.Event()
        self._closing = False
        self._closed_cleanly = False
        self._flush_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._board_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Future | None = None

    async def setup_hook(self) -> None:
        await self.db.connect()
        services = build_services(self.config, self.db, DiscordPyOps(self), self.clock)
        self.services = services
        await services.exclusions.load()
        register_persistent_items(self)
        register_admin_commands(self.tree, services)
        register_stats_command(self.tree, services)
        try:
            synced = await self.tree.sync()
            log.info("Synced %d application commands", len(synced))
        except discord.HTTPException as exc:
            log.error("Failed to sync application commands: %s", exc)
        self._flush_task = self._spawn(services.buffer.run(self._stop), "count-flush")
        self._scheduler_task = self._spawn(self._scheduler_loop(), "reset-scheduler")
        self._board_task = self._spawn(self._board_loop(), "board-updates")

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._report_task_end)
        return task

    def _report_task_end(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.critical("Background task %s died unexpectedly", task.get_name(), exc_info=exc)
        elif not self._closing:
            log.error("Background task %s exited before shutdown", task.get_name())

    async def on_ready(self) -> None:
        log.info("Connected as %s (id=%s) in %d guilds", self.user, self.user.id if self.user else "?", len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        if self._closing or self.services is None:
            return
        try:
            await self.services.counter.handle(message)
        except Exception:
            log.exception("Failed to process message %s", message.id)

    async def _scheduler_loop(self) -> None:
        await self.wait_until_ready()
        assert self.services is not None
        log.info("Reset scheduler started (startup catch-up runs now)")
        await self.services.runner.run(self._stop, interval=TICK_SECONDS)

    async def _board_loop(self) -> None:
        await self.wait_until_ready()
        services = self.services
        assert services is not None
        while not self._stop.is_set():
            try:
                await services.buffer.flush()  # show the freshest persisted counts
                for cfg in await services.repo.list_installed_guilds():
                    try:
                        async with services.locks.get(cfg.guild_id):
                            await services.boards.update_guild(cfg.guild_id)
                    except Exception:
                        log.exception("Board update failed for guild %s", cfg.guild_id)
            except Exception:
                log.exception("Board update cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.board_update_seconds)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        """Idempotent full shutdown; every caller waits until it has completed.

        discord.py's own ``__aexit__`` only waits for the gateway part of
        closing, so the final flush and database close live in one shared task
        that ``run_bot`` awaits explicitly.
        """
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        self._closing = True
        log.info("Shutting down: stopping background tasks")
        self._stop.set()
        for task in (self._scheduler_task, self._board_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [t for t in (self._scheduler_task, self._board_task, self._flush_task) if t is not None]
        if pending:
            _, not_done = await asyncio.wait(pending, timeout=SHUTDOWN_TASK_TIMEOUT)
            for task in not_done:
                log.warning("Task %s did not stop in time", task.get_name())
        try:
            await super().close()
        except Exception:
            log.exception("Error while disconnecting from Discord")
        await self._final_flush()
        await self.db.close()
        log.info("Shutdown complete")

    async def _final_flush(self) -> None:
        if self.services is None or not self.db.connected:
            return
        buffer = self.services.buffer
        for attempt in range(1, FINAL_FLUSH_ATTEMPTS + 1):
            if await buffer.flush():
                log.info("Final flush complete")
                self._closed_cleanly = True
                return
            log.error("Final flush attempt %d/%d failed", attempt, FINAL_FLUSH_ATTEMPTS)
            await asyncio.sleep(min(2 ** attempt, 10))
        log.error("Shutting down with %d buffered messages that could not be persisted", buffer.pending_messages)


# ---------------------------------------------------------------------- main


async def run_bot(config: Config) -> int:
    bot = LeaderboardBot(config)
    loop = asyncio.get_running_loop()

    def request_shutdown(sig: signal.Signals) -> None:
        log.info("Received %s", sig.name)
        asyncio.ensure_future(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - e.g. Windows
            pass

    try:
        async with bot:
            await bot.start(config.token)
    except discord.LoginFailure:
        log.error("Discord rejected the token (DISCORD_TOKEN is invalid)")
        return EXIT_BAD_TOKEN
    except discord.PrivilegedIntentsRequired:  # pragma: no cover - we request none
        log.error("Discord refused the requested intents")
        return EXIT_BAD_TOKEN
    finally:
        await bot.close()  # waits for the final flush and database close
    return 0


def main() -> int:
    load_dotenv()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error.\n{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    setup_logging(config)
    log.info("Starting leaderboard bot (db=%s, logs=%s)", config.db_path, config.log_dir)
    try:
        return asyncio.run(run_bot(config))
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        return 0
    except Exception:
        log.exception("Fatal error; the bot stopped")
        return 1


if __name__ == "__main__":
    sys.exit(main())
