"""
cogs/logging.py — Event logging system
Logs: messages, members, roles, channels, invites, emojis, voice
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional, Union
import discord
from discord import app_commands
from discord.ext import commands
from utils import success_embed, error_embed, info_embed, safe_send, COL_BLUE

log = logging.getLogger("modbot.logging")

LOG_EVENTS = ("messages", "members", "roles", "channels", "invites", "emojis", "voice")


class Logging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def get_log_channels(self, guild_id: int, event: str) -> list[int]:
        async with self.bot.db.execute(
            "SELECT channel_id FROM log_channels WHERE guild_id=? AND event=?",
            (guild_id, event),
        ) as cur:
            rows = await cur.fetchall()
        return [r["channel_id"] for r in rows]

    async def is_ignored(self, guild_id: int, target_id: int) -> bool:
        async with self.bot.db.execute(
            "SELECT 1 FROM log_ignore WHERE guild_id=? AND target_id=? LIMIT 1",
            (guild_id, target_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def send_log(self, guild: discord.Guild, event: str, embed: discord.Embed):
        channel_ids = await self.get_log_channels(guild.id, event)
        for cid in channel_ids:
            ch = guild.get_channel(cid)
            if ch:
                await safe_send(ch, embed=embed)

    # ── Log group ───────────────────────────────────────────────────────────────

    log_group = app_commands.Group(name="log", description="Configure logging", guild_only=True)

    @log_group.command(name="add", description="Add a logging event to a channel")
    @app_commands.describe(
        channel="Channel to send logs to",
        event="Event type to log",
    )
    @app_commands.choices(
        event=[app_commands.Choice(name=e, value=e) for e in LOG_EVENTS] +
              [app_commands.Choice(name="all", value="all")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_add(self, interaction: discord.Interaction, channel: discord.TextChannel, event: str):
        gid = interaction.guild.id
        events = LOG_EVENTS if event == "all" else [event]
        for e in events:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO log_channels (guild_id, channel_id, event) VALUES (?,?,?)",
                (gid, channel.id, e),
            )
        await self.bot.db.commit()
        event_str = "all events" if event == "all" else f"`{event}`"
        await interaction.response.send_message(
            embed=success_embed(f"Logging {event_str} to {channel.mention}."), ephemeral=True
        )

    @log_group.command(name="remove", description="Remove a logging event from a channel")
    @app_commands.describe(channel="Channel", event="Event to remove (or 'all')")
    @app_commands.choices(
        event=[app_commands.Choice(name=e, value=e) for e in LOG_EVENTS] +
              [app_commands.Choice(name="all", value="all")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_remove(self, interaction: discord.Interaction, channel: discord.TextChannel, event: str):
        gid = interaction.guild.id
        if event == "all":
            await self.bot.db.execute(
                "DELETE FROM log_channels WHERE guild_id=? AND channel_id=?", (gid, channel.id)
            )
        else:
            await self.bot.db.execute(
                "DELETE FROM log_channels WHERE guild_id=? AND channel_id=? AND event=?",
                (gid, channel.id, event),
            )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Removed `{event}` logging from {channel.mention}."), ephemeral=True
        )

    @log_group.command(name="ignore", description="Ignore a member or channel from logs")
    @app_commands.describe(
        member="Member to ignore/unignore",
        channel="Channel to ignore/unignore",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_ignore(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        target = member or channel
        if not target:
            return await interaction.response.send_message(
                embed=error_embed("Provide a member or channel to ignore."), ephemeral=True
            )

        gid    = interaction.guild.id
        t_type = "member" if isinstance(target, discord.Member) else "channel"

        async with self.bot.db.execute(
            "SELECT 1 FROM log_ignore WHERE guild_id=? AND target_id=?", (gid, target.id)
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM log_ignore WHERE guild_id=? AND target_id=?", (gid, target.id)
            )
            msg = f"{target.mention} **unignored** from logs."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO log_ignore (guild_id, target_id, target_type) VALUES (?,?,?)",
                (gid, target.id, t_type),
            )
            msg = f"{target.mention} **ignored** from logs."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @log_group.command(name="list", description="View all configured log channels")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_list(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT channel_id, event FROM log_channels WHERE guild_id=? ORDER BY channel_id",
            (gid,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No log channels configured. Use `/log add`."), ephemeral=True
            )

        from collections import defaultdict
        grouped = defaultdict(list)
        for r in rows:
            grouped[r["channel_id"]].append(r["event"])

        lines = [
            f"<#{cid}>: {', '.join(f'`{e}`' for e in evts)}"
            for cid, evts in grouped.items()
        ]
        embed = discord.Embed(title="Log Channels", description="\n".join(lines), color=COL_BLUE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Message events ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if await self.is_ignored(message.guild.id, message.author.id):
            return
        if await self.is_ignored(message.guild.id, message.channel.id):
            return

        embed = discord.Embed(
            title="🌑 Message Deleted",
            color=0xe74c3c,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Author", value=f"{message.author.mention} ({message.author.id})", inline=True)
        if message.content:
            embed.add_field(name="Content", value=message.content[:1024], inline=False)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(a.filename for a in message.attachments), inline=False)
        embed.set_footer(text=f"Message ID: {message.id}")
        await self.send_log(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot:
            return
        if before.content == after.content:
            return
        if await self.is_ignored(before.guild.id, before.author.id):
            return
        if await self.is_ignored(before.guild.id, before.channel.id):
            return

        embed = discord.Embed(
            title="🔮 Message Edited",
            color=0xf39c12,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Author", value=f"{before.author.mention}", inline=True)
        embed.add_field(name="Before", value=before.content[:512] or "—", inline=False)
        embed.add_field(name="After", value=after.content[:512] or "—", inline=False)
        embed.add_field(name="Jump", value=f"[Click]({after.jump_url})", inline=True)
        await self.send_log(before.guild, "messages", embed)

    # ── Member events ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = discord.Embed(
            title="✨ Member Joined",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=True)
        embed.add_field(name="Account created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Members", value=str(member.guild.member_count), inline=True)
        await self.send_log(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        embed = discord.Embed(
            title="💨 Member Left",
            color=0xe74c3c,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=True)
        roles = [r.mention for r in member.roles if not r.is_default()]
        if roles:
            embed.add_field(name="Roles", value=", ".join(roles[:10]), inline=False)
        await self.send_log(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles and before.nick == after.nick:
            return
        embed = discord.Embed(title="🌟 Member Updated", color=0x3498db, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(after), icon_url=after.display_avatar.url)
        embed.add_field(name="User", value=f"{after.mention} ({after.id})", inline=False)

        if before.nick != after.nick:
            embed.add_field(name="Nickname", value=f"`{before.nick}` → `{after.nick}`", inline=False)

        added_roles = set(after.roles) - set(before.roles)
        removed_roles = set(before.roles) - set(after.roles)
        if added_roles:
            embed.add_field(name="Roles Added", value=", ".join(r.mention for r in added_roles), inline=False)
        if removed_roles:
            embed.add_field(name="Roles Removed", value=", ".join(r.mention for r in removed_roles), inline=False)

        await self.send_log(after.guild, "members", embed)

    # ── Voice events ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return
        embed = discord.Embed(color=0x9b59b6, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        if before.channel is None:
            embed.title = "🎵 Joined Voice"
            embed.add_field(name="Channel", value=after.channel.mention)
        elif after.channel is None:
            embed.title = "💨 Left Voice"
            embed.add_field(name="Channel", value=before.channel.mention)
        else:
            embed.title = "🌀 Moved Voice"
            embed.add_field(name="From", value=before.channel.mention, inline=True)
            embed.add_field(name="To", value=after.channel.mention, inline=True)
        await self.send_log(member.guild, "voice", embed)

    # ── Role events ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(title="✨ Role Created", color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Role", value=f"{role.mention} ({role.id})", inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        await self.send_log(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(title="🌑 Role Deleted", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Role", value=f"`{role.name}` ({role.id})", inline=True)
        await self.send_log(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            changes.append("Permissions changed")
        if not changes:
            return
        embed = discord.Embed(title="🔮 Role Updated", color=0xf39c12, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Role", value=after.mention, inline=True)
        embed.add_field(name="Changes", value="\n".join(changes), inline=False)
        await self.send_log(after.guild, "roles", embed)

    # ── Channel events ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="✨ Channel Created", color=0x2ecc71, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=f"{channel.mention} ({channel.id})", inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        await self.send_log(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="🌑 Channel Deleted", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=f"`#{channel.name}` ({channel.id})", inline=True)
        await self.send_log(channel.guild, "channels", embed)

    # ── Invite events ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        embed = discord.Embed(title="🌿 Invite Created", color=0x3498db, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Code", value=invite.code, inline=True)
        embed.add_field(name="Creator", value=str(invite.inviter), inline=True)
        embed.add_field(name="Channel", value=invite.channel.mention if invite.channel else "—", inline=True)
        embed.add_field(name="Max uses", value=str(invite.max_uses or "∞"), inline=True)
        await self.send_log(invite.guild, "invites", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        embed = discord.Embed(title="🌿 Invite Deleted", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Code", value=invite.code, inline=True)
        await self.send_log(invite.guild, "invites", embed)

    # ── Emoji events ──────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        before_ids = {e.id: e for e in before}
        after_ids  = {e.id: e for e in after}

        added   = [e for eid, e in after_ids.items()  if eid not in before_ids]
        removed = [e for eid, e in before_ids.items() if eid not in after_ids]

        if added:
            embed = discord.Embed(title="✨ Emoji Added", color=0x2ecc71, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Emojis", value=" ".join(str(e) for e in added[:10]), inline=False)
            await self.send_log(guild, "emojis", embed)

        if removed:
            embed = discord.Embed(title="🌑 Emoji Removed", color=0xe74c3c, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Emojis", value=", ".join(f"`:{e.name}:`" for e in removed[:10]), inline=False)
            await self.send_log(guild, "emojis", embed)


async def setup(bot):
    await bot.add_cog(Logging(bot))
