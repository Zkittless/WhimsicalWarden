"""
cogs/configuration.py — Server configuration features
Welcome/goodbye/boost messages, autoresponders, reaction roles,
button roles, starboard, counters, bump reminder, auto-messages,
reaction triggers, vanity roles, booster roles, embed builder.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed, warn_embed,
    resolve_variables, parse_embed_script, utcnow,
    safe_send, build_pages, Paginator, to_json, from_json,
    COL_BLUE, COL_GREEN, COL_YELLOW,
)

log = logging.getLogger("modbot.configuration")


class Configuration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._bump_check.start()
        self._auto_messages.start()
        self._counter_update.start()

    def cog_unload(self):
        self._bump_check.cancel()
        self._auto_messages.cancel()
        self._counter_update.cancel()

    # ────────────────────────────────────────────────────────────────────────────
    # Background tasks
    # ────────────────────────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _bump_check(self):
        """Check if 2 hours have passed since last bump and remind."""
        now = utcnow()
        async with self.bot.db.execute(
            "SELECT * FROM bump_config WHERE reminder_sent=0 AND last_bump IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            if now - row["last_bump"] >= 7200:  # 2 hours
                guild = self.bot.get_guild(row["guild_id"])
                if not guild:
                    continue
                ch = guild.get_channel(row["channel_id"])
                if not ch:
                    continue
                msg = row["message"] or "Time to bump! Use `/bump` to bump the server."
                role_mention = f"<@&{row['role_id']}> " if row["role_id"] else ""
                await safe_send(ch, content=f"{role_mention}{msg}")
                await self.bot.db.execute(
                    "UPDATE bump_config SET reminder_sent=1 WHERE guild_id=?", (row["guild_id"],)
                )
        await self.bot.db.commit()

    @tasks.loop(minutes=1)
    async def _auto_messages(self):
        """Send auto-messages on their interval."""
        now = utcnow()
        async with self.bot.db.execute(
            "SELECT * FROM auto_messages WHERE last_sent + interval <= ?", (now,)
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            ch = guild.get_channel(row["channel_id"])
            if not ch:
                continue
            embed = parse_embed_script(row["message"])
            if embed:
                await safe_send(ch, embed=embed)
            else:
                await safe_send(ch, content=row["message"])
            await self.bot.db.execute(
                "UPDATE auto_messages SET last_sent=? WHERE id=?", (now, row["id"])
            )
        await self.bot.db.commit()

    @tasks.loop(minutes=5)
    async def _counter_update(self):
        """Update all stat counter channels."""
        async with self.bot.db.execute("SELECT * FROM counters") as cur:
            rows = await cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if not guild:
                continue
            ch = guild.get_channel(row["channel_id"])
            if not ch:
                continue
            name = self._format_counter(guild, row["counter_type"], row["format"])
            try:
                await ch.edit(name=name)
            except discord.Forbidden:
                pass

    def _format_counter(self, guild: discord.Guild, ctype: str, fmt: str) -> str:
        values = {
            "members":  str(guild.member_count),
            "bots":     str(sum(1 for m in guild.members if m.bot)),
            "humans":   str(sum(1 for m in guild.members if not m.bot)),
            "online":   str(sum(1 for m in guild.members if m.status != discord.Status.offline)),
            "channels": str(len(guild.channels)),
            "roles":    str(len(guild.roles)),
            "boosts":   str(guild.premium_subscription_count),
        }
        count = values.get(ctype, "0")
        if fmt:
            return fmt.replace("{count}", count)
        return f"{ctype.title()}: {count}"

    @_bump_check.before_loop
    @_auto_messages.before_loop
    @_counter_update.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    # ────────────────────────────────────────────────────────────────────────────
    # Welcome / Goodbye / Boost messages
    # ────────────────────────────────────────────────────────────────────────────

    welcome_group = app_commands.Group(name="welcome", description="Welcome message settings", guild_only=True)
    goodbye_group = app_commands.Group(name="goodbye", description="Goodbye message settings", guild_only=True)
    boost_group   = app_commands.Group(name="boost",   description="Boost message settings",   guild_only=True)

    async def _set_system_message(
        self,
        interaction: discord.Interaction,
        event_type: str,
        channel: discord.TextChannel,
        message: str,
        self_destruct: int = 0,
    ):
        gid = interaction.guild.id
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO system_messages
               (guild_id, event_type, channel_id, message, self_destruct)
               VALUES (?,?,?,?,?)""",
            (gid, event_type, channel.id, message, self_destruct),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                f"{event_type.title()} message set in {channel.mention}.\n"
                f"Preview: {message[:100]}"
            ),
            ephemeral=True,
        )

    @welcome_group.command(name="set", description="Set the welcome message")
    @app_commands.describe(
        channel="Channel to send welcome messages in",
        message="Message content (supports variables: {user.mention}, {guild.name}, etc.)",
        self_destruct="Seconds before the message deletes itself (0 = never)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        self_destruct: Optional[int] = 0,
    ):
        await self._set_system_message(interaction, "welcome", channel, message, self_destruct)

    @welcome_group.command(name="test", description="Preview the welcome message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_test(self, interaction: discord.Interaction):
        await self._test_system_message(interaction, "welcome")

    @welcome_group.command(name="disable", description="Disable welcome messages")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_disable(self, interaction: discord.Interaction):
        await self._disable_system_message(interaction, "welcome")

    @goodbye_group.command(name="set", description="Set the goodbye message")
    @app_commands.describe(
        channel="Channel for goodbye messages",
        message="Message content (supports variables)",
        self_destruct="Seconds before auto-delete (0 = never)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        self_destruct: Optional[int] = 0,
    ):
        await self._set_system_message(interaction, "goodbye", channel, message, self_destruct)

    @goodbye_group.command(name="test", description="Preview the goodbye message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_test(self, interaction: discord.Interaction):
        await self._test_system_message(interaction, "goodbye")

    @goodbye_group.command(name="disable", description="Disable goodbye messages")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_disable(self, interaction: discord.Interaction):
        await self._disable_system_message(interaction, "goodbye")

    @boost_group.command(name="set", description="Set the server boost message")
    @app_commands.describe(
        channel="Channel for boost messages",
        message="Message content (supports variables)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boost_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
    ):
        await self._set_system_message(interaction, "boost", channel, message)

    @boost_group.command(name="test", description="Preview the boost message")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boost_test(self, interaction: discord.Interaction):
        await self._test_system_message(interaction, "boost")

    @boost_group.command(name="disable", description="Disable boost messages")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boost_disable(self, interaction: discord.Interaction):
        await self._disable_system_message(interaction, "boost")

    async def _test_system_message(self, interaction: discord.Interaction, event_type: str):
        async with self.bot.db.execute(
            "SELECT * FROM system_messages WHERE guild_id=? AND event_type=?",
            (interaction.guild.id, event_type),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message(
                embed=error_embed(f"No {event_type} message configured."), ephemeral=True
            )
        resolved = resolve_variables(row["message"], interaction.user, interaction.guild)
        embed = parse_embed_script(resolved)
        if embed:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(content=resolved, ephemeral=True)

    async def _disable_system_message(self, interaction: discord.Interaction, event_type: str):
        await self.bot.db.execute(
            "DELETE FROM system_messages WHERE guild_id=? AND event_type=?",
            (interaction.guild.id, event_type),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"{event_type.title()} messages disabled."), ephemeral=True
        )

    # ── Event listeners for system messages ─────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._fire_system_message("welcome", member, member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._fire_system_message("goodbye", member, member.guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is None and after.premium_since is not None:
            await self._fire_system_message("boost", after, after.guild)

    async def _fire_system_message(
        self,
        event_type: str,
        member: discord.Member,
        guild: discord.Guild,
    ):
        async with self.bot.db.execute(
            "SELECT * FROM system_messages WHERE guild_id=? AND event_type=?",
            (guild.id, event_type),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return

        ch = guild.get_channel(row["channel_id"])
        if not ch:
            return

        resolved = resolve_variables(row["message"], member, guild)
        embed = parse_embed_script(resolved)
        if embed:
            msg = await safe_send(ch, embed=embed)
        else:
            msg = await safe_send(ch, content=resolved)

        if msg and row["self_destruct"] > 0:
            await asyncio.sleep(row["self_destruct"])
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    # ────────────────────────────────────────────────────────────────────────────
    # Autoresponders
    # ────────────────────────────────────────────────────────────────────────────

    ar_group = app_commands.Group(name="autoresponder", description="Manage autoresponders", guild_only=True)

    @ar_group.command(name="add", description="Add an autoresponder trigger")
    @app_commands.describe(
        trigger="Text that triggers the response",
        response="Response to send (supports embed scripts)",
        not_strict="Match anywhere in message (not just exact match)",
        self_destruct="Delete response after N seconds (0 = never)",
        delete_trigger="Delete the triggering message",
        reply="Reply to the triggering message",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ar_add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        response: str,
        not_strict: Optional[bool] = False,
        self_destruct: Optional[int] = 0,
        delete_trigger: Optional[bool] = False,
        reply: Optional[bool] = False,
    ):
        try:
            await self.bot.db.execute(
                """INSERT OR REPLACE INTO autoresponders
                   (guild_id, trigger, response, not_strict, self_destruct, delete_trigger, reply)
                   VALUES (?,?,?,?,?,?,?)""",
                (interaction.guild.id, trigger.lower(), response,
                 int(not_strict), self_destruct, int(delete_trigger), int(reply)),
            )
            await self.bot.db.commit()
        except Exception:
            return await interaction.response.send_message(
                embed=error_embed("Failed to add autoresponder."), ephemeral=True
            )

        await interaction.response.send_message(
            embed=success_embed(f"Autoresponder for `{trigger}` added."), ephemeral=True
        )

    @ar_group.command(name="remove", description="Remove an autoresponder")
    @app_commands.describe(trigger="The trigger to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ar_remove(self, interaction: discord.Interaction, trigger: str):
        await self.bot.db.execute(
            "DELETE FROM autoresponders WHERE guild_id=? AND trigger=?",
            (interaction.guild.id, trigger.lower()),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Autoresponder `{trigger}` removed."), ephemeral=True
        )

    @ar_group.command(name="list", description="View all autoresponders")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ar_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT trigger, not_strict, self_destruct FROM autoresponders WHERE guild_id=? ORDER BY trigger",
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No autoresponders configured."), ephemeral=True
            )

        items = []
        for r in rows:
            flags = []
            if r["not_strict"]: flags.append("not_strict")
            if r["self_destruct"]: flags.append(f"sd:{r['self_destruct']}s")
            flag_str = f" `[{', '.join(flags)}]`" if flags else ""
            items.append(f"`{r['trigger']}`{flag_str}")

        pages = build_pages(items, title="Autoresponders", per_page=15)
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user)
            await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        async with self.bot.db.execute(
            "SELECT * FROM autoresponders WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            responders = await cur.fetchall()

        content_lower = message.content.lower()
        for ar in responders:
            trigger = ar["trigger"]
            if ar["not_strict"]:
                matched = trigger in content_lower
            else:
                matched = content_lower.strip() == trigger

            if not matched:
                continue

            # Delete trigger message if configured
            if ar["delete_trigger"]:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

            response = resolve_variables(ar["response"], message.author, message.guild)
            embed = parse_embed_script(response)

            if ar["reply"] and not ar["delete_trigger"]:
                try:
                    if embed:
                        msg = await message.reply(embed=embed)
                    else:
                        msg = await message.reply(response)
                except discord.HTTPException:
                    msg = await safe_send(message.channel, content=response, embed=embed)
            else:
                if embed:
                    msg = await safe_send(message.channel, embed=embed)
                else:
                    msg = await safe_send(message.channel, content=response)

            if msg and ar["self_destruct"] > 0:
                await asyncio.sleep(ar["self_destruct"])
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
            break

    # ── Reaction triggers ─────────────────────────────────────────────────────────

    rxntrigger_group = app_commands.Group(
        name="reactiontrigger",
        description="Auto-react to messages containing a keyword",
        guild_only=True,
    )

    @rxntrigger_group.command(name="add", description="Add a reaction trigger")
    @app_commands.describe(
        trigger="Word to trigger on",
        emoji="Emoji to react with",
        not_strict="Match anywhere in message",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rxntrigger_add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        emoji: str,
        not_strict: Optional[bool] = False,
    ):
        try:
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO reaction_triggers (guild_id, trigger, emoji, not_strict) VALUES (?,?,?,?)",
                (interaction.guild.id, trigger.lower(), emoji, int(not_strict)),
            )
            await self.bot.db.commit()
            await interaction.response.send_message(
                embed=success_embed(f"Reaction trigger `{trigger}` → {emoji} added."), ephemeral=True
            )
        except Exception:
            await interaction.response.send_message(
                embed=error_embed("Failed. Check the emoji is valid."), ephemeral=True
            )

    @rxntrigger_group.command(name="remove", description="Remove a reaction trigger")
    @app_commands.describe(trigger="Trigger to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rxntrigger_remove(self, interaction: discord.Interaction, trigger: str):
        await self.bot.db.execute(
            "DELETE FROM reaction_triggers WHERE guild_id=? AND trigger=?",
            (interaction.guild.id, trigger.lower()),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Reaction trigger `{trigger}` removed."), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message_reaction_trigger(self, message: discord.Message):
        """Separated listener for reaction triggers to avoid on_message conflicts."""
        if not message.guild or message.author.bot:
            return
        async with self.bot.db.execute(
            "SELECT * FROM reaction_triggers WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            triggers = await cur.fetchall()

        content_lower = message.content.lower()
        for t in triggers:
            if t["not_strict"]:
                matched = t["trigger"] in content_lower
            else:
                matched = content_lower.strip() == t["trigger"]
            if matched:
                try:
                    await message.add_reaction(t["emoji"])
                except discord.HTTPException:
                    pass

    # Override on_message to also handle reaction triggers
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):  # noqa: F811
        if not message.guild or message.author.bot:
            return

        # Autoresponders (handled above)
        # Reaction triggers
        async with self.bot.db.execute(
            "SELECT * FROM reaction_triggers WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            triggers = await cur.fetchall()

        content_lower = message.content.lower()
        for t in triggers:
            if t["not_strict"]:
                matched = t["trigger"] in content_lower
            else:
                matched = content_lower.strip() == t["trigger"]
            if matched:
                try:
                    await message.add_reaction(t["emoji"])
                except discord.HTTPException:
                    pass

    # ────────────────────────────────────────────────────────────────────────────
    # Reaction Roles
    # ────────────────────────────────────────────────────────────────────────────

    reactionrole_group = app_commands.Group(
        name="reactionrole", description="Manage reaction roles", guild_only=True
    )

    @reactionrole_group.command(name="add", description="Add a reaction role to a message")
    @app_commands.describe(
        message_id="The message ID",
        channel="Channel the message is in",
        emoji="The emoji to react with",
        role="The role to assign",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rr_add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: discord.TextChannel,
        emoji: str,
        role: discord.Role,
    ):
        try:
            msg = await channel.fetch_message(int(message_id))
        except (discord.NotFound, ValueError):
            return await interaction.response.send_message(
                embed=error_embed("Message not found."), ephemeral=True
            )

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO reaction_roles (guild_id, channel_id, message_id, emoji, role_id) VALUES (?,?,?,?,?)",
            (interaction.guild.id, channel.id, msg.id, emoji, role.id),
        )
        await self.bot.db.commit()

        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            embed=success_embed(f"Reaction role: {emoji} → {role.mention} on [message]({msg.jump_url})"),
            ephemeral=True,
        )

    @reactionrole_group.command(name="remove", description="Remove a reaction role")
    @app_commands.describe(message_id="Message ID", emoji="The emoji")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rr_remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        await self.bot.db.execute(
            "DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (interaction.guild.id, int(message_id), emoji),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Reaction role {emoji} removed."), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, add: bool):
        emoji_str = str(payload.emoji)
        async with self.bot.db.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (payload.guild_id, payload.message_id, emoji_str),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            return
        role = guild.get_role(row["role_id"])
        if not role:
            return

        try:
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.Forbidden:
            pass

    # ────────────────────────────────────────────────────────────────────────────
    # Button Roles
    # ────────────────────────────────────────────────────────────────────────────

    buttonrole_group = app_commands.Group(
        name="buttonrole", description="Manage button roles", guild_only=True
    )

    @buttonrole_group.command(name="create", description="Create a button role message")
    @app_commands.describe(channel="Channel to send the button role message in", title="Embed title")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def br_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "Role Selection",
    ):
        embed = discord.Embed(title=title, description="Click a button to get a role.", color=COL_BLUE)
        msg = await channel.send(embed=embed)
        await interaction.response.send_message(
            embed=success_embed(
                f"Button role message created in {channel.mention}.\n"
                f"Message ID: `{msg.id}`\n"
                f"Use `/buttonrole add {msg.id}` to add buttons."
            ),
            ephemeral=True,
        )

    @buttonrole_group.command(name="add", description="Add a button to an existing button role message")
    @app_commands.describe(
        message_id="Message ID of the button role message",
        channel="Channel the message is in",
        role="Role to assign",
        label="Button label",
        emoji="Button emoji (optional)",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def br_add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: discord.TextChannel,
        role: discord.Role,
        label: str,
        emoji: Optional[str] = None,
    ):
        try:
            msg = await channel.fetch_message(int(message_id))
        except (discord.NotFound, ValueError):
            return await interaction.response.send_message(
                embed=error_embed("Message not found."), ephemeral=True
            )

        await self.bot.db.execute(
            "INSERT INTO button_roles (guild_id, channel_id, message_id, label, emoji, role_id) VALUES (?,?,?,?,?,?)",
            (interaction.guild.id, channel.id, msg.id, label, emoji, role.id),
        )
        await self.bot.db.commit()

        # Rebuild the view
        await self._rebuild_button_view(msg)

        await interaction.response.send_message(
            embed=success_embed(f"Button `{label}` → {role.mention} added."), ephemeral=True
        )

    async def _rebuild_button_view(self, message: discord.Message):
        """Rebuild and re-apply button view to a button role message."""
        async with self.bot.db.execute(
            "SELECT * FROM button_roles WHERE message_id=?", (message.id,)
        ) as cur:
            buttons = await cur.fetchall()

        view = discord.ui.View(timeout=None)
        for b in buttons:
            role_id = b["role_id"]
            label   = b["label"]
            emoji   = b["emoji"]

            btn = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"br_{role_id}",
            )

            async def callback(interaction: discord.Interaction, rid=role_id):
                role = interaction.guild.get_role(rid)
                if not role:
                    return await interaction.response.send_message("Role not found.", ephemeral=True)
                if role in interaction.user.roles:
                    await interaction.user.remove_roles(role, reason="Button role")
                    await interaction.response.send_message(
                        f"Removed {role.mention}.", ephemeral=True
                    )
                else:
                    await interaction.user.add_roles(role, reason="Button role")
                    await interaction.response.send_message(
                        f"Given {role.mention}.", ephemeral=True
                    )

            btn.callback = callback
            view.add_item(btn)

        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

    # ────────────────────────────────────────────────────────────────────────────
    # Starboard
    # ────────────────────────────────────────────────────────────────────────────

    starboard_group = app_commands.Group(
        name="starboard", description="Configure starboard", guild_only=True
    )

    @starboard_group.command(name="setup", description="Set up the starboard")
    @app_commands.describe(
        channel="Channel to post starred messages in",
        threshold="Stars required to get featured",
        emoji="The star emoji (default ⭐)",
        self_star="Allow authors to star their own messages",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sb_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        threshold: Optional[int] = 3,
        emoji: Optional[str] = "⭐",
        self_star: Optional[bool] = False,
    ):
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO starboard_config
               (guild_id, channel_id, threshold, emoji, self_star)
               VALUES (?,?,?,?,?)""",
            (interaction.guild.id, channel.id, threshold, emoji, int(self_star)),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                f"Starboard set up in {channel.mention}.\n"
                f"Threshold: `{threshold} {emoji}` · Self-star: `{self_star}`"
            ),
            ephemeral=True,
        )

    @starboard_group.command(name="disable", description="Disable the starboard")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def sb_disable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM starboard_config WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("Starboard disabled."), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):  # noqa: F811
        await self._handle_reaction(payload, add=True)
        await self._handle_starboard(payload)

    async def _handle_starboard(self, payload: discord.RawReactionActionEvent):
        async with self.bot.db.execute(
            "SELECT * FROM starboard_config WHERE guild_id=?", (payload.guild_id,)
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg:
            return
        if str(payload.emoji) != cfg["emoji"]:
            return

        guild   = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel(payload.channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return

        if not cfg["self_star"] and message.author.id == payload.user_id:
            return

        # Count reactions
        reaction = discord.utils.get(message.reactions, emoji=cfg["emoji"])
        count = reaction.count if reaction else 0

        sb_channel = guild.get_channel(cfg["channel_id"])
        if not sb_channel:
            return

        async with self.bot.db.execute(
            "SELECT * FROM starboard_entries WHERE original_msg_id=?", (message.id,)
        ) as cur:
            entry = await cur.fetchone()

        embed = discord.Embed(
            description=message.content or "",
            color=0xf1c40f,
            timestamp=message.created_at,
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Source", value=f"[Jump]({message.jump_url})", inline=True)
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)
        embed.set_footer(text=f"{count} {cfg['emoji']} • #{channel.name}")

        if count >= cfg["threshold"]:
            if entry:
                # Update existing
                try:
                    star_msg = await sb_channel.fetch_message(entry["star_msg_id"])
                    await star_msg.edit(
                        content=f"{count} {cfg['emoji']} <#{channel.id}>", embed=embed
                    )
                except discord.NotFound:
                    pass
                await self.bot.db.execute(
                    "UPDATE starboard_entries SET star_count=? WHERE original_msg_id=?",
                    (count, message.id),
                )
            else:
                # Post new
                star_msg = await sb_channel.send(
                    content=f"{count} {cfg['emoji']} <#{channel.id}>", embed=embed
                )
                await self.bot.db.execute(
                    "INSERT INTO starboard_entries (guild_id, original_msg_id, star_msg_id, star_count) VALUES (?,?,?,?)",
                    (payload.guild_id, message.id, star_msg.id, count),
                )
            await self.bot.db.commit()

    # ────────────────────────────────────────────────────────────────────────────
    # Counters
    # ────────────────────────────────────────────────────────────────────────────

    counter_group = app_commands.Group(
        name="counter", description="Stat counter channels", guild_only=True
    )

    COUNTER_TYPES = ("members", "bots", "humans", "online", "channels", "roles", "boosts")

    @counter_group.command(name="add", description="Create a stat counter channel")
    @app_commands.describe(
        counter_type="What to count",
        channel="Existing voice channel to use as counter",
        format="Format string, use {count} as placeholder (e.g. 'Members: {count}')",
    )
    @app_commands.choices(
        counter_type=[app_commands.Choice(name=t, value=t) for t in COUNTER_TYPES],
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def counter_add(
        self,
        interaction: discord.Interaction,
        counter_type: str,
        channel: discord.VoiceChannel,
        format: Optional[str] = None,
    ):
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO counters (guild_id, channel_id, counter_type, format)
               VALUES (?,?,?,?)""",
            (interaction.guild.id, channel.id, counter_type, format),
        )
        await self.bot.db.commit()

        # Update immediately
        name = self._format_counter(interaction.guild, counter_type, format)
        try:
            await channel.edit(name=name)
        except discord.Forbidden:
            pass

        await interaction.response.send_message(
            embed=success_embed(f"Counter `{counter_type}` added to {channel.mention}."),
            ephemeral=True,
        )

    @counter_group.command(name="remove", description="Remove a counter channel")
    @app_commands.describe(channel="The counter channel to remove")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counter_remove(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await self.bot.db.execute(
            "DELETE FROM counters WHERE guild_id=? AND channel_id=?",
            (interaction.guild.id, channel.id),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Counter removed from {channel.mention}."), ephemeral=True
        )

    # ────────────────────────────────────────────────────────────────────────────
    # Bump reminder (Disboard /bump detection)
    # ────────────────────────────────────────────────────────────────────────────

    bump_group = app_commands.Group(name="bump", description="Bump reminder configuration", guild_only=True)

    DISBOARD_ID = 302050872383242240

    @bump_group.command(name="setup", description="Set up the bump reminder system")
    @app_commands.describe(
        channel="Channel for bump reminders",
        role="Role to ping when it's time to bump",
        message="Reminder message (default: generic bump reminder)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bump_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
        message: Optional[str] = None,
    ):
        await self.bot.db.execute(
            """INSERT OR REPLACE INTO bump_config
               (guild_id, channel_id, role_id, message, last_bump, reminder_sent)
               VALUES (?,?,?,?,NULL,0)""",
            (interaction.guild.id, channel.id, role.id if role else None, message),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                f"Bump reminder configured.\n"
                f"Channel: {channel.mention} · Role: {role.mention if role else 'None'}"
            ),
            ephemeral=True,
        )

    @bump_group.command(name="disable", description="Disable the bump reminder")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bump_disable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM bump_config WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("Bump reminder disabled."), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):  # noqa: F811
        """Detect Disboard bump confirmation messages."""
        if not message.guild:
            return
        if message.author.id != self.DISBOARD_ID:
            return
        if not message.embeds:
            return
        embed = message.embeds[0]
        if embed.description and "bump done" in embed.description.lower():
            await self.bot.db.execute(
                "UPDATE bump_config SET last_bump=?, reminder_sent=0 WHERE guild_id=?",
                (utcnow(), message.guild.id),
            )
            await self.bot.db.commit()


async def setup(bot):
    await bot.add_cog(Configuration(bot))
