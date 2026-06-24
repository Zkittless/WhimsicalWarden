"""
cogs/moderation.py — Full moderation command suite
ban, softban, hardban, tempban, kick, mute (3 types), jail, warn, case system
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed, warn_embed, log_embed,
    parse_duration, seconds_to_human, utcnow, discord_timestamp,
    dispatch_mod_log, safe_send, try_dm, create_case,
    require_fake_perm, guild_only, build_pages, Paginator, ConfirmView,
    resolve_member_or_user, resolve_variables, parse_embed_script,
    COL_RED, COL_GREEN, COL_YELLOW, COL_BLUE,
)

log = logging.getLogger("modbot.moderation")


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._check_temp_punishments.start()

    def cog_unload(self):
        self._check_temp_punishments.cancel()

    # ── Temp punishment expiry loop ─────────────────────────────────────────────
    @tasks.loop(seconds=30)
    async def _check_temp_punishments(self):
        """Unban/unmute users whose temp punishment has expired."""
        now = utcnow()
        db = self.bot.db

        async with db.execute(
            "SELECT * FROM temp_punishments WHERE expires_at <= ?", (now,)
        ) as cur:
            expired = await cur.fetchall()

        for row in expired:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue

            if row["action"] == "tempban":
                try:
                    user = await self.bot.fetch_user(row["user_id"])
                    await guild.unban(user, reason="Temporary ban expired")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            elif row["action"] == "tempmute":
                member = guild.get_member(row["user_id"])
                if member:
                    # Remove mute role
                    async with db.execute(
                        "SELECT mute_role FROM guild_config WHERE guild_id=?", (guild.id,)
                    ) as cur2:
                        cfg = await cur2.fetchone()
                    if cfg and cfg["mute_role"]:
                        role = guild.get_role(cfg["mute_role"])
                        if role and role in member.roles:
                            try:
                                await member.remove_roles(role, reason="Temp mute expired")
                            except discord.Forbidden:
                                pass

            await db.execute(
                "DELETE FROM temp_punishments WHERE id=?", (row["id"],)
            )

        if expired:
            await db.commit()

    @_check_temp_punishments.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ── Setup commands ──────────────────────────────────────────────────────────

    setup_group = app_commands.Group(
        name="setup",
        description="Initial server setup commands",
        guild_only=True,
    )

    @setup_group.command(name="moderation", description="Create mod-log channel and jail role/channel")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_mod(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db

        created = []

        # Create mod-log channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        mod_log = await guild.create_text_channel("mod-log", overwrites=overwrites)
        created.append(f"📋 <#{mod_log.id}> — mod log")

        # Create jail role
        jail_role = await guild.create_role(name="Jailed", color=discord.Color.dark_gray())
        created.append(f"🔒 {jail_role.mention} — jail role")

        # Create jail channel
        jail_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            jail_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        jail_ch = await guild.create_text_channel("jail", overwrites=jail_overwrites)
        created.append(f"🔒 <#{jail_ch.id}> — jail channel")

        # Deny jail role from seeing all other channels
        for channel in guild.channels:
            if channel.id != jail_ch.id:
                try:
                    await channel.set_permissions(
                        jail_role,
                        read_messages=False,
                        reason="Jail role setup",
                    )
                except discord.Forbidden:
                    pass

        # Save to DB
        await db.execute(
            """INSERT OR REPLACE INTO guild_config
               (guild_id, mod_log_channel, jail_channel, jail_role, setup_done)
               VALUES (?,?,?,?,1)""",
            (guild.id, mod_log.id, jail_ch.id, jail_role.id),
        )
        await db.commit()

        await interaction.followup.send(
            embed=success_embed(
                "Setup complete!\n\n" + "\n".join(created),
                title="Moderation Setup",
            ),
            ephemeral=True,
        )

    @setup_group.command(name="mute", description="Create muted, image-muted and reaction-muted roles")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    async def setup_mute(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db

        created = []

        mute_role = await guild.create_role(name="Muted", color=discord.Color.light_gray())
        img_role  = await guild.create_role(name="Image Muted", color=discord.Color.light_gray())
        rxn_role  = await guild.create_role(name="Reaction Muted", color=discord.Color.light_gray())
        created.extend([
            f"🔇 {mute_role.mention} — text muted",
            f"🖼️ {img_role.mention} — image muted",
            f"💬 {rxn_role.mention} — reaction muted",
        ])

        # Apply channel permission overrides
        for channel in guild.text_channels:
            try:
                await channel.set_permissions(mute_role, send_messages=False, reason="Mute role setup")
                await channel.set_permissions(img_role, attach_files=False, embed_links=False, reason="Image mute setup")
                await channel.set_permissions(rxn_role, add_reactions=False, reason="Reaction mute setup")
            except discord.Forbidden:
                pass

        await db.execute(
            """UPDATE guild_config SET mute_role=?, image_mute_role=?, reaction_mute_role=?
               WHERE guild_id=?""",
            (mute_role.id, img_role.id, rxn_role.id, guild.id),
        )
        await db.commit()

        await interaction.followup.send(
            embed=success_embed("\n".join(created), title="Mute Roles Created"),
            ephemeral=True,
        )

    # ── Core mod helpers ────────────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: int) -> dict:
        async with self.bot.db.execute(
            "SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def get_invoke_message(self, guild_id: int, command: str) -> tuple[str, str]:
        """Get custom invoke message and DM for a command."""
        # For now return defaults; invoke system can be extended
        return None, None

    async def send_mod_response(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        ephemeral: bool = False,
    ):
        try:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        except discord.InteractionResponded:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    async def _log_mod_action(
        self,
        guild: discord.Guild,
        action: str,
        user: Union[discord.Member, discord.User],
        moderator: discord.Member,
        reason: str,
        duration: int = None,
        case_num: int = None,
        color: int = COL_RED,
    ):
        embed = log_embed(
            action=action,
            user=user,
            moderator=moderator,
            reason=reason,
            duration=seconds_to_human(duration) if duration else None,
            case=case_num,
            color=color,
        )
        await dispatch_mod_log(self.bot, guild, embed)

    def _hierarchy_check(
        self,
        moderator: discord.Member,
        target: discord.Member,
    ) -> Optional[str]:
        """Returns an error string if the action isn't allowed, else None."""
        if target == moderator:
            return "You cannot moderate yourself."
        if target.id == moderator.guild.owner_id:
            return "You cannot moderate the server owner."
        if target.top_role >= moderator.top_role and moderator.id != moderator.guild.owner_id:
            return f"{target.mention}'s top role is higher than or equal to yours."
        if target.top_role >= moderator.guild.me.top_role:
            return f"My role is not high enough to moderate {target.mention}."
        return None

    # ────────────────────────────────────────────────────────────────────────────
    # /ban
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        user="Member to ban",
        reason="Reason for the ban",
        delete_days="Days of messages to delete (0-7)",
        silent="Don't DM the user",
    )
    @require_fake_perm("ban_members")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
        delete_days: Optional[int] = 0,
        silent: Optional[bool] = False,
    ):
        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "ban", reason,
        )

        if not silent:
            dm_embed = discord.Embed(
                description=f"You have been **banned** from **{interaction.guild.name}**.\nReason: {reason}",
                color=COL_RED,
            )
            await try_dm(user, embed=dm_embed)

        await interaction.guild.ban(
            user,
            reason=f"{interaction.user} ({interaction.user.id}): {reason}",
            delete_message_days=max(0, min(7, delete_days or 0)),
        )

        await interaction.response.send_message(
            embed=success_embed(f"**{user}** has been banned. Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "⚔️ Ban", user, interaction.user, reason, case_num=case_num
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /softban
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="softban", description="Ban and immediately unban to delete messages")
    @app_commands.describe(user="Member to softban", reason="Reason")
    @require_fake_perm("ban_members")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def softban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "softban", reason,
        )

        dm_embed = discord.Embed(
            description=f"You have been **softbanned** from **{interaction.guild.name}**.\nReason: {reason}",
            color=COL_YELLOW,
        )
        await try_dm(user, embed=dm_embed)
        await interaction.guild.ban(user, reason=f"Softban: {reason}", delete_message_days=7)
        await asyncio.sleep(0.5)
        await interaction.guild.unban(user, reason="Softban — unban after message deletion")

        await interaction.response.send_message(
            embed=success_embed(f"**{user}** softbanned (messages deleted). Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "⚔️ Softban", user, interaction.user, reason,
            case_num=case_num, color=COL_YELLOW,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /hardban
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="hardban", description="Ban a user by ID even if not in server")
    @app_commands.describe(user_id="User ID to ban", reason="Reason")
    @require_fake_perm("ban_members")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def hardban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: Optional[str] = "No reason provided",
    ):
        try:
            uid = int(user_id)
            user = await self.bot.fetch_user(uid)
        except (ValueError, discord.NotFound):
            return await interaction.response.send_message(
                embed=error_embed("Invalid user ID or user not found."), ephemeral=True
            )

        case_num = await create_case(
            self.bot.db, interaction.guild.id, uid,
            interaction.user.id, "hardban", reason,
        )
        await interaction.guild.ban(
            discord.Object(id=uid),
            reason=f"Hardban by {interaction.user}: {reason}",
            delete_message_days=0,
        )
        await interaction.response.send_message(
            embed=success_embed(f"**{user}** (`{uid}`) hardbanned. Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "⚔️ Hardban", user, interaction.user, reason, case_num=case_num
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /tempban
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="tempban", description="Temporarily ban a member")
    @app_commands.describe(
        user="Member to tempban",
        duration="Duration (e.g. 7d, 24h, 30m)",
        reason="Reason",
    )
    @require_fake_perm("ban_members")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def tempban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str] = "No reason provided",
    ):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=error_embed("Invalid duration. Examples: `7d`, `24h`, `30m`"), ephemeral=True
            )

        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        expires_at = utcnow() + secs
        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "tempban", reason, secs,
        )

        dm_embed = discord.Embed(
            description=(
                f"You have been **temporarily banned** from **{interaction.guild.name}**.\n"
                f"Duration: {seconds_to_human(secs)}\nReason: {reason}"
            ),
            color=COL_RED,
        )
        await try_dm(user, embed=dm_embed)
        await interaction.guild.ban(user, reason=f"Tempban ({seconds_to_human(secs)}): {reason}")

        await self.bot.db.execute(
            "INSERT INTO temp_punishments (guild_id, user_id, action, expires_at, case_id) VALUES (?,?,?,?,?)",
            (interaction.guild.id, user.id, "tempban", expires_at, case_num),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            embed=success_embed(
                f"**{user}** tempbanned for **{seconds_to_human(secs)}**. "
                f"Expires {discord_timestamp(expires_at)}. Case #{case_num}"
            )
        )
        await self._log_mod_action(
            interaction.guild, "🌑 Tempban", user, interaction.user, reason,
            duration=secs, case_num=case_num,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /unban
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Unban a user by ID or username")
    @app_commands.describe(user_id="User ID to unban", reason="Reason")
    @require_fake_perm("ban_members")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: Optional[str] = "No reason provided",
    ):
        try:
            uid = int(user_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Please provide a valid user ID."), ephemeral=True
            )

        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            return await interaction.response.send_message(
                embed=error_embed("This user is not banned."), ephemeral=True
            )

        await interaction.guild.unban(ban_entry.user, reason=reason)

        # Remove any temp ban record
        await self.bot.db.execute(
            "DELETE FROM temp_punishments WHERE guild_id=? AND user_id=? AND action='tempban'",
            (interaction.guild.id, uid),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            embed=success_embed(f"**{ban_entry.user}** has been unbanned.")
        )
        await self._log_mod_action(
            interaction.guild, "✨ Unban", ban_entry.user, interaction.user,
            reason, color=COL_GREEN,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /kick
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(user="Member to kick", reason="Reason", silent="Don't DM the user")
    @require_fake_perm("kick_members")
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
        silent: Optional[bool] = False,
    ):
        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "kick", reason,
        )

        if not silent:
            dm_embed = discord.Embed(
                description=f"You have been **kicked** from **{interaction.guild.name}**.\nReason: {reason}",
                color=COL_YELLOW,
            )
            await try_dm(user, embed=dm_embed)

        await interaction.guild.kick(
            user, reason=f"{interaction.user} ({interaction.user.id}): {reason}"
        )
        await interaction.response.send_message(
            embed=success_embed(f"**{user}** has been kicked. Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "💨 Kick", user, interaction.user, reason,
            case_num=case_num, color=COL_YELLOW,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /timeout
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="timeout", description="Timeout a member")
    @app_commands.describe(
        user="Member to timeout",
        duration="Duration (e.g. 10m, 1h, 7d — max 28d)",
        reason="Reason",
    )
    @require_fake_perm("moderate_members")
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str] = "No reason provided",
    ):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=error_embed("Invalid duration."), ephemeral=True
            )
        max_secs = 28 * 86400
        if secs > max_secs:
            return await interaction.response.send_message(
                embed=error_embed("Maximum timeout duration is 28 days."), ephemeral=True
            )

        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "timeout", reason, secs,
        )
        until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        await user.timeout(until, reason=reason)

        await interaction.response.send_message(
            embed=success_embed(
                f"**{user}** timed out for **{seconds_to_human(secs)}**. Case #{case_num}"
            )
        )
        await self._log_mod_action(
            interaction.guild, "🕯️ Timeout", user, interaction.user, reason,
            duration=secs, case_num=case_num, color=COL_YELLOW,
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(user="Member to un-timeout", reason="Reason")
    @require_fake_perm("moderate_members")
    async def untimeout(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        if not user.is_timed_out():
            return await interaction.response.send_message(
                embed=error_embed(f"{user.mention} is not timed out."), ephemeral=True
            )
        await user.timeout(None, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"Timeout removed from **{user}**.")
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /mute — text mute
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="mute", description="Mute a member (text)")
    @app_commands.describe(
        user="Member to mute",
        duration="Optional duration (e.g. 1h, 30m). Leave empty for permanent.",
        reason="Reason",
    )
    @require_fake_perm("moderate_members")
    async def mute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: Optional[str] = None,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        mute_role_id = cfg.get("mute_role")
        if not mute_role_id:
            return await interaction.response.send_message(
                embed=error_embed("Mute role not configured. Run `/setup mute` first."),
                ephemeral=True,
            )

        mute_role = interaction.guild.get_role(mute_role_id)
        if not mute_role:
            return await interaction.response.send_message(
                embed=error_embed("Mute role not found. Please re-run `/setup mute`."),
                ephemeral=True,
            )

        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        secs = parse_duration(duration) if duration else None
        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "mute", reason, secs,
        )

        await user.add_roles(mute_role, reason=reason)

        dm_embed = discord.Embed(
            description=(
                f"You have been **muted** in **{interaction.guild.name}**.\n"
                + (f"Duration: {seconds_to_human(secs)}\n" if secs else "")
                + f"Reason: {reason}"
            ),
            color=COL_YELLOW,
        )
        await try_dm(user, embed=dm_embed)

        if secs:
            expires_at = utcnow() + secs
            await self.bot.db.execute(
                "INSERT INTO temp_punishments (guild_id, user_id, action, expires_at, case_id) VALUES (?,?,?,?,?)",
                (interaction.guild.id, user.id, "tempmute", expires_at, case_num),
            )
            await self.bot.db.commit()

        dur_str = f" for **{seconds_to_human(secs)}**" if secs else " permanently"
        await interaction.response.send_message(
            embed=success_embed(f"**{user}** muted{dur_str}. Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "🌑 Mute", user, interaction.user, reason,
            duration=secs, case_num=case_num, color=COL_YELLOW,
        )

    @app_commands.command(name="unmute", description="Unmute a member")
    @app_commands.describe(user="Member to unmute", reason="Reason")
    @require_fake_perm("moderate_members")
    async def unmute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        mute_role = interaction.guild.get_role(cfg.get("mute_role", 0))
        if not mute_role or mute_role not in user.roles:
            return await interaction.response.send_message(
                embed=error_embed(f"{user.mention} is not muted."), ephemeral=True
            )

        await user.remove_roles(mute_role, reason=reason)
        # Clear temp punishment
        await self.bot.db.execute(
            "DELETE FROM temp_punishments WHERE guild_id=? AND user_id=? AND action='tempmute'",
            (interaction.guild.id, user.id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            embed=success_embed(f"**{user}** has been unmuted.")
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /imagemute / /reactionmute
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="imagemute", description="Restrict a member from posting images/embeds")
    @app_commands.describe(user="Member to image-mute", reason="Reason")
    @require_fake_perm("manage_messages")
    async def imagemute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("image_mute_role", 0))
        if not role:
            return await interaction.response.send_message(
                embed=error_embed("Image mute role not set. Run `/setup mute` first."), ephemeral=True
            )
        await user.add_roles(role, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"**{user}** can no longer post images or embeds.")
        )

    @app_commands.command(name="imagemute_remove", description="Remove image mute from a member")
    @app_commands.describe(user="Member", reason="Reason")
    @require_fake_perm("manage_messages")
    async def imagemute_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("image_mute_role", 0))
        if not role or role not in user.roles:
            return await interaction.response.send_message(
                embed=error_embed(f"{user.mention} doesn't have an image mute."), ephemeral=True
            )
        await user.remove_roles(role, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"Image mute removed from **{user}**.")
        )

    @app_commands.command(name="reactionmute", description="Restrict a member from reacting")
    @app_commands.describe(user="Member to reaction-mute", reason="Reason")
    @require_fake_perm("manage_messages")
    async def reactionmute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("reaction_mute_role", 0))
        if not role:
            return await interaction.response.send_message(
                embed=error_embed("Reaction mute role not set. Run `/setup mute` first."), ephemeral=True
            )
        await user.add_roles(role, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"**{user}** can no longer add reactions.")
        )

    @app_commands.command(name="reactionmute_remove", description="Remove reaction mute")
    @app_commands.describe(user="Member", reason="Reason")
    @require_fake_perm("manage_messages")
    async def reactionmute_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("reaction_mute_role", 0))
        if not role or role not in user.roles:
            return await interaction.response.send_message(
                embed=error_embed(f"{user.mention} doesn't have a reaction mute."), ephemeral=True
            )
        await user.remove_roles(role, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"Reaction mute removed from **{user}**.")
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /jail / /unjail
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="jail", description="Restrict a member to the jail channel")
    @app_commands.describe(user="Member to jail", reason="Reason")
    @require_fake_perm("moderate_members")
    async def jail(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        jail_role_id = cfg.get("jail_role")
        if not jail_role_id:
            return await interaction.response.send_message(
                embed=error_embed("Jail not configured. Run `/setup moderation` first."),
                ephemeral=True,
            )

        jail_role = interaction.guild.get_role(jail_role_id)
        if not jail_role:
            return await interaction.response.send_message(
                embed=error_embed("Jail role not found."), ephemeral=True
            )

        err = self._hierarchy_check(interaction.user, user)
        if err:
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "jail", reason,
        )

        # Store their current roles (excluding @everyone)
        roles_to_restore = [r.id for r in user.roles if not r.is_default()]
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO temp_punishments (guild_id, user_id, action, expires_at, case_id)
               VALUES (?,?,?,?,?)""",
            (interaction.guild.id, user.id, "jail", 0, case_num),
        )
        await self.bot.db.commit()

        # Remove all roles and add jail role
        try:
            await user.edit(
                roles=[jail_role],
                reason=f"Jailed by {interaction.user}: {reason}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                embed=error_embed("I don't have permission to edit this user's roles."), ephemeral=True
            )

        dm_embed = discord.Embed(
            description=f"You have been **jailed** in **{interaction.guild.name}**.\nReason: {reason}",
            color=COL_RED,
        )
        await try_dm(user, embed=dm_embed)

        await interaction.response.send_message(
            embed=success_embed(f"**{user}** has been jailed. Case #{case_num}")
        )
        await self._log_mod_action(
            interaction.guild, "⛓️ Jail", user, interaction.user, reason, case_num=case_num
        )

    @app_commands.command(name="unjail", description="Release a member from jail")
    @app_commands.describe(user="Member to unjail", reason="Reason")
    @require_fake_perm("moderate_members")
    async def unjail(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        cfg = await self.get_guild_config(interaction.guild.id)
        jail_role = interaction.guild.get_role(cfg.get("jail_role", 0))

        if not jail_role or jail_role not in user.roles:
            return await interaction.response.send_message(
                embed=error_embed(f"{user.mention} is not jailed."), ephemeral=True
            )

        # Remove jail role — restoring original roles is complex, just remove jail role
        await user.remove_roles(jail_role, reason=f"Unjailed by {interaction.user}: {reason}")

        await self.bot.db.execute(
            "DELETE FROM temp_punishments WHERE guild_id=? AND user_id=? AND action='jail'",
            (interaction.guild.id, user.id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            embed=success_embed(f"**{user}** has been released from jail.")
        )
        await self._log_mod_action(
            interaction.guild, "🗝️ Unjail", user, interaction.user, reason, color=COL_GREEN
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /warn
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(user="Member to warn", reason="Reason")
    @require_fake_perm("moderate_members")
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: Optional[str] = "No reason provided",
    ):
        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "warn", reason,
        )

        dm_embed = discord.Embed(
            description=f"You have been **warned** in **{interaction.guild.name}**.\nReason: {reason}",
            color=COL_YELLOW,
        )
        await try_dm(user, embed=dm_embed)

        # Count active warns
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM cases WHERE guild_id=? AND user_id=? AND action='warn' AND active=1",
            (interaction.guild.id, user.id),
        ) as cur:
            row = await cur.fetchone()
        warn_count = row[0]

        # Check escalation
        async with self.bot.db.execute(
            "SELECT * FROM warn_escalation WHERE guild_id=? AND warn_count=?",
            (interaction.guild.id, warn_count),
        ) as cur:
            escalation = await cur.fetchone()

        escalation_msg = ""
        if escalation:
            action = escalation["action"]
            duration = escalation["duration"]
            escalation_msg = f"\n⚠️ Auto-action triggered: **{action}**"
            if action == "mute":
                cfg = await self.get_guild_config(interaction.guild.id)
                mute_role = interaction.guild.get_role(cfg.get("mute_role", 0))
                if mute_role:
                    await user.add_roles(mute_role, reason=f"Warn escalation ({warn_count} warns)")
                    if duration:
                        expires_at = utcnow() + duration
                        await self.bot.db.execute(
                            "INSERT INTO temp_punishments (guild_id,user_id,action,expires_at) VALUES(?,?,?,?)",
                            (interaction.guild.id, user.id, "tempmute", expires_at),
                        )
                        await self.bot.db.commit()
            elif action == "kick":
                await interaction.guild.kick(user, reason=f"Warn escalation ({warn_count} warns)")
            elif action in ("ban", "tempban"):
                await interaction.guild.ban(user, reason=f"Warn escalation ({warn_count} warns)")

        await interaction.response.send_message(
            embed=success_embed(
                f"**{user}** warned. They now have **{warn_count}** warn(s). "
                f"Case #{case_num}{escalation_msg}"
            )
        )
        await self._log_mod_action(
            interaction.guild, "🔮 Warn", user, interaction.user, reason,
            case_num=case_num, color=COL_YELLOW,
        )

    @app_commands.command(name="unwarn", description="Remove a warning from a member")
    @app_commands.describe(user="Member", case_number="Case number of the warning to remove")
    @require_fake_perm("moderate_members")
    async def unwarn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        case_number: int,
    ):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT * FROM cases WHERE guild_id=? AND case_number=? AND user_id=? AND action='warn'",
            (gid, case_number, user.id),
        ) as cur:
            case = await cur.fetchone()

        if not case:
            return await interaction.response.send_message(
                embed=error_embed(f"No warn case #{case_number} found for {user.mention}."),
                ephemeral=True,
            )

        await self.bot.db.execute(
            "UPDATE cases SET active=0 WHERE guild_id=? AND case_number=?",
            (gid, case_number),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Warning case #{case_number} removed from **{user}**.")
        )

    @app_commands.command(name="note", description="Add a staff note to a member (not visible to them)")
    @app_commands.describe(user="Member", note="The note content")
    @require_fake_perm("moderate_members")
    async def note(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        note: str,
    ):
        case_num = await create_case(
            self.bot.db, interaction.guild.id, user.id,
            interaction.user.id, "note", note,
        )
        await interaction.response.send_message(
            embed=success_embed(f"Note added for **{user}**. Case #{case_num}"), ephemeral=True
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /case commands
    # ────────────────────────────────────────────────────────────────────────────

    case_group = app_commands.Group(
        name="case",
        description="View and manage moderation cases",
        guild_only=True,
    )

    @case_group.command(name="view", description="View a specific case")
    @app_commands.describe(case_number="The case number")
    @require_fake_perm("moderate_members")
    async def case_view(self, interaction: discord.Interaction, case_number: int):
        async with self.bot.db.execute(
            "SELECT * FROM cases WHERE guild_id=? AND case_number=?",
            (interaction.guild.id, case_number),
        ) as cur:
            case = await cur.fetchone()

        if not case:
            return await interaction.response.send_message(
                embed=error_embed(f"Case #{case_number} not found."), ephemeral=True
            )

        user = await self.bot.fetch_user(case["user_id"])
        mod = await self.bot.fetch_user(case["mod_id"])

        embed = discord.Embed(
            title=f"Case #{case_number} — {case['action'].title()}",
            color=COL_RED if case["action"] in ("ban","hardban","tempban") else COL_YELLOW,
        )
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=True)
        embed.add_field(name="Moderator", value=f"{mod}", inline=True)
        embed.add_field(name="Reason", value=case["reason"] or "No reason", inline=False)
        if case["duration"]:
            embed.add_field(name="Duration", value=seconds_to_human(case["duration"]), inline=True)
        embed.add_field(name="Active", value="Yes" if case["active"] else "No", inline=True)
        embed.set_footer(text=discord_timestamp(case["created_at"], "f"))

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @case_group.command(name="history", description="View all mod cases for a user")
    @app_commands.describe(user="The user to look up")
    @require_fake_perm("moderate_members")
    async def case_history(self, interaction: discord.Interaction, user: discord.User):
        async with self.bot.db.execute(
            "SELECT * FROM cases WHERE guild_id=? AND user_id=? ORDER BY case_number DESC",
            (interaction.guild.id, user.id),
        ) as cur:
            cases = await cur.fetchall()

        if not cases:
            return await interaction.response.send_message(
                embed=info_embed(f"No cases found for {user.mention}."), ephemeral=True
            )

        items = []
        for c in cases:
            dur = f" ({seconds_to_human(c['duration'])})" if c["duration"] else ""
            active = "" if c["active"] else " ~~"
            items.append(
                f"`#{c['case_number']}` **{c['action'].upper()}**{dur} — "
                f"{c['reason'] or 'No reason'} {discord_timestamp(c['created_at'], 'R')}{active}"
            )

        pages = build_pages(items, title=f"Cases for {user}", per_page=8)
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user)
            await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    @case_group.command(name="reason", description="Edit the reason for a case")
    @app_commands.describe(case_number="Case number", reason="New reason")
    @require_fake_perm("moderate_members")
    async def case_reason(
        self,
        interaction: discord.Interaction,
        case_number: int,
        reason: str,
    ):
        async with self.bot.db.execute(
            "SELECT * FROM cases WHERE guild_id=? AND case_number=?",
            (interaction.guild.id, case_number),
        ) as cur:
            case = await cur.fetchone()

        if not case:
            return await interaction.response.send_message(
                embed=error_embed(f"Case #{case_number} not found."), ephemeral=True
            )

        # Mods can only edit their own cases, admins can edit any
        if case["mod_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("You can only edit your own cases."), ephemeral=True
            )

        await self.bot.db.execute(
            "UPDATE cases SET reason=? WHERE guild_id=? AND case_number=?",
            (reason, interaction.guild.id, case_number),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Case #{case_number} reason updated."), ephemeral=True
        )

    @case_group.command(name="delete", description="Delete a moderation case")
    @app_commands.describe(case_number="Case number to delete")
    @app_commands.checks.has_permissions(administrator=True)
    async def case_delete(self, interaction: discord.Interaction, case_number: int):
        await self.bot.db.execute(
            "DELETE FROM cases WHERE guild_id=? AND case_number=?",
            (interaction.guild.id, case_number),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Case #{case_number} deleted."), ephemeral=True
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /purge
    # ────────────────────────────────────────────────────────────────────────────

    purge_group = app_commands.Group(
        name="purge",
        description="Bulk delete messages",
        guild_only=True,
    )

    @purge_group.command(name="messages", description="Delete a number of messages")
    @app_commands.describe(amount="Number of messages to delete (1-500)")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_messages(self, interaction: discord.Interaction, amount: int):
        if amount < 1 or amount > 500:
            return await interaction.response.send_message(
                embed=error_embed("Amount must be between 1 and 500."), ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** messages."), ephemeral=True
        )

    @purge_group.command(name="user", description="Delete messages from a specific user")
    @app_commands.describe(user="The user", amount="How many messages to scan (up to 500)")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int = 100,
    ):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=min(amount, 500),
            check=lambda m: m.author.id == user.id,
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** messages from {user.mention}."),
            ephemeral=True,
        )

    @purge_group.command(name="bots", description="Delete messages from bots")
    @app_commands.describe(amount="How many messages to scan")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_bots(self, interaction: discord.Interaction, amount: int = 100):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=min(amount, 500),
            check=lambda m: m.author.bot,
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** bot messages."), ephemeral=True
        )

    @purge_group.command(name="contains", description="Delete messages containing specific text")
    @app_commands.describe(text="Text to match", amount="Messages to scan")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_contains(
        self,
        interaction: discord.Interaction,
        text: str,
        amount: int = 100,
    ):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=min(amount, 500),
            check=lambda m: text.lower() in m.content.lower(),
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** messages containing `{text}`."),
            ephemeral=True,
        )

    @purge_group.command(name="attachments", description="Delete messages with attachments")
    @app_commands.describe(amount="Messages to scan")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_attachments(self, interaction: discord.Interaction, amount: int = 100):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=min(amount, 500),
            check=lambda m: len(m.attachments) > 0,
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** messages with attachments."),
            ephemeral=True,
        )

    @purge_group.command(name="embeds", description="Delete messages with embeds")
    @app_commands.describe(amount="Messages to scan")
    @require_fake_perm("manage_messages")
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_embeds(self, interaction: discord.Interaction, amount: int = 100):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=min(amount, 500),
            check=lambda m: len(m.embeds) > 0,
        )
        await interaction.followup.send(
            embed=success_embed(f"Deleted **{len(deleted)}** messages with embeds."),
            ephemeral=True,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /slowmode / /lock / /unlock
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="slowmode", description="Set slowmode on a channel")
    @app_commands.describe(
        seconds="Seconds between messages (0 to disable, max 21600)",
        channel="Channel to set slowmode in (default: current)",
    )
    @require_fake_perm("manage_channels")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: int,
        channel: Optional[discord.TextChannel] = None,
    ):
        ch = channel or interaction.channel
        if seconds < 0 or seconds > 21600:
            return await interaction.response.send_message(
                embed=error_embed("Slowmode must be between 0 and 21600 seconds."), ephemeral=True
            )
        await ch.edit(slowmode_delay=seconds)
        if seconds == 0:
            msg = f"Slowmode **disabled** in {ch.mention}."
        else:
            msg = f"Slowmode set to **{seconds}s** in {ch.mention}."
        await interaction.response.send_message(embed=success_embed(msg))

    @app_commands.command(name="lock", description="Lock a channel (prevent @everyone from sending)")
    @app_commands.describe(
        channel="Channel to lock (default: current)",
        reason="Reason",
    )
    @require_fake_perm("manage_channels")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        reason: Optional[str] = "No reason provided",
    ):
        ch = channel or interaction.channel
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"🔒 {ch.mention} has been locked.")
        )

    @app_commands.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(
        channel="Channel to unlock (default: current)",
        reason="Reason",
    )
    @require_fake_perm("manage_channels")
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        reason: Optional[str] = "No reason provided",
    ):
        ch = channel or interaction.channel
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
        await interaction.response.send_message(
            embed=success_embed(f"🔓 {ch.mention} has been unlocked.")
        )

    @app_commands.command(name="lockdown", description="Lock ALL channels in the server")
    @app_commands.describe(reason="Reason for lockdown")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lockdown(
        self,
        interaction: discord.Interaction,
        reason: Optional[str] = "Server lockdown",
    ):
        await interaction.response.defer()
        locked = 0
        for channel in interaction.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(interaction.guild.default_role)
                overwrite.send_messages = False
                await channel.set_permissions(
                    interaction.guild.default_role, overwrite=overwrite, reason=reason
                )
                locked += 1
            except discord.Forbidden:
                pass
        await interaction.followup.send(
            embed=success_embed(f"🔒 **Lockdown active.** Locked {locked} channels.\nReason: {reason}")
        )

    @app_commands.command(name="unlockdown", description="Unlock ALL channels (lift lockdown)")
    @app_commands.describe(reason="Reason")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlockdown(
        self,
        interaction: discord.Interaction,
        reason: Optional[str] = "Lockdown lifted",
    ):
        await interaction.response.defer()
        unlocked = 0
        for channel in interaction.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(interaction.guild.default_role)
                overwrite.send_messages = None
                await channel.set_permissions(
                    interaction.guild.default_role, overwrite=overwrite, reason=reason
                )
                unlocked += 1
            except discord.Forbidden:
                pass
        await interaction.followup.send(
            embed=success_embed(f"🔓 Lockdown lifted. Unlocked {unlocked} channels.")
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /warn escalation config
    # ────────────────────────────────────────────────────────────────────────────

    escalation = app_commands.Group(
        name="escalation",
        description="Configure automatic warn escalation actions",
        guild_only=True,
    )

    @escalation.command(name="set", description="Set an auto-action when a member reaches N warns")
    @app_commands.describe(
        warn_count="Number of warns to trigger on",
        action="Action to take",
        duration="Duration for mute/tempban (e.g. 1h, 7d). Leave empty for permanent.",
    )
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in ("mute", "kick", "tempban", "ban")],
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def escalation_set(
        self,
        interaction: discord.Interaction,
        warn_count: int,
        action: str,
        duration: Optional[str] = None,
    ):
        secs = parse_duration(duration) if duration else None
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO warn_escalation (guild_id, warn_count, action, duration) VALUES (?,?,?,?)",
            (interaction.guild.id, warn_count, action, secs),
        )
        await self.bot.db.commit()
        dur_str = f" for {seconds_to_human(secs)}" if secs else ""
        await interaction.response.send_message(
            embed=success_embed(
                f"At **{warn_count} warns** → **{action}**{dur_str}."
            ),
            ephemeral=True,
        )

    @escalation.command(name="remove", description="Remove an escalation rule")
    @app_commands.describe(warn_count="Which warn count to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def escalation_remove(self, interaction: discord.Interaction, warn_count: int):
        await self.bot.db.execute(
            "DELETE FROM warn_escalation WHERE guild_id=? AND warn_count=?",
            (interaction.guild.id, warn_count),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Escalation rule at {warn_count} warns removed."), ephemeral=True
        )

    @escalation.command(name="list", description="View all escalation rules")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def escalation_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM warn_escalation WHERE guild_id=? ORDER BY warn_count",
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No escalation rules set. Use `/escalation set` to add one."),
                ephemeral=True,
            )

        lines = []
        for r in rows:
            dur_str = f" for {seconds_to_human(r['duration'])}" if r["duration"] else ""
            lines.append(f"**{r['warn_count']} warns** → `{r['action']}`{dur_str}")

        await interaction.response.send_message(
            embed=info_embed("\n".join(lines), title="Warn Escalation Rules"),
            ephemeral=True,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /nick
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="nick", description="Change a member's nickname")
    @app_commands.describe(user="Member", nickname="New nickname (leave empty to reset)")
    @require_fake_perm("manage_nicknames")
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    async def nick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        nickname: Optional[str] = None,
    ):
        await user.edit(nick=nickname, reason=f"Nick changed by {interaction.user}")
        if nickname:
            msg = f"**{user.name}**'s nickname set to `{nickname}`."
        else:
            msg = f"**{user.name}**'s nickname reset."
        await interaction.response.send_message(embed=success_embed(msg))

    # ────────────────────────────────────────────────────────────────────────────
    # /banlist
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="banlist", description="View the server's ban list")
    @require_fake_perm("ban_members")
    async def banlist(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bans = [entry async for entry in interaction.guild.bans()]
        if not bans:
            return await interaction.followup.send(
                embed=info_embed("No bans on this server."), ephemeral=True
            )

        items = [f"`{entry.user.id}` **{entry.user}** — {entry.reason or 'No reason'}" for entry in bans]
        pages = build_pages(items, title=f"Ban List ({len(bans)} total)", per_page=15)

        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user)
            await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
