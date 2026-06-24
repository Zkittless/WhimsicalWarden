"""
cogs/leveling.py — XP and leveling system
XP gain with cooldown, level calculation, level-up messages,
role rewards with stacking, leaderboard, rank card, setlevel/setxp,
ignore channels/roles.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from utils import (
    success_embed, error_embed, info_embed,
    utcnow, safe_send, resolve_variables, build_pages, Paginator,
    COL_BLUE, COL_YELLOW,
)

log = logging.getLogger("modbot.leveling")


def xp_for_level(level: int) -> int:
    """XP required to reach a given level (cumulative)."""
    return 5 * (level ** 2) + 50 * level + 100


def level_from_xp(xp: int) -> int:
    """Calculate level from total XP."""
    level = 0
    while xp >= xp_for_level(level):
        xp -= xp_for_level(level)
        level += 1
    return level


def xp_into_level(xp: int) -> tuple[int, int, int]:
    """Returns (current_level, xp_in_current_level, xp_needed_for_next_level)."""
    level = 0
    remaining = xp
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return level, remaining, xp_for_level(level)


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_config(self, guild_id: int) -> dict:
        async with self.bot.db.execute(
            "SELECT * FROM leveling_config WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def is_ignored(self, guild_id: int, member: discord.Member, channel_id: int) -> bool:
        ids = [channel_id] + [r.id for r in member.roles]
        placeholders = ",".join("?" * len(ids))
        async with self.bot.db.execute(
            f"SELECT 1 FROM level_ignore WHERE guild_id=? AND target_id IN ({placeholders}) LIMIT 1",
            (guild_id, *ids),
        ) as cur:
            return await cur.fetchone() is not None

    # ── XP gain on message ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        cfg = await self.get_config(message.guild.id)
        if not cfg or not cfg.get("enabled"):
            return

        if await self.is_ignored(message.guild.id, message.author, message.channel.id):
            return

        user_id  = message.author.id
        guild_id = message.guild.id
        now      = utcnow()

        # Fetch current XP data
        async with self.bot.db.execute(
            "SELECT xp, level, last_xp FROM user_xp WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()

        cooldown = cfg.get("xp_cooldown", 60)
        if row and now - row["last_xp"] < cooldown:
            return  # On cooldown

        # Calculate XP gain
        xp_min  = cfg.get("xp_min", 15)
        xp_max  = cfg.get("xp_max", 25)
        xp_rate = cfg.get("xp_rate", 1.0)
        gained  = int(random.randint(xp_min, xp_max) * xp_rate)

        old_xp    = row["xp"] if row else 0
        old_level = row["level"] if row else 0
        new_xp    = old_xp + gained
        new_level = level_from_xp(new_xp)

        await self.bot.db.execute(
            """INSERT INTO user_xp (guild_id, user_id, xp, level, last_xp)
               VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?, last_xp=?""",
            (guild_id, user_id, new_xp, new_level, now, new_xp, new_level, now),
        )
        await self.bot.db.commit()

        if new_level > old_level:
            await self._on_level_up(message, new_level, cfg)

    async def _on_level_up(self, message: discord.Message, new_level: int, cfg: dict):
        """Handle level-up actions: message, role rewards."""
        guild  = message.guild
        member = message.author

        # Apply role rewards
        async with self.bot.db.execute(
            "SELECT role_id FROM level_rewards WHERE guild_id=? AND level<=? ORDER BY level DESC",
            (guild.id, new_level),
        ) as cur:
            rewards = await cur.fetchall()

        stack = cfg.get("stack_roles", 1)
        if not stack:
            # Remove previous reward roles, add only the highest matching one
            async with self.bot.db.execute(
                "SELECT role_id FROM level_rewards WHERE guild_id=?", (guild.id,)
            ) as cur:
                all_rewards = await cur.fetchall()
            all_reward_ids = {r["role_id"] for r in all_rewards}
            current_reward_roles = [r for r in member.roles if r.id in all_reward_ids]
            if current_reward_roles:
                try:
                    await member.remove_roles(*current_reward_roles, reason="Level-up: replacing role reward")
                except discord.Forbidden:
                    pass

        if rewards:
            top_role_id = rewards[0]["role_id"] if not stack else None
            for r in rewards:
                role = guild.get_role(r["role_id"])
                if role:
                    if not stack and r["role_id"] != top_role_id:
                        continue
                    if role not in member.roles:
                        try:
                            await member.add_roles(role, reason=f"Level {new_level} reward")
                        except discord.Forbidden:
                            pass

        # Send level-up message
        mode = cfg.get("message_mode", "context")
        if mode == "none":
            return

        lv_msg = cfg.get("level_message") or f"🎉 {member.mention} leveled up to **Level {new_level}**!"
        lv_msg = resolve_variables(lv_msg, member, guild, extra={"{level}": str(new_level)})

        if mode == "context":
            await safe_send(message.channel, content=lv_msg)
        elif mode == "pm":
            await member.send(lv_msg).catch(lambda: None) if hasattr(member, 'send') else None
        elif mode == "channel":
            lv_ch_id = cfg.get("message_channel")
            if lv_ch_id:
                lv_ch = guild.get_channel(lv_ch_id)
                if lv_ch:
                    await safe_send(lv_ch, content=lv_msg)

    # ────────────────────────────────────────────────────────────────────────────
    # /leveling commands
    # ────────────────────────────────────────────────────────────────────────────

    leveling_group = app_commands.Group(
        name="leveling", description="Configure the leveling system", guild_only=True
    )

    async def _ensure_config(self, guild_id: int):
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO leveling_config (guild_id) VALUES (?)", (guild_id,)
        )
        await self.bot.db.commit()

    @leveling_group.command(name="enable", description="Enable the leveling system")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_enable(self, interaction: discord.Interaction):
        await self._ensure_config(interaction.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_config SET enabled=1 WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("Leveling system **enabled**."), ephemeral=True
        )

    @leveling_group.command(name="disable", description="Disable the leveling system")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_disable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "UPDATE leveling_config SET enabled=0 WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("Leveling system **disabled**."), ephemeral=True
        )

    @leveling_group.command(name="config", description="Configure XP gain settings")
    @app_commands.describe(
        xp_min="Minimum XP per message",
        xp_max="Maximum XP per message",
        cooldown="Cooldown in seconds between XP gains",
        rate="XP multiplier (e.g. 1.5 = 50% more XP)",
        stack_roles="Keep all earned roles, or only the highest",
        message_mode="Where to send level-up messages",
    )
    @app_commands.choices(
        message_mode=[
            app_commands.Choice(name="In channel", value="context"),
            app_commands.Choice(name="DM", value="pm"),
            app_commands.Choice(name="Specific channel", value="channel"),
            app_commands.Choice(name="None", value="none"),
        ],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_config(
        self,
        interaction: discord.Interaction,
        xp_min: Optional[int] = 15,
        xp_max: Optional[int] = 25,
        cooldown: Optional[int] = 60,
        rate: Optional[float] = 1.0,
        stack_roles: Optional[bool] = True,
        message_mode: Optional[str] = "context",
    ):
        if xp_min > xp_max:
            return await interaction.response.send_message(
                embed=error_embed("xp_min must be ≤ xp_max."), ephemeral=True
            )

        await self._ensure_config(interaction.guild.id)
        await self.bot.db.execute(
            """UPDATE leveling_config SET
               xp_min=?, xp_max=?, xp_cooldown=?, xp_rate=?, stack_roles=?, message_mode=?
               WHERE guild_id=?""",
            (xp_min, xp_max, cooldown, rate, int(stack_roles), message_mode, interaction.guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                f"XP config updated.\n"
                f"XP: `{xp_min}–{xp_max}` per message · Cooldown: `{cooldown}s` · Rate: `{rate}x`\n"
                f"Stacking: `{stack_roles}` · Messages: `{message_mode}`"
            ),
            ephemeral=True,
        )

    @leveling_group.command(name="message", description="Set a custom level-up message")
    @app_commands.describe(
        message="Message template. Use {user.mention}, {level}, {guild.name}",
        channel="Channel for level-up messages (if mode is 'channel')",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_message(
        self,
        interaction: discord.Interaction,
        message: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        await self._ensure_config(interaction.guild.id)
        await self.bot.db.execute(
            "UPDATE leveling_config SET level_message=?, message_channel=? WHERE guild_id=?",
            (message, channel.id if channel else None, interaction.guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Level-up message set. Preview: {message[:100]}"), ephemeral=True
        )

    @leveling_group.command(name="reward", description="Add a role reward for reaching a level")
    @app_commands.describe(level="Level to reward at", role="Role to give")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_reward(
        self,
        interaction: discord.Interaction,
        level: int,
        role: discord.Role,
    ):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO level_rewards (guild_id, level, role_id) VALUES (?,?,?)",
            (interaction.guild.id, level, role.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"{role.mention} will be awarded at **level {level}**."), ephemeral=True
        )

    @leveling_group.command(name="removereward", description="Remove a level role reward")
    @app_commands.describe(level="Level to remove reward from")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_removereward(self, interaction: discord.Interaction, level: int):
        await self.bot.db.execute(
            "DELETE FROM level_rewards WHERE guild_id=? AND level=?",
            (interaction.guild.id, level),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Reward for level {level} removed."), ephemeral=True
        )

    @leveling_group.command(name="rewards", description="View all level rewards")
    async def lv_rewards(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM level_rewards WHERE guild_id=? ORDER BY level",
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No level rewards configured."), ephemeral=True
            )

        lines = [
            f"Level **{r['level']}** → <@&{r['role_id']}>"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=info_embed("\n".join(lines), title="Level Rewards"), ephemeral=True
        )

    @leveling_group.command(name="ignore", description="Ignore a channel or role from earning XP")
    @app_commands.describe(target="Channel or role to ignore/unignore")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lv_ignore(
        self,
        interaction: discord.Interaction,
        target: Union[discord.TextChannel, discord.Role],
    ):
        gid = interaction.guild.id
        t_type = "channel" if isinstance(target, discord.TextChannel) else "role"

        async with self.bot.db.execute(
            "SELECT 1 FROM level_ignore WHERE guild_id=? AND target_id=?", (gid, target.id)
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM level_ignore WHERE guild_id=? AND target_id=?", (gid, target.id)
            )
            msg = f"{target.mention} **unignored** from XP gain."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO level_ignore (guild_id, target_id, target_type) VALUES (?,?,?)",
                (gid, target.id, t_type),
            )
            msg = f"{target.mention} will not gain XP."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    # ── User commands ────────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="View your or another member's rank card")
    @app_commands.describe(member="Member to check (default: yourself)")
    async def rank(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ):
        member = member or interaction.user
        gid    = interaction.guild.id

        async with self.bot.db.execute(
            "SELECT xp, level FROM user_xp WHERE guild_id=? AND user_id=?",
            (gid, member.id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=info_embed(f"{member.mention} hasn't earned any XP yet."), ephemeral=True
            )

        total_xp = row["xp"]
        level, xp_in_level, xp_needed = xp_into_level(total_xp)

        # Get rank
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM user_xp WHERE guild_id=? AND xp > ?",
            (gid, total_xp),
        ) as cur:
            rank_pos = (await cur.fetchone())[0] + 1

        bar_filled = int((xp_in_level / max(1, xp_needed)) * 20)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)

        embed = discord.Embed(color=COL_BLUE)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Rank", value=f"**#{rank_pos}**", inline=True)
        embed.add_field(name="Total XP", value=f"**{total_xp:,}**", inline=True)
        embed.add_field(
            name=f"Progress to Level {level + 1}",
            value=f"`{bar}` {xp_in_level:,} / {xp_needed:,} XP",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="View the XP leaderboard")
    @app_commands.describe(page="Page number")
    async def leaderboard(self, interaction: discord.Interaction, page: Optional[int] = 1):
        gid = interaction.guild.id

        async with self.bot.db.execute(
            "SELECT user_id, xp, level FROM user_xp WHERE guild_id=? ORDER BY xp DESC LIMIT 100",
            (gid,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No one has earned XP yet."), ephemeral=True
            )

        items = []
        for i, r in enumerate(rows, 1):
            member = interaction.guild.get_member(r["user_id"])
            name   = member.display_name if member else f"User {r['user_id']}"
            medal  = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"`#{i}`"
            items.append(f"{medal} **{name}** — Level {r['level']} ({r['xp']:,} XP)")

        pages = build_pages(items, title=f"XP Leaderboard — {interaction.guild.name}", per_page=10)
        view  = Paginator(pages, interaction.user)
        start_page = min(page - 1, len(pages) - 1)
        view.page = start_page

        await interaction.response.send_message(embed=pages[start_page], view=view)

    @app_commands.command(name="setlevel", description="Set a member's level")
    @app_commands.describe(member="Member", level="Level to set")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlevel(self, interaction: discord.Interaction, member: discord.Member, level: int):
        if level < 0:
            return await interaction.response.send_message(
                embed=error_embed("Level must be 0 or higher."), ephemeral=True
            )

        # Calculate the XP for start of that level
        xp = sum(xp_for_level(l) for l in range(level))
        gid = interaction.guild.id

        await self.bot.db.execute(
            """INSERT INTO user_xp (guild_id, user_id, xp, level, last_xp)
               VALUES (?,?,?,?,0)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?""",
            (gid, member.id, xp, level, xp, level),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"**{member.display_name}** set to level **{level}**."), ephemeral=True
        )

    @app_commands.command(name="setxp", description="Set a member's total XP")
    @app_commands.describe(member="Member", xp="Total XP to set")
    @app_commands.checks.has_permissions(administrator=True)
    async def setxp(self, interaction: discord.Interaction, member: discord.Member, xp: int):
        if xp < 0:
            return await interaction.response.send_message(
                embed=error_embed("XP must be 0 or higher."), ephemeral=True
            )

        level = level_from_xp(xp)
        await self.bot.db.execute(
            """INSERT INTO user_xp (guild_id, user_id, xp, level, last_xp)
               VALUES (?,?,?,?,0)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?""",
            (interaction.guild.id, member.id, xp, level, xp, level),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"**{member.display_name}** XP set to **{xp:,}** (Level {level})."),
            ephemeral=True,
        )

    @app_commands.command(name="resetxp", description="Reset a member's XP and level to 0")
    @app_commands.describe(member="Member to reset")
    @app_commands.checks.has_permissions(administrator=True)
    async def resetxp(self, interaction: discord.Interaction, member: discord.Member):
        await self.bot.db.execute(
            "DELETE FROM user_xp WHERE guild_id=? AND user_id=?",
            (interaction.guild.id, member.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"**{member.display_name}**'s XP reset."), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Leveling(bot))
