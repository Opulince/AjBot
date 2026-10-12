"""Moderation slash commands.

Each command is hidden from members without the matching permission
(``default_permissions``) *and* checks it at runtime, because server admins can
override command visibility per role in Integrations settings. The bot's own
permissions are checked up front too. Responses are ephemeral. The callbacks
are thin: the logic and every safety rule live in :mod:`bot.moderation`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord
from discord import app_commands

from bot import moderation as mod
from bot.commands.admin import _reply

if TYPE_CHECKING:
    from bot.main import Services

log = logging.getLogger(__name__)

Reason = app_commands.Range[str, 1, mod.REASON_MAX]


def actor_of(interaction: discord.Interaction) -> mod.Actor:
    return mod.Actor(interaction.user.id, interaction.user.name)


async def run_action(interaction: discord.Interaction, action: Callable[[], Awaitable[str]]) -> None:
    """Defer (the checks and the action take several API calls), run, and reply ephemerally."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        message = await action()
    except mod.ModError as exc:
        message = str(exc)
    await _reply(interaction, message)


def register_moderation_commands(tree: app_commands.CommandTree, services: "Services") -> None:
    # ------------------------------------------------------------ timeouts

    @tree.command(name="timeout", description="Time a member out (they can't chat, react or join voice)")
    @app_commands.describe(
        user="Member to time out",
        duration="How long: 30s, 10m, 2h, 7d, 1h30m … (max 28d)",
        reason="Shown in the mod log and the Audit Log",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout_cmd(
        interaction: discord.Interaction, user: discord.Member, duration: str, reason: Optional[Reason] = None
    ) -> None:
        assert interaction.guild_id is not None
        try:
            length = mod.parse_duration(duration)
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        await run_action(
            interaction,
            lambda: mod.timeout(services, interaction.guild_id, actor_of(interaction), user.id, length, reason),
        )

    @tree.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(user="Member whose timeout ends now", reason="Shown in the mod log and the Audit Log")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout_cmd(interaction: discord.Interaction, user: discord.Member, reason: Optional[Reason] = None) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.untimeout(services, interaction.guild_id, actor_of(interaction), user.id, reason)
        )

    # ------------------------------------------------------------ warnings

    @tree.command(name="warn", description="Warn a member (recorded as a case; they get a DM if possible)")
    @app_commands.describe(user="Member to warn", reason="Why (shown to them and in the mod log)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: Reason) -> None:
        guild = interaction.guild
        assert guild is not None
        await run_action(
            interaction, lambda: mod.warn(services, guild.id, guild.name, actor_of(interaction), user.id, reason)
        )

    @tree.command(name="warnings", description="List a user's active warnings")
    @app_commands.describe(user="Whose warnings to show")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_cmd(interaction: discord.Interaction, user: discord.User) -> None:
        assert interaction.guild_id is not None
        await _reply(interaction, await mod.list_warnings(services, interaction.guild_id, user.id))

    @tree.command(name="clearwarn", description="Clear one warning (the case stays in the history)")
    @app_commands.describe(case_id="Case number of the warning")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarn_cmd(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.clear_warning(services, interaction.guild_id, actor_of(interaction), case_id)
        )

    @tree.command(name="clearwarns", description="Clear all of a user's warnings (the cases stay in the history)")
    @app_commands.describe(user="Whose warnings to clear")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarns_cmd(interaction: discord.Interaction, user: discord.User) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.clear_warnings(services, interaction.guild_id, actor_of(interaction), user.id)
        )

    # -------------------------------------------------------- kick and ban

    @tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(user="Member to kick", reason="Shown in the mod log and the Audit Log")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick_cmd(interaction: discord.Interaction, user: discord.Member, reason: Optional[Reason] = None) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.kick(services, interaction.guild_id, actor_of(interaction), user.id, reason)
        )

    @tree.command(name="ban", description="Ban a user (they don't have to be in the server)")
    @app_commands.describe(
        user="User to ban",
        reason="Shown in the mod log and the Audit Log",
        delete_message_days="Also delete their messages from the last 0-7 days (default 0)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban_cmd(
        interaction: discord.Interaction,
        user: discord.User,
        reason: Optional[Reason] = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction,
            lambda: mod.ban(
                services, interaction.guild_id, actor_of(interaction), user.id, reason,
                delete_message_days=delete_message_days,
            ),
        )

    @tree.command(name="unban", description="Lift a ban")
    @app_commands.describe(user_id="ID of the banned user", reason="Shown in the mod log and the Audit Log")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban_cmd(
        interaction: discord.Interaction, user_id: app_commands.Range[str, 1, 32], reason: Optional[Reason] = None
    ) -> None:
        assert interaction.guild_id is not None
        try:
            target = mod.parse_user_id(user_id)
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        await run_action(
            interaction, lambda: mod.unban(services, interaction.guild_id, actor_of(interaction), target, reason)
        )

    # --------------------------------------------------------------- purge

    @tree.command(name="purge", description="Bulk-delete recent messages in this channel")
    @app_commands.describe(
        amount="How many of the newest messages to scan (1-100)",
        user="Only delete this user's messages among them",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True, read_message_history=True)
    @app_commands.checks.has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge_cmd(
        interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], user: Optional[discord.User] = None
    ) -> None:
        assert interaction.guild_id is not None and interaction.channel_id is not None
        channel_id = interaction.channel_id
        await run_action(
            interaction,
            lambda: mod.purge(
                services, interaction.guild_id, actor_of(interaction), channel_id, amount, user.id if user else None
            ),
        )

    # ------------------------------------------------------------- mod log

    modlog = app_commands.Group(
        name="modlog",
        description="Where moderation actions are logged",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @modlog.command(name="set", description="Log moderation actions in a channel")
    @app_commands.describe(channel="Channel for the mod log (keep it staff-only)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_set_cmd(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild = interaction.guild
        assert guild is not None
        if channel.guild.id != guild.id:
            await _reply(interaction, "That channel belongs to a different server.")
            return
        me = guild.me
        if me is None:
            await _reply(interaction, "I can't see my own membership in this server yet; try again in a minute.")
            return
        perms = channel.permissions_for(me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        if missing:
            await _reply(interaction, f"I can't post in {channel.mention}: missing {', '.join(missing)}.")
            return
        await _reply(interaction, await mod.set_modlog(services, guild.id, channel.id))

    @modlog.command(name="clear", description="Stop logging moderation actions (cases are still recorded)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def modlog_clear_cmd(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await _reply(interaction, await mod.set_modlog(services, interaction.guild_id, None))

    tree.add_command(modlog)
