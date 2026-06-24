"""
cogs/voicemaster.py — Temporary voice channel system
Hub channel "join to create", interface channel with control buttons,
temp channel lifecycle, owner controls, lock/ghost/limit/rename/transfer.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed, utcnow,
    safe_send, resolve_variables,
    COL_BLUE,
)

log = logging.getLogger("modbot.voicemaster")


class VoiceMasterControls(discord.ui.View):
    """Persistent button interface for temp channel owners."""

    def __init__(self):
        super().__init__(timeout=None)

    async def get_owner_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        db = interaction.client.db
        async with db.execute(
            "SELECT channel_id FROM temp_channels WHERE guild_id=? AND owner_id=?",
            (interaction.guild.id, interaction.user.id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return interaction.guild.get_channel(row["channel_id"])

    async def check_owner(self, interaction: discord.Interaction) -> bool:
        ch = await self.get_owner_channel(interaction)
        if not ch:
            await interaction.response.send_message(
                "You don't own a temp channel.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔒 Lock", style=discord.ButtonStyle.secondary, custom_id="vm_lock", row=0)
    async def lock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.connect = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("🔒 Channel locked.", ephemeral=True)

    @discord.ui.button(label="🔓 Unlock", style=discord.ButtonStyle.secondary, custom_id="vm_unlock", row=0)
    async def unlock_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.connect = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("🔓 Channel unlocked.", ephemeral=True)

    @discord.ui.button(label="👁️ Ghost", style=discord.ButtonStyle.secondary, custom_id="vm_ghost", row=0)
    async def ghost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.view_channel = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("👻 Channel is now hidden.", ephemeral=True)

    @discord.ui.button(label="👁️ Unghost", style=discord.ButtonStyle.secondary, custom_id="vm_unghost", row=0)
    async def unghost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        overwrite = ch.overwrites_for(interaction.guild.default_role)
        overwrite.view_channel = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message("👁️ Channel is now visible.", ephemeral=True)

    @discord.ui.button(label="✏️ Rename", style=discord.ButtonStyle.primary, custom_id="vm_rename", row=1)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        modal = RenameModal(ch)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="👥 Limit", style=discord.ButtonStyle.primary, custom_id="vm_limit", row=1)
    async def limit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        modal = LimitModal(ch)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="➕ Permit", style=discord.ButtonStyle.success, custom_id="vm_permit", row=1)
    async def permit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        await interaction.response.send_message(
            "Mention a member to permit them to join:", ephemeral=True
        )

    @discord.ui.button(label="🚫 Reject", style=discord.ButtonStyle.danger, custom_id="vm_reject", row=1)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        await interaction.response.send_message(
            "Mention a member to reject from your channel:", ephemeral=True
        )

    @discord.ui.button(label="🔄 Transfer", style=discord.ButtonStyle.secondary, custom_id="vm_transfer", row=2)
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = await self.get_owner_channel(interaction)
        if not ch:
            return await interaction.response.send_message("You don't own a temp channel.", ephemeral=True)
        await interaction.response.send_message(
            "Mention a member currently in your channel to transfer ownership:", ephemeral=True
        )

    @discord.ui.button(label="⛏️ Claim", style=discord.ButtonStyle.secondary, custom_id="vm_claim", row=2)
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        # Find any temp channel the user is currently in
        member = interaction.user
        if not member.voice or not member.voice.channel:
            return await interaction.response.send_message("You're not in a voice channel.", ephemeral=True)

        vc = member.voice.channel
        async with db.execute(
            "SELECT owner_id FROM temp_channels WHERE channel_id=?", (vc.id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message("That isn't a temp channel.", ephemeral=True)

        owner = interaction.guild.get_member(row["owner_id"])
        if owner and owner.voice and owner.voice.channel == vc:
            return await interaction.response.send_message("The owner is still in the channel.", ephemeral=True)

        await db.execute(
            "UPDATE temp_channels SET owner_id=? WHERE channel_id=?", (member.id, vc.id)
        )
        await db.commit()
        await interaction.response.send_message(f"You are now the owner of **{vc.name}**.", ephemeral=True)


class RenameModal(discord.ui.Modal, title="Rename Channel"):
    name = discord.ui.TextInput(label="New name", max_length=100)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.edit(name=self.name.value)
            await interaction.response.send_message(f"✅ Renamed to **{self.name.value}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ No permission to rename.", ephemeral=True)


class LimitModal(discord.ui.Modal, title="Set User Limit"):
    limit = discord.ui.TextInput(label="User limit (0 = no limit)", max_length=3)

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.limit.value)
            await self.channel.edit(user_limit=val)
            msg = f"✅ Limit set to **{val}**." if val > 0 else "✅ Limit removed."
            await interaction.response.send_message(msg, ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid number.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ No permission.", ephemeral=True)


class VoiceMaster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cleanup_empty.start()
        # Register persistent view
        bot.add_view(VoiceMasterControls())

    def cog_unload(self):
        self._cleanup_empty.cancel()

    @tasks.loop(minutes=2)
    async def _cleanup_empty(self):
        """Delete empty temp channels."""
        async with self.bot.db.execute("SELECT * FROM temp_channels") as cur:
            rows = await cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            ch = guild.get_channel(row["channel_id"])
            if not ch:
                await self.bot.db.execute(
                    "DELETE FROM temp_channels WHERE channel_id=?", (row["channel_id"],)
                )
                continue
            if len(ch.members) == 0:
                try:
                    await ch.delete(reason="Temp channel empty")
                except discord.HTTPException:
                    pass
                await self.bot.db.execute(
                    "DELETE FROM temp_channels WHERE channel_id=?", (row["channel_id"],)
                )

        await self.bot.db.commit()

    @_cleanup_empty.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ── Voice state listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild

        async with self.bot.db.execute(
            "SELECT * FROM voicemaster_config WHERE guild_id=?", (guild.id,)
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg:
            return

        # ── Joined hub channel → create temp VC ─────────────────────────────
        if after.channel and after.channel.id == cfg["hub_channel_id"]:
            name = resolve_variables(
                cfg["default_name"] or "{user.display_name}'s vc",
                member, guild,
            )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=True),
                member: discord.PermissionOverwrite(
                    connect=True, mute_members=True, deafen_members=True,
                    move_members=True, manage_channels=True,
                ),
                guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True),
            }
            category = guild.get_channel(cfg["category_id"]) if cfg["category_id"] else after.channel.category

            try:
                new_ch = await guild.create_voice_channel(
                    name=name,
                    category=category,
                    overwrites=overwrites,
                    bitrate=min((cfg["default_bitrate"] or 64) * 1000, guild.bitrate_limit),
                    reason=f"VoiceMaster: {member}",
                )
                if cfg.get("default_region"):
                    try:
                        await new_ch.edit(rtc_region=cfg["default_region"])
                    except Exception:
                        pass

                await member.move_to(new_ch, reason="VoiceMaster temp channel")

                await self.bot.db.execute(
                    "INSERT OR REPLACE INTO temp_channels (channel_id, guild_id, owner_id, created_at) VALUES (?,?,?,?)",
                    (new_ch.id, guild.id, member.id, utcnow()),
                )
                await self.bot.db.commit()

                # Assign join role if configured
                if cfg.get("join_role_id"):
                    join_role = guild.get_role(cfg["join_role_id"])
                    if join_role:
                        try:
                            await member.add_roles(join_role, reason="VoiceMaster join role")
                        except discord.Forbidden:
                            pass

            except discord.Forbidden:
                log.warning(f"VoiceMaster: No permission to create channel in {guild}")
            except Exception as e:
                log.error(f"VoiceMaster channel creation error: {e}")

        # ── Left a temp channel → clean up if empty ──────────────────────────
        if before.channel and before.channel != after.channel:
            async with self.bot.db.execute(
                "SELECT 1 FROM temp_channels WHERE channel_id=?", (before.channel.id,)
            ) as cur:
                is_temp = await cur.fetchone()

            if is_temp and len(before.channel.members) == 0:
                try:
                    await before.channel.delete(reason="VoiceMaster: empty temp channel")
                except discord.HTTPException:
                    pass
                await self.bot.db.execute(
                    "DELETE FROM temp_channels WHERE channel_id=?", (before.channel.id,)
                )
                await self.bot.db.commit()

            # Remove join role if configured
            if cfg.get("join_role_id") and not after.channel:
                join_role = guild.get_role(cfg["join_role_id"])
                if join_role and join_role in member.roles:
                    try:
                        await member.remove_roles(join_role, reason="VoiceMaster: left VC")
                    except discord.Forbidden:
                        pass

    # ────────────────────────────────────────────────────────────────────────────
    # /voicemaster commands
    # ────────────────────────────────────────────────────────────────────────────

    vm_group = app_commands.Group(
        name="voicemaster",
        description="Configure the VoiceMaster system",
        guild_only=True,
    )

    @vm_group.command(name="setup", description="Set up the VoiceMaster system")
    @app_commands.describe(
        hub_channel="The 'Join to Create' voice channel",
        interface_channel="Text channel for the control panel",
        category="Category for temp channels (optional)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def vm_setup(
        self,
        interaction: discord.Interaction,
        hub_channel: discord.VoiceChannel,
        interface_channel: discord.TextChannel,
        category: Optional[discord.CategoryChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # Post the control panel interface
        embed = discord.Embed(
            title="🎙️ VoiceMaster — Channel Controls",
            description=(
                "Use the buttons below to control your temporary voice channel.\n\n"
                "**🔒 Lock** — Only allowed users can join\n"
                "**🔓 Unlock** — Anyone can join\n"
                "**👁️ Ghost** — Hide channel from others\n"
                "**👁️ Unghost** — Make visible again\n"
                "**✏️ Rename** — Change channel name\n"
                "**👥 Limit** — Set user limit\n"
                "**➕ Permit** — Allow a specific user\n"
                "**🚫 Reject** — Kick & block a user\n"
                "**🔄 Transfer** — Give ownership\n"
                "**⛏️ Claim** — Claim an ownerless channel"
            ),
            color=COL_BLUE,
        )
        view = VoiceMasterControls()
        interface_msg = await interface_channel.send(embed=embed, view=view)

        await self.bot.db.execute(
            """INSERT OR REPLACE INTO voicemaster_config
               (guild_id, hub_channel_id, interface_channel_id, category_id)
               VALUES (?,?,?,?)""",
            (
                interaction.guild.id,
                hub_channel.id,
                interface_msg.channel.id,
                category.id if category else None,
            ),
        )
        await self.bot.db.commit()

        await interaction.followup.send(
            embed=success_embed(
                f"VoiceMaster configured!\n"
                f"Hub: {hub_channel.mention}\n"
                f"Interface: {interface_channel.mention}"
            ),
            ephemeral=True,
        )

    @vm_group.command(name="defaultname", description="Set the default name for new temp channels")
    @app_commands.describe(name="Name template (supports {user.display_name} etc.)")
    @app_commands.checks.has_permissions(administrator=True)
    async def vm_defaultname(self, interaction: discord.Interaction, name: str):
        await self.bot.db.execute(
            "UPDATE voicemaster_config SET default_name=? WHERE guild_id=?",
            (name, interaction.guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Default channel name set to: `{name}`"), ephemeral=True
        )

    @vm_group.command(name="defaultbitrate", description="Set the default bitrate for temp channels")
    @app_commands.describe(bitrate="Bitrate in kbps (e.g. 64)")
    @app_commands.checks.has_permissions(administrator=True)
    async def vm_bitrate(self, interaction: discord.Interaction, bitrate: int):
        if bitrate < 8 or bitrate > 384:
            return await interaction.response.send_message(
                embed=error_embed("Bitrate must be between 8 and 384 kbps."), ephemeral=True
            )
        await self.bot.db.execute(
            "UPDATE voicemaster_config SET default_bitrate=? WHERE guild_id=?",
            (bitrate, interaction.guild.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Default bitrate set to `{bitrate} kbps`."), ephemeral=True
        )

    @vm_group.command(name="joinrole", description="Set a role given when members join any temp VC")
    @app_commands.describe(role="Role to assign (or the same role to remove it)")
    @app_commands.checks.has_permissions(administrator=True)
    async def vm_joinrole(self, interaction: discord.Interaction, role: discord.Role):
        async with self.bot.db.execute(
            "SELECT join_role_id FROM voicemaster_config WHERE guild_id=?", (interaction.guild.id,)
        ) as cur:
            row = await cur.fetchone()

        if row and row["join_role_id"] == role.id:
            await self.bot.db.execute(
                "UPDATE voicemaster_config SET join_role_id=NULL WHERE guild_id=?",
                (interaction.guild.id,),
            )
            msg = f"{role.mention} removed as join role."
        else:
            await self.bot.db.execute(
                "UPDATE voicemaster_config SET join_role_id=? WHERE guild_id=?",
                (role.id, interaction.guild.id),
            )
            msg = f"{role.mention} will be given to members in temp VCs."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @vm_group.command(name="disable", description="Disable the VoiceMaster system")
    @app_commands.checks.has_permissions(administrator=True)
    async def vm_disable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM voicemaster_config WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("VoiceMaster disabled."), ephemeral=True
        )

    # ── Slash commands for channel owners ────────────────────────────────────────

    vc_group = app_commands.Group(name="vc", description="Control your temp voice channel", guild_only=True)

    async def get_own_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        async with self.bot.db.execute(
            "SELECT channel_id FROM temp_channels WHERE guild_id=? AND owner_id=?",
            (interaction.guild.id, interaction.user.id),
        ) as cur:
            row = await cur.fetchone()
        return interaction.guild.get_channel(row["channel_id"]) if row else None

    @vc_group.command(name="rename", description="Rename your temp channel")
    @app_commands.describe(name="New channel name")
    async def vc_rename(self, interaction: discord.Interaction, name: str):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        await ch.edit(name=name)
        await interaction.response.send_message(embed=success_embed(f"Renamed to **{name}**."))

    @vc_group.command(name="limit", description="Set a user limit for your channel")
    @app_commands.describe(limit="Max users (0 = no limit)")
    async def vc_limit(self, interaction: discord.Interaction, limit: int):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        await ch.edit(user_limit=limit)
        await interaction.response.send_message(
            embed=success_embed(f"User limit set to **{limit}**." if limit else "Limit removed.")
        )

    @vc_group.command(name="lock", description="Lock your temp channel")
    async def vc_lock(self, interaction: discord.Interaction):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.connect = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        await interaction.response.send_message(embed=success_embed("🔒 Channel locked."))

    @vc_group.command(name="unlock", description="Unlock your temp channel")
    async def vc_unlock(self, interaction: discord.Interaction):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.connect = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        await interaction.response.send_message(embed=success_embed("🔓 Channel unlocked."))

    @vc_group.command(name="permit", description="Allow a specific member to join your locked channel")
    @app_commands.describe(member="Member to permit")
    async def vc_permit(self, interaction: discord.Interaction, member: discord.Member):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        await ch.set_permissions(member, connect=True, view_channel=True)
        await interaction.response.send_message(embed=success_embed(f"{member.mention} can now join."))

    @vc_group.command(name="reject", description="Remove a member from your channel and block them")
    @app_commands.describe(member="Member to reject")
    async def vc_reject(self, interaction: discord.Interaction, member: discord.Member):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        await ch.set_permissions(member, connect=False, view_channel=False)
        if member.voice and member.voice.channel == ch:
            try:
                await member.move_to(None, reason="Rejected from temp channel")
            except discord.Forbidden:
                pass
        await interaction.response.send_message(embed=success_embed(f"{member.mention} rejected."))

    @vc_group.command(name="transfer", description="Transfer ownership of your channel to someone else")
    @app_commands.describe(member="New owner (must be in your channel)")
    async def vc_transfer(self, interaction: discord.Interaction, member: discord.Member):
        ch = await self.get_own_channel(interaction)
        if not ch:
            return await interaction.response.send_message(
                embed=error_embed("You don't own a temp channel."), ephemeral=True
            )
        if member.id == interaction.user.id:
            return await interaction.response.send_message(
                embed=error_embed("You're already the owner."), ephemeral=True
            )
        if not member.voice or member.voice.channel != ch:
            return await interaction.response.send_message(
                embed=error_embed(f"{member.mention} must be in your channel."), ephemeral=True
            )

        await self.bot.db.execute(
            "UPDATE temp_channels SET owner_id=? WHERE channel_id=?", (member.id, ch.id)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Ownership transferred to {member.mention}.")
        )


async def setup(bot):
    await bot.add_cog(VoiceMaster(bot))
