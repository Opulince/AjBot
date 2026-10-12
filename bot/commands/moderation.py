"""Moderation slash commands.

Each command is hidden from members without the matching permission
(``default_permissions``) *and* checks it at runtime, because server admins can
override command visibility per role in Integrations settings. The bot's own
permissions are checked up front too. Responses are ephemeral. The callbacks
are thin: the logic and every safety rule live in :mod:`bot.moderation`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

import discord
from discord import app_commands

from bot import moderation as mod
from bot.commands.admin import _reply

if TYPE_CHECKING:
    from bot.main import Services

log = logging.getLogger(__name__)

Reason = app_commands.Range[str, 1, mod.REASON_MAX]
ResetPeriod = Literal["daily", "weekly", "monthly", "alltime"]
CONFIRM_TIMEOUT = 60.0


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


class ConfirmView(discord.ui.View):
    """Confirm / Cancel for a destructive action; only the moderator who asked can press it."""

    def __init__(self, invoker_id: int, action: Callable[[], Awaitable[str]], *, timeout: float = CONFIRM_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the moderator who asked can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reset counts", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Resetting…", view=None)
        try:
            message = await self.action()
        except mod.ModError as exc:
            message = str(exc)
        await interaction.edit_original_response(content=message)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was reset.", view=None)


# ------------------------------------------------------------------ modals


def _value(field: discord.ui.Label) -> str:
    component = field.component
    assert isinstance(component, discord.ui.TextInput)
    return component.value


class _ModModal(discord.ui.Modal):
    """Base for the context-menu forms: errors are reported to the moderator, not swallowed."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        log.error("Moderation form %s failed in guild %s", type(self).__name__, interaction.guild_id, exc_info=error)
        try:
            await _reply(interaction, "Something went wrong while running that action. The error has been logged.")
        except discord.HTTPException:
            log.warning("Could not deliver the error message for a moderation form")


def _reason_input(*, required: bool = True) -> discord.ui.TextInput:
    return discord.ui.TextInput(style=discord.TextStyle.paragraph, required=required, max_length=mod.REASON_MAX)


class WarnModal(_ModModal, title="Warn member"):
    reason: discord.ui.Label = discord.ui.Label(
        text="Reason", description="Shown to them (by DM) and in the mod log.", component=_reason_input()
    )

    def __init__(self, services: "Services", target_id: int) -> None:
        super().__init__()
        self.services = services
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        guild = interaction.guild
        assert guild is not None
        reason = _value(self.reason)
        await run_action(
            interaction, lambda: mod.warn(self.services, guild.id, guild.name, actor_of(interaction), self.target_id, reason)
        )


class TimeoutModal(_ModModal, title="Time out member"):
    duration: discord.ui.Label = discord.ui.Label(
        text="Duration", description="30s, 10m, 2h, 7d or 1h30m (at most 28d).",
        component=discord.ui.TextInput(placeholder="10m", max_length=32),
    )
    reason: discord.ui.Label = discord.ui.Label(
        text="Reason", description="Optional. Shown in the mod log and the Audit Log.",
        component=_reason_input(required=False),
    )

    def __init__(self, services: "Services", target_id: int) -> None:
        super().__init__()
        self.services = services
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        try:
            length = mod.parse_duration(_value(self.duration))
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        reason = _value(self.reason) or None
        await run_action(
            interaction,
            lambda: mod.timeout(self.services, guild_id, actor_of(interaction), self.target_id, length, reason),
        )


class DeleteWarnModal(_ModModal, title="Delete message and warn"):
    reason: discord.ui.Label = discord.ui.Label(
        text="Reason", description="Shown to them (by DM) and in the mod log.", component=_reason_input()
    )

    def __init__(self, services: "Services", message: discord.Message) -> None:
        super().__init__()
        self.services = services
        self.channel_id = message.channel.id
        self.message_id = message.id
        self.author_id = message.author.id
        self.content = message.content

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        guild = interaction.guild
        assert guild is not None
        reason = _value(self.reason)
        await run_action(
            interaction,
            lambda: mod.delete_and_warn(
                self.services, guild.id, guild.name, actor_of(interaction), channel_id=self.channel_id,
                message_id=self.message_id, author_id=self.author_id, content=self.content, reason=reason,
            ),
        )


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

    # /mute and /unmute are the same as /timeout and /untimeout (Discord's timeout is its mute).

    @tree.command(name="mute", description="Mute a member (a Discord timeout: no chat, reactions or voice)")
    @app_commands.describe(
        user="Member to mute",
        duration="How long: 30s, 10m, 2h, 7d, 1h30m … (max 28d)",
        reason="Shown in the mod log and the Audit Log",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def mute_cmd(
        interaction: discord.Interaction, user: discord.Member, duration: str, reason: Optional[Reason] = None
    ) -> None:
        await timeout_cmd.callback(interaction, user, duration, reason)

    @tree.command(name="unmute", description="Unmute a member (ends their timeout)")
    @app_commands.describe(user="Member to unmute", reason="Shown in the mod log and the Audit Log")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def unmute_cmd(interaction: discord.Interaction, user: discord.Member, reason: Optional[Reason] = None) -> None:
        await untimeout_cmd.callback(interaction, user, reason)

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

    @tree.command(name="ban", description="Ban a user, optionally for a limited time")
    @app_commands.describe(
        user="User to ban",
        reason="Shown in the mod log and the Audit Log",
        delete_message_days="Also delete their messages from the last 0-7 days (default 0)",
        duration="Temporary ban: 1h, 7d, 30d … up to 365d (leave empty for a permanent ban)",
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
        duration: Optional[app_commands.Range[str, 1, 32]] = None,
    ) -> None:
        assert interaction.guild_id is not None
        try:
            length = mod.parse_duration(duration) if duration else None
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        await run_action(
            interaction,
            lambda: mod.ban(
                services, interaction.guild_id, actor_of(interaction), user.id, reason,
                delete_message_days=delete_message_days, duration=length,
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

    # ----------------------------------------------------- warn escalation

    warnconfig = app_commands.Group(
        name="warnconfig",
        description="Automatic timeouts when a member reaches a number of active warnings",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @warnconfig.command(name="add", description="Time members out automatically at a warning count")
    @app_commands.describe(count="Active warnings that trigger it", duration="Timeout length: 10m, 2h, 1d … (max 28d)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def warnconfig_add_cmd(
        interaction: discord.Interaction, count: app_commands.Range[int, 1, 100], duration: str
    ) -> None:
        assert interaction.guild_id is not None
        try:
            message = await mod.add_warn_threshold(services, interaction.guild_id, count, mod.parse_duration(duration))
        except mod.ModError as exc:
            message = str(exc)
        await _reply(interaction, message)

    @warnconfig.command(name="remove", description="Remove the automatic timeout for a warning count")
    @app_commands.describe(count="The warning count of the rule to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def warnconfig_remove_cmd(interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]) -> None:
        assert interaction.guild_id is not None
        try:
            message = await mod.remove_warn_threshold(services, interaction.guild_id, count)
        except mod.ModError as exc:
            message = str(exc)
        await _reply(interaction, message)

    @warnconfig.command(name="list", description="List the automatic timeouts")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def warnconfig_list_cmd(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await _reply(interaction, await mod.list_warn_thresholds(services, interaction.guild_id))

    tree.add_command(warnconfig)

    # --------------------------------------------------------------- cases

    case = app_commands.Group(
        name="case",
        description="Look up or correct moderation cases",
        guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    @case.command(name="view", description="Show a moderation case")
    @app_commands.describe(case_id="Case number")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case_view_cmd(interaction: discord.Interaction, case_id: app_commands.Range[int, 1]) -> None:
        assert interaction.guild_id is not None
        try:
            embed = await mod.view_case(services, interaction.guild_id, case_id)
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @case.command(name="reason", description="Change a case's reason (also edits its mod-log entry)")
    @app_commands.describe(case_id="Case number", reason="The new reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case_reason_cmd(
        interaction: discord.Interaction, case_id: app_commands.Range[int, 1], reason: Reason
    ) -> None:
        assert interaction.guild_id is not None
        await run_action(interaction, lambda: mod.edit_case_reason(services, interaction.guild_id, case_id, reason))

    tree.add_command(case)

    # ---------------------------------------------- leaderboard moderation

    @tree.command(name="lbban", description="Ban a user from the leaderboard (hidden, can't win, not counted)")
    @app_commands.describe(user="User to remove from the leaderboard", reason="Shown in the mod log")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lbban_cmd(interaction: discord.Interaction, user: discord.User, reason: Optional[Reason] = None) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.lb_ban(services, interaction.guild_id, actor_of(interaction), user.id, reason)
        )

    @tree.command(name="lbunban", description="Put a user back on the leaderboard (their earlier counts return)")
    @app_commands.describe(user="User to restore", reason="Shown in the mod log")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lbunban_cmd(interaction: discord.Interaction, user: discord.User, reason: Optional[Reason] = None) -> None:
        assert interaction.guild_id is not None
        await run_action(
            interaction, lambda: mod.lb_unban(services, interaction.guild_id, actor_of(interaction), user.id, reason)
        )

    @tree.command(name="lbbans", description="List users banned from the leaderboard")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lbbans_cmd(interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await _reply(interaction, await mod.list_lb_bans(services, interaction.guild_id))

    @tree.command(name="resetcounts", description="Delete a user's counted messages for the current period")
    @app_commands.describe(user="Whose counts to reset", period="The current day, week or month, or all time")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resetcounts_cmd(interaction: discord.Interaction, user: discord.User, period: ResetPeriod) -> None:
        assert interaction.guild_id is not None
        guild_id = interaction.guild_id
        actor = actor_of(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            label = await mod.check_reset_counts(services, guild_id, actor, user.id, period)
        except mod.ModError as exc:
            await _reply(interaction, str(exc))
            return
        view = ConfirmView(actor.user_id, lambda: mod.reset_counts(services, guild_id, actor, user.id, period))
        await interaction.followup.send(
            f"Reset <@{user.id}>'s **{label}** count? Their counted messages in that range are deleted, which also "
            "lowers every board that includes those days (a weekly reset lowers their monthly and all-time totals "
            "too). This can't be undone.",
            view=view,
            ephemeral=True,
        )

    @tree.command(name="countcooldown", description="Minimum seconds between two counted messages of one user")
    @app_commands.describe(seconds="0 turns it off (default); up to 60")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def countcooldown_cmd(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 60]) -> None:
        assert interaction.guild_id is not None
        await _reply(interaction, await mod.set_count_cooldown(services, interaction.guild_id, seconds))

    # ------------------------------------------------------- context menus
    # Discord allows 5 user and 5 message context menus per app; this adds 2 + 1.
    # The decorators below the context_menu one are read when the menu is created:
    # default_permissions hides it, the checks re-verify at runtime.

    @tree.context_menu(name="Warn")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_menu(interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.send_modal(WarnModal(services, member.id))

    @tree.context_menu(name="Timeout")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout_menu(interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.send_modal(TimeoutModal(services, member.id))

    @tree.context_menu(name="Delete + warn")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True, manage_messages=True)
    @app_commands.checks.has_permissions(moderate_members=True, manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def delete_warn_menu(interaction: discord.Interaction, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != interaction.guild_id:
            await _reply(interaction, "That message isn't from this server.")
            return
        await interaction.response.send_modal(DeleteWarnModal(services, message))
