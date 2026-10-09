"""Administrative slash commands (all require Manage Server).

Command callbacks are thin: they validate Discord-side inputs and delegate to
the plain async functions below, which tests call directly.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import discord
from discord import app_commands

from bot.db.repo import ResetRun, RunStatus
from bot.discord_ops import RoleCheck
from bot.periods import PeriodType

if TYPE_CHECKING:
    from bot.main import Services

log = logging.getLogger(__name__)

PeriodChoice = Literal["daily", "weekly", "monthly"]
MAX_URL_LENGTH = 2048
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".arpa")


# ----------------------------------------------------------------- validation


def validate_banner_url(raw: str) -> tuple[str | None, str | None]:
    """Return ``(url, None)`` if acceptable, else ``(None, reason)``.

    Purely syntactic: the bot never fetches the URL (Discord's media proxy
    does). Rejects non-HTTPS, embedded credentials, local/private hosts and
    malformed values.
    """
    url = (raw or "").strip()
    if not url:
        return None, "The URL is empty."
    if len(url) > MAX_URL_LENGTH:
        return None, f"The URL is longer than {MAX_URL_LENGTH} characters."
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None, "The URL must not contain spaces or control characters."
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for an invalid port
    except ValueError:
        return None, "That is not a valid URL."
    if parts.scheme.lower() != "https":
        return None, "Only https:// image URLs are allowed."
    if parts.username is not None or parts.password is not None:
        return None, "The URL must not contain a username or password."
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None, "The URL has no host name."
    if port is not None and port != 443:
        return None, "Only the default HTTPS port is allowed."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            return None, "The URL points to a private or reserved address."
    else:
        if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host:
            return None, "The URL must use a public host name."
    return url, None


# ------------------------------------------------------------------ actions


async def setup_guild(services: "Services", guild_id: int, channel_id: int) -> str:
    now = services.clock.now()
    async with services.locks.get(guild_id):
        before = await services.repo.get_guild_config(guild_id)
        cfg = await services.repo.setup_guild(guild_id, channel_id, now)
        ok = await services.boards.ensure_boards(guild_id)
    assert cfg.installed_at is not None
    first = before is None or before.installed_at is None
    installed = f"<t:{int(cfg.installed_at.timestamp())}:f>"
    head = (
        f"Leaderboard installed in <#{channel_id}>. Counting periods start from {installed}."
        if first
        else f"Leaderboard channel set to <#{channel_id}> (installed {installed}, unchanged)."
    )
    if ok:
        return head + " The four boards are in place."
    return head + " Some boards could not be posted yet; I'll keep retrying automatically. Check my permissions in that channel."


async def exclude_add(services: "Services", guild_id: int, channel_id: int) -> bool:
    added = await services.repo.add_exclusion(guild_id, channel_id, services.clock.now())
    services.exclusions.set_excluded(guild_id, channel_id, True)
    log.info("Exclusion added: guild=%s channel=%s", guild_id, channel_id)
    return added


async def exclude_remove(services: "Services", guild_id: int, channel_id: int) -> bool:
    removed = await services.repo.remove_exclusion(guild_id, channel_id)
    services.exclusions.set_excluded(guild_id, channel_id, False)
    log.info("Exclusion removed: guild=%s channel=%s", guild_id, channel_id)
    return removed


async def set_reward_role(services: "Services", guild_id: int, period: str, role_id: int) -> RoleCheck:
    await services.repo.set_reward_role(guild_id, PeriodType(period), role_id, services.clock.now())
    log.info("Reward role set: guild=%s period=%s role=%s", guild_id, period, role_id)
    return services.ops.check_role_manageable(guild_id, role_id)


async def set_banner(services: "Services", guild_id: int, url: str | None) -> None:
    await services.repo.set_banner(guild_id, url, services.clock.now())
    log.info("Banner %s for guild %s", "set" if url else "cleared", guild_id)
    async with services.locks.get(guild_id):
        await services.boards.update_guild(guild_id)


def describe_forced_run(run: ResetRun) -> str:
    head = f"Forced **test** reset `{run.period_key}` ({run.period_type.value}) — state **{run.status.value}**."
    if run.status is RunStatus.SKIPPED_EMPTY:
        return head + "\nNobody eligible had messages in this period, so no announcement or role change was made."
    if run.status is RunStatus.DONE:
        winner = f"<@{run.winner_user_id}> with {run.winner_count} messages"
        role = f"\nRole step note: {run.last_error}" if run.last_error else ""
        return head + f"\nWinner: {winner}. Announcement and role step completed.{role}"
    if run.status is RunStatus.ROLES_SUPERSEDED:
        return head + "\nA newer run owns the reward role, so this test run did not change it."
    detail = f"\nLast error: {run.last_error}" if run.last_error else ""
    return (
        head
        + "\nThe reset did not finish; it will keep retrying automatically with backoff."
        + detail
    )


# ----------------------------------------------------------------- commands


async def _reply(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def register_admin_commands(tree: app_commands.CommandTree, services: "Services") -> None:
    manage = discord.Permissions(manage_guild=True)

    @tree.command(name="setup", description="Set the leaderboard channel and post the four boards")
    @app_commands.describe(channel="Channel where the leaderboards will be posted")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild = interaction.guild
        assert guild is not None
        me = guild.me
        if me is None:
            await _reply(interaction, "I can't see my own membership in this server yet; try again in a minute.")
            return
        perms = channel.permissions_for(me)
        needed = {
            "View Channel": perms.view_channel,
            "Send Messages": perms.send_messages,
            "Embed Links": perms.embed_links,
            "Read Message History": perms.read_message_history,
        }
        missing = [name for name, ok in needed.items() if not ok]
        if missing:
            await _reply(interaction, f"I can't post boards in {channel.mention}: missing {', '.join(missing)}.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _reply(interaction, await setup_guild(services, guild.id, channel.id))

    exclude = app_commands.Group(
        name="exclude",
        description="Channels and categories whose messages do not count",
        guild_only=True,
        default_permissions=manage,
    )

    @exclude.command(name="add", description="Stop counting messages in a channel or category")
    @app_commands.describe(target="Channel, forum or category to exclude")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def exclude_add_cmd(interaction: discord.Interaction, target: discord.abc.GuildChannel) -> None:
        assert interaction.guild_id is not None
        if target.guild.id != interaction.guild_id:
            await _reply(interaction, "That channel belongs to a different server.")
            return
        added = await exclude_add(services, interaction.guild_id, target.id)
        kind = "Category" if isinstance(target, discord.CategoryChannel) else "Channel"
        state = "is now excluded" if added else "was already excluded"
        await _reply(interaction, f"{kind} {target.mention} {state}. Threads and forum posts inside it are excluded too.")

    @exclude.command(name="remove", description="Count messages in a channel or category again")
    @app_commands.describe(target="Channel, forum or category to include again")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def exclude_remove_cmd(interaction: discord.Interaction, target: discord.abc.GuildChannel) -> None:
        assert interaction.guild_id is not None
        removed = await exclude_remove(services, interaction.guild_id, target.id)
        state = "is no longer excluded" if removed else "was not excluded"
        await _reply(interaction, f"{target.mention} {state}.")

    @exclude.command(name="list", description="List excluded channels and categories")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def exclude_list_cmd(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        ids = await services.repo.list_exclusions(interaction.guild_id)
        if not ids:
            await _reply(interaction, "No channels or categories are excluded.")
            return
        guild = interaction.guild
        lines = []
        for channel_id in ids:
            channel = guild.get_channel(channel_id) if guild else None
            if channel is None:
                lines.append(f"• `{channel_id}` (deleted or unknown channel)")
            elif isinstance(channel, discord.CategoryChannel):
                lines.append(f"• Category **{discord.utils.escape_markdown(channel.name)}** (`{channel_id}`)")
            else:
                lines.append(f"• {channel.mention} (`{channel_id}`)")
        text = "Excluded:\n" + "\n".join(lines)
        await _reply(interaction, text[:1990])

    tree.add_command(exclude)

    @tree.command(name="setrole", description="Set the reward role for a period")
    @app_commands.describe(period="Which period's winner receives the role", role="The reward role")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setrole_cmd(interaction: discord.Interaction, period: PeriodChoice, role: discord.Role) -> None:
        guild = interaction.guild
        assert guild is not None
        if role.guild.id != guild.id:
            await _reply(interaction, "That role belongs to a different server.")
            return
        if role.is_default():
            await _reply(interaction, "@everyone can't be used as a reward role.")
            return
        if role.managed:
            await _reply(interaction, f"{role.mention} is managed by an integration and can't be assigned.")
            return
        check = await set_reward_role(services, guild.id, period, role.id)
        message = f"The {period} reward role is now {role.mention}."
        if not check.ok:
            message += f"\n**Warning:** I can't manage this role right now — {check.reason}. Role changes will fail until this is fixed."
        await _reply(interaction, message)

    banner = app_commands.Group(
        name="setbanner",
        description="Configure the banner image shown on the boards",
        guild_only=True,
        default_permissions=manage,
    )

    @banner.command(name="set", description="Set the banner image URL (https)")
    @app_commands.describe(url="Direct https:// link to the image")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def banner_set_cmd(interaction: discord.Interaction, url: str) -> None:
        assert interaction.guild_id is not None
        clean, error = validate_banner_url(url)
        if error is not None:
            await _reply(interaction, f"Banner not changed: {error}")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await set_banner(services, interaction.guild_id, clean)
        await _reply(interaction, "Banner updated. If Discord can't load the image, the boards fall back to no banner.")

    @banner.command(name="clear", description="Remove the banner image")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def banner_clear_cmd(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await set_banner(services, interaction.guild_id, None)
        await _reply(interaction, "Banner removed.")

    tree.add_command(banner)

    @tree.command(name="forcereset", description="TEST: run the reset flow now on a synthetic test period")
    @app_commands.describe(period="Which reset to simulate")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def forcereset_cmd(interaction: discord.Interaction, period: PeriodChoice) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            run = await services.runner.force_reset(interaction.guild_id, period)
        except RuntimeError as exc:
            await _reply(interaction, f"Forced test reset failed: {exc}")
            return
        await _reply(interaction, describe_forced_run(run))

    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission to use this command."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use this command here."
        else:
            command = interaction.command.qualified_name if interaction.command else "?"
            log.error("Slash command /%s failed in guild %s", command, interaction.guild_id, exc_info=error)
            message = "Something went wrong while running that command. The error has been logged."
        try:
            await _reply(interaction, message)
        except discord.HTTPException:
            log.warning("Could not deliver the error message for an interaction")
