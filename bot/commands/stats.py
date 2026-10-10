"""Public ``/stats`` command (ephemeral response).

Ranks come from :meth:`bot.ranking.Ranker.standing`, which uses exactly the same
ordering function as the boards, so a user's rank here always matches their
position on the corresponding board.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from bot.periods import BOARD_ORDER
from bot.ranking import Standing
from bot.ui.embeds import build_stats_embed

if TYPE_CHECKING:
    from bot.main import Services


async def user_standings(services: "Services", guild_id: int, user_id: int) -> dict[str, Standing]:
    now = services.clock.now()
    return {t: await services.ranker.standing(guild_id, t, now, user_id) for t in BOARD_ORDER}


def register_stats_command(tree: app_commands.CommandTree, services: "Services") -> None:
    @tree.command(name="stats", description="Show message counts and ranks")
    @app_commands.describe(user="Member to look up (defaults to you)")
    @app_commands.guild_only()
    async def stats_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
        assert interaction.guild_id is not None
        target = user or interaction.user
        standings = await user_standings(services, interaction.guild_id, target.id)
        embed = build_stats_embed(
            user_id=target.id,
            name=getattr(target, "display_name", None) or target.name,
            standings=standings,
            now=services.clock.now(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
