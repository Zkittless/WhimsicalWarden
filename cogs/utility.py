"""
cogs/utility.py — General utility commands
Snipe, edit snipe, polls (timed, button votes), reminders (background task),
userinfo, serverinfo, roleinfo, avatar, banner, ping, help.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed,
    parse_duration, seconds_to_human, utcnow, discord_timestamp,
    safe_send, to_json, from_json, build_pages, Paginator,
    COL_BLUE, COL_GREEN, COL_YELLOW,
)

log = logging.getLogger("modbot.utility")


# ── Poll entry view ─────────────────────────────────────────────────────────────

class PollView(discord.ui.View):
    def __init__(self, poll_id: int, options: list[str]):
        super().__init__(timeout=None)
        for i, opt in enumerate(options):
            btn = discord.ui.Button(
                label=f"{opt[:70]}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"poll_{poll_id}_{i}",
            )
            self.add_item(btn)


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._check_reminders.start()
        self._check_polls.start()

    def cog_unload(self):
        self._check_reminders.cancel()
        self._check_polls.cancel()

    # ────────────────────────────────────────────────────────────────────────────
    # Background tasks
    # ────────────────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def _check_reminders(self):
        now = utcnow()
        async with self.bot.db.execute(
            "SELECT * FROM reminders WHERE remind_at <= ?", (now,)
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            user = self.bot.get_user(row["user_id"])
            if user:
                try:
                    embed = discord.Embed(
                        title="⏰ Reminder",
                        description=row["message"],
                        color=COL_YELLOW,
                        timestamp=datetime.now(timezone.utc),
                    )
                    await user.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    # Try channel
                    if row.get("channel_id"):
                        ch = self.bot.get_channel(row["channel_id"])
                        if ch:
                            await safe_send(ch, content=f"{user.mention}", embed=embed)

            await self.bot.db.execute("DELETE FROM reminders WHERE id=?", (row["id"],))

        if rows:
            await self.bot.db.commit()

    @tasks.loop(seconds=30)
    async def _check_polls(self):
        now = utcnow()
        async with self.bot.db.execute(
            "SELECT * FROM polls WHERE ended=0 AND ends_at <= ?", (now,)
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            await self._end_poll(dict(row))

    @_check_reminders.before_loop
    @_check_polls.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    async def _end_poll(self, row: dict):
        db    = self.bot.db
        guild = self.bot.get_guild(row["guild_id"])
        if not guild:
            return

        await db.execute("UPDATE polls SET ended=1 WHERE id=?", (row["id"],))
        await db.commit()

        ch = guild.get_channel(row["channel_id"])
        if not ch:
            return

        options = from_json(row["options"], [])

        # Tally votes
        votes = [0] * len(options)
        async with db.execute(
            "SELECT option_idx, COUNT(*) as cnt FROM poll_votes WHERE poll_id=? GROUP BY option_idx",
            (row["id"],),
        ) as cur:
            tally = await cur.fetchall()
        for t in tally:
            idx = t["option_idx"]
            if 0 <= idx < len(votes):
                votes[idx] = t["cnt"]

        total = sum(votes)
        lines = []
        for i, opt in enumerate(options):
            pct = int(votes[i] / total * 100) if total > 0 else 0
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            lines.append(f"**{opt}**\n`{bar}` {votes[i]} votes ({pct}%)")

        embed = discord.Embed(
            title=f"📊 Poll Ended: {row['question']}",
            description="\n\n".join(lines) if lines else "No votes cast.",
            color=COL_BLUE,
        )
        embed.set_footer(text=f"Total votes: {total}")

        try:
            msg = await ch.fetch_message(row["message_id"])
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            await safe_send(ch, embed=embed)

    # ────────────────────────────────────────────────────────────────────────────
    # Snipe / Edit Snipe
    # ────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO snipe_cache
               (channel_id, user_id, content, attachment, deleted_at)
               VALUES (?,?,?,?,?)""",
            (
                message.channel.id,
                message.author.id,
                message.content or "",
                message.attachments[0].url if message.attachments else None,
                utcnow(),
            ),
        )
        await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot:
            return
        if before.content == after.content:
            return
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO edit_snipe_cache
               (channel_id, user_id, before, after, edited_at)
               VALUES (?,?,?,?,?)""",
            (before.channel.id, before.author.id, before.content, after.content, utcnow()),
        )
        await self.bot.db.commit()

    @app_commands.command(name="snipe", description="Show the last deleted message in this channel")
    async def snipe(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM snipe_cache WHERE channel_id=?", (interaction.channel.id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=info_embed("Nothing to snipe here."), ephemeral=True
            )

        user = await self.bot.fetch_user(row["user_id"])
        embed = discord.Embed(
            description=row["content"] or "*[no text]*",
            color=COL_BLUE,
            timestamp=datetime.fromtimestamp(row["deleted_at"], tz=timezone.utc),
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        if row["attachment"]:
            embed.set_image(url=row["attachment"])
        embed.set_footer(text=f"Deleted {discord_timestamp(row['deleted_at'], 'R')}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="editsnipe", description="Show the last edited message in this channel")
    async def editsnipe(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM edit_snipe_cache WHERE channel_id=?", (interaction.channel.id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=info_embed("Nothing to editsnipe here."), ephemeral=True
            )

        user = await self.bot.fetch_user(row["user_id"])
        embed = discord.Embed(color=COL_BLUE, timestamp=datetime.fromtimestamp(row["edited_at"], tz=timezone.utc))
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="Before", value=row["before"][:1024] or "—", inline=False)
        embed.add_field(name="After", value=row["after"][:1024] or "—", inline=False)
        embed.set_footer(text=f"Edited {discord_timestamp(row['edited_at'], 'R')}")

        await interaction.response.send_message(embed=embed)

    # ────────────────────────────────────────────────────────────────────────────
    # Polls
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="poll", description="Create a poll with button voting")
    @app_commands.describe(
        question="The poll question",
        options="Comma-separated options (up to 5, e.g. 'Yes,No,Maybe')",
        duration="How long the poll runs (e.g. 1h, 1d). Leave empty for no auto-end.",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def poll(
        self,
        interaction: discord.Interaction,
        question: str,
        options: str,
        duration: Optional[str] = None,
    ):
        option_list = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_list) < 2:
            return await interaction.response.send_message(
                embed=error_embed("You need at least 2 options."), ephemeral=True
            )
        if len(option_list) > 5:
            return await interaction.response.send_message(
                embed=error_embed("Maximum 5 options per poll."), ephemeral=True
            )

        secs    = parse_duration(duration) if duration else None
        ends_at = utcnow() + secs if secs else None

        embed = discord.Embed(
            title=f"📊 {question}",
            description="\n".join(f"**{opt}**" for opt in option_list),
            color=COL_BLUE,
        )
        if ends_at:
            embed.set_footer(text=f"Ends {discord_timestamp(ends_at, 'R')}")

        await interaction.response.defer()
        await self.bot.db.execute(
            "INSERT INTO polls (guild_id, channel_id, question, options, ends_at) VALUES (?,?,?,?,?)",
            (interaction.guild.id, interaction.channel.id, question, to_json(option_list), ends_at),
        )
        await self.bot.db.commit()

        async with self.bot.db.execute("SELECT last_insert_rowid() as id") as cur:
            poll_id = (await cur.fetchone())["id"]

        view = discord.ui.View(timeout=None)
        for i, opt in enumerate(option_list):
            btn = discord.ui.Button(
                label=opt[:70],
                style=discord.ButtonStyle.secondary,
                custom_id=f"poll_{poll_id}_{i}",
            )

            async def vote_callback(inter: discord.Interaction, option_idx=i, pid=poll_id):
                db = inter.client.db
                async with db.execute(
                    "SELECT ended FROM polls WHERE id=?", (pid,)
                ) as cur:
                    p = await cur.fetchone()
                if not p or p["ended"]:
                    return await inter.response.send_message("This poll has ended.", ephemeral=True)

                async with db.execute(
                    "SELECT option_idx FROM poll_votes WHERE poll_id=? AND user_id=?",
                    (pid, inter.user.id),
                ) as cur:
                    existing = await cur.fetchone()

                if existing:
                    if existing["option_idx"] == option_idx:
                        await db.execute(
                            "DELETE FROM poll_votes WHERE poll_id=? AND user_id=?", (pid, inter.user.id)
                        )
                        await db.commit()
                        return await inter.response.send_message("Vote removed.", ephemeral=True)
                    else:
                        await db.execute(
                            "UPDATE poll_votes SET option_idx=? WHERE poll_id=? AND user_id=?",
                            (option_idx, pid, inter.user.id),
                        )
                else:
                    await db.execute(
                        "INSERT INTO poll_votes (poll_id, user_id, option_idx) VALUES (?,?,?)",
                        (pid, inter.user.id, option_idx),
                    )
                await db.commit()
                await inter.response.send_message(f"Voted for **{option_list[option_idx]}**.", ephemeral=True)

            btn.callback = vote_callback
            view.add_item(btn)

        msg = await interaction.followup.send(embed=embed, view=view)

        await self.bot.db.execute(
            "UPDATE polls SET channel_id=?, message_id=? WHERE id=?",
            (interaction.channel.id, msg.id, poll_id),
        )
        await self.bot.db.commit()

    @app_commands.command(name="endpoll", description="End a poll early and show results")
    @app_commands.describe(message_id="Message ID of the poll")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def endpoll(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Invalid message ID."), ephemeral=True
            )

        async with self.bot.db.execute(
            "SELECT * FROM polls WHERE guild_id=? AND message_id=? AND ended=0",
            (interaction.guild.id, mid),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                embed=error_embed("Active poll not found."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await self._end_poll(dict(row))
        await interaction.followup.send(embed=success_embed("Poll ended."), ephemeral=True)

    # ────────────────────────────────────────────────────────────────────────────
    # Reminders
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="remind", description="Set a reminder")
    @app_commands.describe(
        duration="When to remind you (e.g. 1h, 30m, 2d)",
        message="What to remind you about",
    )
    async def remind(self, interaction: discord.Interaction, duration: str, message: str):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=error_embed("Invalid duration. Examples: `1h`, `30m`, `2d`"), ephemeral=True
            )
        if secs > 30 * 86400:
            return await interaction.response.send_message(
                embed=error_embed("Maximum reminder duration is 30 days."), ephemeral=True
            )

        remind_at = utcnow() + secs
        await self.bot.db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, message, remind_at) VALUES (?,?,?,?,?)",
            (interaction.user.id, interaction.guild.id if interaction.guild else None,
             interaction.channel.id, message, remind_at),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            embed=success_embed(
                f"⏰ I'll remind you in **{seconds_to_human(secs)}**.\n"
                f"Reminder: {message}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reminders", description="View your active reminders")
    async def reminders_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM reminders WHERE user_id=? ORDER BY remind_at",
            (interaction.user.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("You have no active reminders."), ephemeral=True
            )

        lines = [
            f"`#{r['id']}` {discord_timestamp(r['remind_at'], 'R')} — {r['message'][:80]}"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=info_embed("\n".join(lines), title="Your Reminders"), ephemeral=True
        )

    @app_commands.command(name="delremind", description="Delete a reminder")
    @app_commands.describe(reminder_id="Reminder ID (from /reminders)")
    async def delremind(self, interaction: discord.Interaction, reminder_id: int):
        async with self.bot.db.execute(
            "SELECT 1 FROM reminders WHERE id=? AND user_id=?",
            (reminder_id, interaction.user.id),
        ) as cur:
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    embed=error_embed("Reminder not found or doesn't belong to you."), ephemeral=True
                )

        await self.bot.db.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Reminder #{reminder_id} deleted."), ephemeral=True
        )

    # ────────────────────────────────────────────────────────────────────────────
    # Info commands
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="userinfo", description="View information about a member")
    @app_commands.describe(member="Member to look up (default: yourself)")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ):
        member = member or interaction.user
        created_ts = int(member.created_at.timestamp())
        joined_ts  = int(member.joined_at.timestamp()) if member.joined_at else None

        embed = discord.Embed(color=member.color if member.color != discord.Color.default() else COL_BLUE)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="ID",          value=str(member.id),               inline=True)
        embed.add_field(name="Display Name",value=member.display_name,           inline=True)
        embed.add_field(name="Bot",         value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Created",     value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=True)
        if joined_ts:
            embed.add_field(name="Joined", value=f"<t:{joined_ts}:D> (<t:{joined_ts}:R>)", inline=True)
        if member.premium_since:
            boost_ts = int(member.premium_since.timestamp())
            embed.add_field(name="Boosting Since", value=f"<t:{boost_ts}:D>", inline=True)

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        if roles:
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=" ".join(roles[:15]) + (" ..." if len(roles) > 15 else ""),
                inline=False,
            )

        # Top permissions
        key_perms = []
        if member.guild_permissions.administrator:
            key_perms.append("Administrator")
        if member.guild_permissions.ban_members:
            key_perms.append("Ban Members")
        if member.guild_permissions.kick_members:
            key_perms.append("Kick Members")
        if member.guild_permissions.manage_guild:
            key_perms.append("Manage Server")
        if member.guild_permissions.manage_channels:
            key_perms.append("Manage Channels")
        if member.guild_permissions.manage_roles:
            key_perms.append("Manage Roles")
        if member.guild_permissions.manage_messages:
            key_perms.append("Manage Messages")
        if key_perms:
            embed.add_field(name="Key Permissions", value=", ".join(key_perms), inline=False)

        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="View information about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        created_ts = int(guild.created_at.timestamp())

        embed = discord.Embed(
            title=guild.name,
            color=COL_BLUE,
            timestamp=datetime.now(timezone.utc),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        embed.add_field(name="ID",        value=str(guild.id), inline=True)
        embed.add_field(name="Owner",     value=f"{guild.owner.mention if guild.owner else 'Unknown'}", inline=True)
        embed.add_field(name="Created",   value=f"<t:{created_ts}:D>", inline=True)
        embed.add_field(name="Members",   value=str(guild.member_count), inline=True)
        embed.add_field(name="Channels",  value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Roles",     value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Boosts",    value=f"{guild.premium_subscription_count} (Level {guild.premium_tier})", inline=True)
        embed.add_field(name="Emojis",    value=f"{len(guild.emojis)}/{guild.emoji_limit}", inline=True)
        embed.add_field(name="Stickers",  value=f"{len(guild.stickers)}/{guild.sticker_limit}", inline=True)

        if guild.features:
            embed.add_field(
                name="Features",
                value=", ".join(f.replace("_", " ").title() for f in guild.features[:10]),
                inline=False,
            )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="roleinfo", description="View information about a role")
    @app_commands.describe(role="The role to look up")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        embed = discord.Embed(
            title=f"@{role.name}",
            color=role.color if role.color != discord.Color.default() else COL_BLUE,
        )
        embed.add_field(name="ID",         value=str(role.id),                        inline=True)
        embed.add_field(name="Color",      value=str(role.color),                     inline=True)
        embed.add_field(name="Position",   value=str(role.position),                  inline=True)
        embed.add_field(name="Hoisted",    value="Yes" if role.hoist else "No",       inline=True)
        embed.add_field(name="Mentionable",value="Yes" if role.mentionable else "No", inline=True)
        embed.add_field(name="Members",    value=str(len(role.members)),               inline=True)

        created_ts = int(role.created_at.timestamp())
        embed.add_field(name="Created", value=f"<t:{created_ts}:D>", inline=True)

        # Key permissions
        perms = []
        for perm, value in role.permissions:
            if value and perm in (
                "administrator","ban_members","kick_members","manage_guild",
                "manage_channels","manage_roles","manage_messages","manage_webhooks",
                "mention_everyone","moderate_members",
            ):
                perms.append(perm.replace("_", " ").title())
        if perms:
            embed.add_field(name="Key Permissions", value=", ".join(perms), inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="View a member's avatar")
    @app_commands.describe(member="Member (default: yourself)", global_avatar="Show global avatar instead of server avatar")
    async def avatar(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        global_avatar: Optional[bool] = False,
    ):
        member = member or interaction.user
        avatar = member.avatar.url if (global_avatar and member.avatar) else member.display_avatar.url

        embed = discord.Embed(
            title=f"{member.display_name}'s Avatar",
            color=COL_BLUE,
        )
        embed.set_image(url=avatar)
        embed.add_field(name="Links", value=f"[PNG]({avatar}?format=png) | [JPG]({avatar}?format=jpg) | [WEBP]({avatar}?format=webp)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="banner", description="View a member's banner")
    @app_commands.describe(member="Member (default: yourself)")
    async def banner(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ):
        member = member or interaction.user
        # Need to fetch the user object for banner
        user = await self.bot.fetch_user(member.id)
        if not user.banner:
            return await interaction.response.send_message(
                embed=info_embed(f"{member.display_name} doesn't have a banner."), ephemeral=True
            )

        embed = discord.Embed(title=f"{member.display_name}'s Banner", color=COL_BLUE)
        embed.set_image(url=user.banner.url)
        await interaction.response.send_message(embed=embed)

    # ── Ping ─────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ping", description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        color = COL_GREEN if latency < 100 else COL_YELLOW if latency < 200 else 0xe74c3c
        embed = discord.Embed(
            title="✨ Pong!",
            description=f"**Websocket:** `{latency}ms`",
            color=color,
        )
        await interaction.response.send_message(embed=embed)

    # ── Help ─────────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="View all available commands")
    @app_commands.describe(category="Command category to view")
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Moderation",    value="mod"),
            app_commands.Choice(name="Security",      value="sec"),
            app_commands.Choice(name="AutoMod",       value="automod"),
            app_commands.Choice(name="Configuration", value="config"),
            app_commands.Choice(name="VoiceMaster",   value="vm"),
            app_commands.Choice(name="Tickets",       value="tickets"),
            app_commands.Choice(name="Leveling",      value="leveling"),
            app_commands.Choice(name="Giveaways",     value="giveaways"),
            app_commands.Choice(name="Utility",       value="utility"),
        ],
    )
    async def help(
        self,
        interaction: discord.Interaction,
        category: Optional[str] = None,
    ):
        categories = {
            "mod": (
                "⚔️ Moderation",
                "/ban /softban /hardban /tempban /unban\n"
                "/kick /timeout /untimeout\n"
                "/mute /unmute /imagemute /imagemute_remove\n"
                "/reactionmute /reactionmute_remove\n"
                "/jail /unjail /warn /unwarn /note\n"
                "/case view|history|reason|delete\n"
                "/purge messages|user|bots|contains|attachments|embeds\n"
                "/slowmode /lock /unlock /lockdown /unlockdown\n"
                "/escalation set|remove|list\n"
                "/nick /banlist\n"
                "/setup moderation|mute"
            ),
            "sec": (
                "🛡️ Security",
                "/antinuke enable|disable|module|whitelist|admin|config\n"
                "/antiraid massjoin|avatar|age|whitelist|state|config\n"
                "/fakepermissions grant|revoke|list|reset\n"
                "/bind staff|stafflist\n"
                "/recentban /raid"
            ),
            "automod": (
                "🤖 AutoMod",
                "/automod spam|caps|mentions|emoji|links|invites|phishing|duplicate\n"
                "/automod whitelist|blacklist|ignore|status\n"
                "/filter add|remove|list|clear"
            ),
            "config": (
                "⚙️ Configuration",
                "/welcome set|test|disable\n"
                "/goodbye set|test|disable\n"
                "/boost set|test|disable\n"
                "/autoresponder add|remove|list\n"
                "/reactiontrigger add|remove\n"
                "/reactionrole add|remove\n"
                "/buttonrole create|add\n"
                "/starboard setup|disable\n"
                "/counter add|remove\n"
                "/bump setup|disable\n"
                "/log add|remove|ignore|list"
            ),
            "vm": (
                "🎙️ VoiceMaster",
                "/voicemaster setup|defaultname|defaultbitrate|joinrole|disable\n"
                "/vc rename|limit|lock|unlock|permit|reject|transfer"
            ),
            "tickets": (
                "🎫 Tickets",
                "/ticket panel|delete_panel|close|reopen|delete|transcript\n"
                "/ticket claim|add|remove|list"
            ),
            "leveling": (
                "⭐ Leveling",
                "/leveling enable|disable|config|message|reward|removereward|rewards|ignore\n"
                "/rank /leaderboard /setlevel /setxp /resetxp"
            ),
            "giveaways": (
                "🎉 Giveaways",
                "/giveaway start|end|cancel|reroll|edit|list|entries"
            ),
            "utility": (
                "🔧 Utility",
                "/snipe /editsnipe\n"
                "/poll /endpoll\n"
                "/remind /reminders /delremind\n"
                "/userinfo /serverinfo /roleinfo\n"
                "/avatar /banner /ping /help"
            ),
        }

        if category and category in categories:
            title, desc = categories[category]
            embed = discord.Embed(title=title, description=f"```\n{desc}\n```", color=COL_BLUE)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Overview
        embed = discord.Embed(
            title="🔮 Whimsy — Help",
            description=(
                "A full-featured moderation bot.\n"
                "Use `/help [category]` for detailed command lists.\n\n"
                + "\n".join(
                    f"**{title}**"
                    for _, (title, _) in categories.items()
                )
            ),
            color=COL_BLUE,
        )
        embed.set_footer(text="All commands are slash commands — type / to get started")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Utility(bot))
