"""
cogs/giveaways.py — Full giveaway lifecycle
Start, end, cancel, reroll, edit, entry via button,
auto-end background task, required roles, min/max level gates.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed,
    parse_duration, seconds_to_human, utcnow, discord_timestamp,
    safe_send, to_json, from_json,
    COL_BLUE, COL_YELLOW,
)

log = logging.getLogger("modbot.giveaways")


class GiveawayEntryView(discord.ui.View):
    """Persistent giveaway entry button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✨ Enter",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway_enter",
    )
    async def enter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Giveaways = interaction.client.cogs.get("Giveaways")
        if cog:
            await cog._handle_entry(interaction)


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(GiveawayEntryView())
        self._check_giveaways.start()

    def cog_unload(self):
        self._check_giveaways.cancel()

    @tasks.loop(seconds=15)
    async def _check_giveaways(self):
        """Auto-end giveaways when they expire."""
        now = utcnow()
        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE ended=0 AND ends_at <= ?", (now,)
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            await self._end_giveaway(dict(row))

    @_check_giveaways.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ── Core helpers ─────────────────────────────────────────────────────────────

    def _build_giveaway_embed(self, row: dict, ended: bool = False) -> discord.Embed:
        ends_at  = row["ends_at"]
        winners  = row["winners"]
        host_id  = row["host_id"]

        color = 0x2ecc71 if not ended else 0x95a5a6

        embed = discord.Embed(
            title=f"✨ {row['prize']}",
            description=row.get("description") or "",
            color=color,
        )

        if not ended:
            embed.add_field(name="Ends", value=discord_timestamp(ends_at, "R"), inline=True)
        else:
            embed.add_field(name="Ended", value=discord_timestamp(ends_at, "R"), inline=True)

        embed.add_field(name="Winners", value=str(winners), inline=True)
        embed.add_field(name="Hosted by", value=f"<@{host_id}>", inline=True)

        required_roles = from_json(row.get("required_roles"), [])
        if required_roles:
            embed.add_field(
                name="Required Roles",
                value=", ".join(f"<@&{r}>" for r in required_roles),
                inline=False,
            )

        min_level = row.get("min_level")
        max_level = row.get("max_level")
        if min_level or max_level:
            level_str = f"Level {min_level or 0}"
            if max_level:
                level_str += f"–{max_level}"
            embed.add_field(name="Level Requirement", value=level_str, inline=True)

        if row.get("thumbnail"):
            embed.set_thumbnail(url=row["thumbnail"])
        if row.get("image"):
            embed.set_image(url=row["image"])

        embed.set_footer(text="React with 🎉 to enter!" if not ended else "Giveaway ended")

        return embed

    async def _handle_entry(self, interaction: discord.Interaction):
        """Handle giveaway entry via button."""
        db  = self.bot.db
        msg = interaction.message

        async with db.execute(
            "SELECT * FROM giveaways WHERE message_id=? AND ended=0", (msg.id,)
        ) as cur:
            giveaway = await cur.fetchone()

        if not giveaway:
            return await interaction.response.send_message(
                "This giveaway has ended or doesn't exist.", ephemeral=True
            )

        guild  = interaction.guild
        member = interaction.user

        # Check required roles
        required_roles = from_json(giveaway["required_roles"], [])
        if required_roles:
            member_role_ids = {r.id for r in member.roles}
            if not any(int(rid) in member_role_ids for rid in required_roles):
                role_mentions = ", ".join(f"<@&{r}>" for r in required_roles)
                return await interaction.response.send_message(
                    f"You need one of these roles to enter: {role_mentions}", ephemeral=True
                )

        # Check level requirement
        min_level = giveaway["min_level"]
        max_level = giveaway["max_level"]
        if min_level or max_level:
            async with db.execute(
                "SELECT level FROM user_xp WHERE guild_id=? AND user_id=?",
                (guild.id, member.id),
            ) as cur:
                xp_row = await cur.fetchone()
            user_level = xp_row["level"] if xp_row else 0

            if min_level and user_level < min_level:
                return await interaction.response.send_message(
                    f"You need to be at least Level {min_level} to enter this giveaway.", ephemeral=True
                )
            if max_level and user_level > max_level:
                return await interaction.response.send_message(
                    f"This giveaway is for Level {max_level} and below.", ephemeral=True
                )

        # Check if already entered
        async with db.execute(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
            (giveaway["id"], member.id),
        ) as cur:
            already = await cur.fetchone()

        if already:
            # Toggle: remove entry
            await db.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
                (giveaway["id"], member.id),
            )
            await db.commit()
            return await interaction.response.send_message(
                "You've **left** the giveaway. 😢", ephemeral=True
            )

        # Enter
        await db.execute(
            "INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (?,?)",
            (giveaway["id"], member.id),
        )
        await db.commit()
        await interaction.response.send_message(
            "You've **entered** the giveaway! Good luck! 🎉", ephemeral=True
        )

    async def _end_giveaway(self, row: dict):
        """End a giveaway and pick winners."""
        db = self.bot.db

        await db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (row["id"],))
        await db.commit()

        guild = self.bot.get_guild(row["guild_id"])
        if not guild:
            return

        ch = guild.get_channel(row["channel_id"])
        if not ch:
            return

        # Fetch entries
        async with db.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (row["id"],)
        ) as cur:
            entries = [r["user_id"] for r in await cur.fetchall()]

        winner_count = min(row["winners"], len(entries))
        winners = []
        if entries:
            picked = random.sample(entries, winner_count)
            winners = [f"<@{uid}>" for uid in picked]

        # Update the giveaway message
        try:
            msg = await ch.fetch_message(row["message_id"])
            embed = self._build_giveaway_embed(row, ended=True)
            if winners:
                embed.add_field(name=f"🏆 Winner{'s' if len(winners) > 1 else ''}", value=", ".join(winners), inline=False)
            else:
                embed.add_field(name="Winners", value="No valid entries", inline=False)

            await msg.edit(embed=embed, view=None)
        except (discord.NotFound, discord.HTTPException):
            pass

        # Announce winners
        if winners:
            mention_str = ", ".join(winners)
            await safe_send(
                ch,
                content=f"✨ Congratulations {mention_str}! You won **{row['prize']}**!",
            )
        else:
            await safe_send(ch, content=f"✨ The giveaway for **{row['prize']}** has ended with no valid entries.")

    # ────────────────────────────────────────────────────────────────────────────
    # /giveaway commands
    # ────────────────────────────────────────────────────────────────────────────

    gw_group = app_commands.Group(
        name="giveaway",
        description="Manage giveaways",
        guild_only=True,
    )

    @gw_group.command(name="start", description="Start a new giveaway")
    @app_commands.describe(
        channel="Channel to host the giveaway",
        duration="How long the giveaway runs (e.g. 1d, 12h, 30m)",
        winners="Number of winners",
        prize="What's being given away",
        description="Additional description",
        required_roles="Comma-separated role IDs required to enter",
        min_level="Minimum level required to enter",
        max_level="Maximum level allowed",
        thumbnail="Thumbnail image URL",
        image="Main image URL",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gw_start(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: str,
        winners: int,
        prize: str,
        description: Optional[str] = None,
        required_roles: Optional[str] = None,
        min_level: Optional[int] = None,
        max_level: Optional[int] = None,
        thumbnail: Optional[str] = None,
        image: Optional[str] = None,
    ):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=error_embed("Invalid duration. Examples: `1d`, `12h`, `30m`"), ephemeral=True
            )
        if winners < 1 or winners > 20:
            return await interaction.response.send_message(
                embed=error_embed("Winners must be between 1 and 20."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        ends_at = utcnow() + secs
        host_id = interaction.user.id
        gid     = interaction.guild.id

        # Parse required roles
        role_ids = []
        if required_roles:
            for part in required_roles.split(","):
                part = part.strip()
                try:
                    role_ids.append(int(part))
                except ValueError:
                    pass

        row = {
            "prize":          prize,
            "description":    description,
            "winners":        winners,
            "host_id":        host_id,
            "ends_at":        ends_at,
            "required_roles": to_json(role_ids) if role_ids else None,
            "min_level":      min_level,
            "max_level":      max_level,
            "thumbnail":      thumbnail,
            "image":          image,
            "ended":          0,
        }

        embed = self._build_giveaway_embed(row)
        view  = GiveawayEntryView()
        msg   = await channel.send(embed=embed, view=view)

        await self.bot.db.execute(
            """INSERT INTO giveaways
               (guild_id, channel_id, message_id, host_id, prize, description, thumbnail, image,
                winners, ends_at, ended, required_roles, min_level, max_level)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
            (gid, channel.id, msg.id, host_id, prize, description, thumbnail, image,
             winners, ends_at, to_json(role_ids) if role_ids else None, min_level, max_level),
        )
        await self.bot.db.commit()

        await interaction.followup.send(
            embed=success_embed(
                f"Giveaway started in {channel.mention}!\n"
                f"Prize: **{prize}** · Duration: {seconds_to_human(secs)} · Winners: {winners}"
            ),
            ephemeral=True,
        )

    @gw_group.command(name="end", description="End a giveaway early")
    @app_commands.describe(message_id="Message ID of the giveaway")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gw_end(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE guild_id=? AND message_id=? AND ended=0",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Active giveaway not found."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await self._end_giveaway(dict(row))
        await interaction.followup.send(
            embed=success_embed("Giveaway ended."), ephemeral=True
        )

    @gw_group.command(name="cancel", description="Cancel a giveaway without picking winners")
    @app_commands.describe(message_id="Message ID of the giveaway")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gw_cancel(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE guild_id=? AND message_id=? AND ended=0",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Active giveaway not found."), ephemeral=True
            )

        await self.bot.db.execute(
            "UPDATE giveaways SET ended=1 WHERE id=?", (row["id"],)
        )
        await self.bot.db.commit()

        # Update message
        guild = interaction.guild
        ch    = guild.get_channel(row["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(mid)
                embed = discord.Embed(
                    title=f"~~✨ {row['prize']}~~",
                    description="This giveaway was cancelled.",
                    color=0x95a5a6,
                )
                await msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            embed=success_embed(f"Giveaway for **{row['prize']}** cancelled."), ephemeral=True
        )

    @gw_group.command(name="reroll", description="Reroll the winners of an ended giveaway")
    @app_commands.describe(message_id="Message ID of the ended giveaway", winners="How many new winners")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gw_reroll(
        self,
        interaction: discord.Interaction,
        message_id: str,
        winners: Optional[int] = None,
    ):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE guild_id=? AND message_id=? AND ended=1",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Ended giveaway not found."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (row["id"],)
        ) as cur:
            entries = [r["user_id"] for r in await cur.fetchall()]

        winner_count = min(winners or row["winners"], len(entries))
        if not entries:
            return await interaction.response.send_message(
                embed=error_embed("No entries to reroll from."), ephemeral=True
            )

        picked = random.sample(entries, winner_count)
        mentions = ", ".join(f"<@{uid}>" for uid in picked)

        ch = interaction.guild.get_channel(row["channel_id"])
        if ch:
            await safe_send(
                ch,
                content=f"✨ **Reroll!** New winner(s): {mentions} — Congratulations on **{row['prize']}**!"
            )

        await interaction.response.send_message(
            embed=success_embed(f"Rerolled! New winners: {mentions}"), ephemeral=True
        )

    @gw_group.command(name="edit", description="Edit an active giveaway")
    @app_commands.describe(
        message_id="Message ID of the giveaway",
        prize="New prize name",
        description="New description",
        winners="New winner count",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gw_edit(
        self,
        interaction: discord.Interaction,
        message_id: str,
        prize: Optional[str] = None,
        description: Optional[str] = None,
        winners: Optional[int] = None,
    ):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE guild_id=? AND message_id=? AND ended=0",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Active giveaway not found."), ephemeral=True
            )

        updates = []
        values  = []
        if prize:
            updates.append("prize=?");       values.append(prize)
        if description is not None:
            updates.append("description=?"); values.append(description)
        if winners:
            updates.append("winners=?");     values.append(winners)

        if not updates:
            return await interaction.response.send_message(
                embed=error_embed("No changes specified."), ephemeral=True
            )

        values.append(row["id"])
        await self.bot.db.execute(
            f"UPDATE giveaways SET {', '.join(updates)} WHERE id=?", values
        )
        await self.bot.db.commit()

        # Refresh message
        updated_row = dict(row)
        if prize:        updated_row["prize"]       = prize
        if description:  updated_row["description"] = description
        if winners:      updated_row["winners"]     = winners

        ch = interaction.guild.get_channel(row["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(mid)
                embed = self._build_giveaway_embed(updated_row)
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            embed=success_embed("Giveaway updated."), ephemeral=True
        )

    @gw_group.command(name="list", description="View active giveaways")
    async def gw_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM giveaways WHERE guild_id=? AND ended=0 ORDER BY ends_at",
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No active giveaways."), ephemeral=True
            )

        lines = []
        for r in rows:
            ch    = interaction.guild.get_channel(r["channel_id"])
            ch_str = ch.mention if ch else "unknown"
            lines.append(
                f"**{r['prize']}** — {ch_str} — ends {discord_timestamp(r['ends_at'], 'R')} — {r['winners']} winner(s)"
            )

        embed = discord.Embed(
            title=f"Active Giveaways ({len(rows)})",
            description="\n".join(lines),
            color=COL_BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @gw_group.command(name="entries", description="View how many entries a giveaway has")
    @app_commands.describe(message_id="Message ID of the giveaway")
    async def gw_entries(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT id, prize FROM giveaways WHERE guild_id=? AND message_id=?",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Giveaway not found."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (row["id"],)
        ) as cur:
            count = (await cur.fetchone())[0]

        await interaction.response.send_message(
            embed=info_embed(f"**{row['prize']}** has **{count}** {'entry' if count == 1 else 'entries'}."),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
