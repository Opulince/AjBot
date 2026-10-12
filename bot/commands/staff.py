"""Staff-role gate: only members with one of the configured roles may use the bot's commands.

It is an extra runtime check on top of each command's Discord permission
check (``STAFF_ROLE_IDS``), added to every slash command, subcommand and
context menu except the public ``/stats``. The server owner always passes, so
a typo in the role list can't lock the server out. An empty list turns the
gate off.
"""

from __future__ import annotations

from typing import Any, Collection

import discord
from discord import app_commands

PUBLIC_COMMANDS = frozenset({"stats"})


class NotStaff(app_commands.CheckFailure):
    """The member has none of the staff roles."""


def is_staff(user: Any, guild_owner_id: int | None, staff_role_ids: Collection[int]) -> bool:
    if not staff_role_ids:
        return True
    if guild_owner_id is not None and user.id == guild_owner_id:
        return True
    role_ids = {role.id for role in getattr(user, "roles", ())}
    return not role_ids.isdisjoint(staff_role_ids)


def staff_check(staff_role_ids: Collection[int]) -> Any:
    roles = frozenset(staff_role_ids)

    def predicate(interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if is_staff(interaction.user, guild.owner_id if guild is not None else None, roles):
            return True
        raise NotStaff("not a staff member")

    return predicate


def apply_staff_gate(
    tree: app_commands.CommandTree, staff_role_ids: Collection[int], *, public: Collection[str] = PUBLIC_COMMANDS
) -> int:
    """Add the staff check to every registered command except ``public`` ones; returns how many were gated."""
    if not staff_role_ids:
        return 0
    check = staff_check(staff_role_ids)
    gated = 0
    for command_type in (discord.AppCommandType.chat_input, discord.AppCommandType.user, discord.AppCommandType.message):
        for command in tree.get_commands(type=command_type):
            if command.name in public:
                continue
            if isinstance(command, app_commands.Group):
                for leaf in command.walk_commands():
                    if isinstance(leaf, app_commands.Command):
                        leaf.add_check(check)
                        gated += 1
            else:
                command.add_check(check)
                gated += 1
    return gated
