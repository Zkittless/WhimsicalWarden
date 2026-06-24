"""
cogs/tickets.py — Full ticket system
Panels (buttons/dropdown), options, forms with fields,
ticket lifecycle (open/claim/close/reopen/delete), transcript export,
support/trainee roles, auto-close timer.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed, utcnow,
    safe_send, COL_BLUE,
)

log = logging.getLogger("modbot.tickets")


# ── Ticket action views ─────────────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    """Persistent buttons inside an open ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.close_ticket(interaction)

    @discord.ui.button(label="✋ Claim", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.claim_ticket(interaction)

    @discord.ui.button(label="📄 Transcript", style=discord.ButtonStyle.secondary, custom_id="ticket_transcript")
    async def transcript_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.generate_transcript(interaction)


class ClosedTicketView(discord.ui.View):
    """Buttons for a closed ticket."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔓 Reopen", style=discord.ButtonStyle.success, custom_id="ticket_reopen")
    async def reopen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.reopen_ticket(interaction)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="ticket_delete")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.delete_ticket(interaction)

    @discord.ui.button(label="📄 Transcript", style=discord.ButtonStyle.secondary, custom_id="ticket_transcript_closed")
    async def transcript_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            await cog.generate_transcript(interaction)


class PanelView(discord.ui.View):
    """Dynamic button panel for opening tickets."""

    def __init__(self, panel_id: int, options: list):
        super().__init__(timeout=None)
        for opt in options:
            btn = discord.ui.Button(
                label=opt["label"],
                emoji=opt.get("emoji"),
                style=discord.ButtonStyle.primary,
                custom_id=f"ticket_open_{opt['id']}",
            )
            self.add_item(btn)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open_default")
    async def open_default(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Tickets = interaction.client.cogs.get("Tickets")
        if cog:
            option_id = int(button.custom_id.split("_")[-1]) if button.custom_id != "ticket_open_default" else None
            await cog._open_ticket(interaction, option_id)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TicketControlView())
        bot.add_view(ClosedTicketView())
        self._check_auto_close.start()

    def cog_unload(self):
        self._check_auto_close.cancel()

    @tasks.loop(minutes=10)
    async def _check_auto_close(self):
        """Auto-close inactive tickets."""
        now = utcnow()
        async with self.bot.db.execute(
            """SELECT t.*, o.auto_close_hours FROM tickets t
               JOIN ticket_options o ON t.option_id = o.id
               WHERE t.status='open' AND o.auto_close_hours IS NOT NULL AND o.auto_close_hours > 0"""
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            cutoff = row["opened_at"] + (row["auto_close_hours"] * 3600)
            if now >= cutoff:
                guild = self.bot.get_guild(row["guild_id"])
                if not guild:
                    continue
                ch = guild.get_channel(row["channel_id"])
                if not ch:
                    continue
                await ch.send(
                    embed=info_embed(
                        "This ticket has been automatically closed due to inactivity.",
                        title="Auto-closed"
                    )
                )
                await self._do_close_ticket(ch, guild, row["channel_id"])

    @_check_auto_close.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ── Core ticket operations ───────────────────────────────────────────────────

    async def _open_ticket(
        self,
        interaction: discord.Interaction,
        option_id: Optional[int] = None,
    ):
        guild  = interaction.guild
        member = interaction.user
        db     = self.bot.db

        # Check for existing open ticket
        async with db.execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
            (guild.id, member.id),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                return await interaction.response.send_message(
                    embed=error_embed(f"You already have an open ticket: {ch.mention}"),
                    ephemeral=True,
                )

        # Fetch option config
        if option_id:
            async with db.execute("SELECT * FROM ticket_options WHERE id=?", (option_id,)) as cur:
                option = await cur.fetchone()
        else:
            option = None

        # Determine category
        category_id = option["category_id"] if option and option["category_id"] else None
        category    = guild.get_channel(category_id) if category_id else None

        # Check required role
        if option and option["required_role_id"]:
            req_role = guild.get_role(option["required_role_id"])
            if req_role and req_role not in member.roles:
                return await interaction.response.send_message(
                    embed=error_embed(f"You need {req_role.mention} to open this ticket."),
                    ephemeral=True,
                )

        await interaction.response.defer(ephemeral=True)

        # Build overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
            member:             discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
        }

        # Add support role
        if option:
            async with db.execute(
                "SELECT support_role_id FROM ticket_panels WHERE id=?", (option_id,)
            ) as cur:
                panel = await cur.fetchone()
            if panel and panel["support_role_id"]:
                role = guild.get_role(panel["support_role_id"])
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Create channel
        ch_name = f"ticket-{member.name.lower().replace(' ', '-')}"[:100]
        try:
            ticket_ch = await guild.create_text_channel(
                ch_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket by {member}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=error_embed("I don't have permission to create the ticket channel."),
                ephemeral=True,
            )

        # Save ticket
        panel_id = option_id  # simplified
        await db.execute(
            """INSERT INTO tickets (guild_id, channel_id, user_id, panel_id, option_id, status, opened_at)
               VALUES (?,?,?,?,?,?,?)""",
            (guild.id, ticket_ch.id, member.id, panel_id, option_id, "open", utcnow()),
        )
        await db.commit()

        # Send greeting
        greeting = option["greeting"] if option and option["greeting"] else "A staff member will be with you shortly."
        embed = discord.Embed(
            title="✨ Ticket Opened",
            description=f"{member.mention} — {greeting}",
            color=COL_BLUE,
            timestamp=datetime.now(timezone.utc),
        )
        view = TicketControlView()
        await ticket_ch.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=success_embed(f"Ticket created: {ticket_ch.mention}"),
            ephemeral=True,
        )

    async def close_ticket(self, interaction: discord.Interaction):
        """Close a ticket channel."""
        db    = self.bot.db
        ch    = interaction.channel
        guild = interaction.guild

        async with db.execute(
            "SELECT * FROM tickets WHERE channel_id=? AND status='open'", (ch.id,)
        ) as cur:
            ticket = await cur.fetchone()

        if not ticket:
            return await interaction.response.send_message(
                embed=error_embed("This isn't an open ticket."), ephemeral=True
            )

        await interaction.response.defer()
        await self._do_close_ticket(ch, guild, ch.id)

    async def _do_close_ticket(self, ch: discord.TextChannel, guild: discord.Guild, channel_id: int):
        db = self.bot.db

        # Update DB
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=? WHERE channel_id=?",
            (utcnow(), channel_id),
        )
        await db.commit()

        # Remove member permissions, add closed overlay
        async with db.execute(
            "SELECT user_id FROM tickets WHERE channel_id=?", (channel_id,)
        ) as cur:
            ticket = await cur.fetchone()

        if ticket:
            member = guild.get_member(ticket["user_id"])
            if member:
                try:
                    await ch.set_permissions(member, send_messages=False)
                except discord.Forbidden:
                    pass

        embed = discord.Embed(
            title="🌑 Ticket Closed",
            description="This ticket has been closed. Staff can reopen or delete it below.",
            color=0xe74c3c,
            timestamp=datetime.now(timezone.utc),
        )
        view = ClosedTicketView()
        try:
            await ch.send(embed=embed, view=view)
            await ch.edit(name=f"closed-{ch.name}")
        except discord.HTTPException:
            pass

    async def claim_ticket(self, interaction: discord.Interaction):
        """Claim a ticket as a staff member."""
        db  = self.bot.db
        ch  = interaction.channel
        mod = interaction.user

        async with db.execute(
            "SELECT * FROM tickets WHERE channel_id=? AND status='open'", (ch.id,)
        ) as cur:
            ticket = await cur.fetchone()

        if not ticket:
            return await interaction.response.send_message(
                embed=error_embed("This isn't an open ticket."), ephemeral=True
            )

        await db.execute(
            "UPDATE tickets SET claimed_by=? WHERE channel_id=?", (mod.id, ch.id)
        )
        await db.commit()

        await interaction.response.send_message(
            embed=success_embed(f"✋ {mod.mention} has claimed this ticket.")
        )

    async def reopen_ticket(self, interaction: discord.Interaction):
        """Reopen a closed ticket."""
        db    = self.bot.db
        ch    = interaction.channel
        guild = interaction.guild

        async with db.execute(
            "SELECT * FROM tickets WHERE channel_id=? AND status='closed'", (ch.id,)
        ) as cur:
            ticket = await cur.fetchone()

        if not ticket:
            return await interaction.response.send_message(
                embed=error_embed("This ticket isn't closed."), ephemeral=True
            )

        member = guild.get_member(ticket["user_id"])
        if member:
            try:
                await ch.set_permissions(member, read_messages=True, send_messages=True)
            except discord.Forbidden:
                pass

        await db.execute(
            "UPDATE tickets SET status='open', closed_at=NULL WHERE channel_id=?", (ch.id,)
        )
        await db.commit()

        new_name = ch.name.replace("closed-", "", 1)
        try:
            await ch.edit(name=new_name)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="✨ Ticket Reopened",
            color=0x2ecc71,
            timestamp=datetime.now(timezone.utc),
        )
        view = TicketControlView()
        await interaction.response.send_message(embed=embed, view=view)

    async def delete_ticket(self, interaction: discord.Interaction):
        """Delete a ticket channel."""
        db = self.bot.db
        ch = interaction.channel

        async with db.execute(
            "SELECT 1 FROM tickets WHERE channel_id=?", (ch.id,)
        ) as cur:
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    embed=error_embed("This isn't a ticket channel."), ephemeral=True
                )

        await interaction.response.send_message(
            embed=info_embed("Deleting ticket in 5 seconds...")
        )
        import asyncio
        await asyncio.sleep(5)

        await db.execute("UPDATE tickets SET status='deleted' WHERE channel_id=?", (ch.id,))
        await db.commit()

        try:
            await ch.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.HTTPException:
            pass

    async def generate_transcript(self, interaction: discord.Interaction):
        """Generate a text transcript of the ticket."""
        await interaction.response.defer(ephemeral=True)
        ch = interaction.channel

        lines = [f"# Transcript for #{ch.name}", f"Generated: {datetime.now(timezone.utc).isoformat()}", ""]
        async for msg in ch.history(limit=None, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{timestamp}] {msg.author} ({msg.author.id}): {msg.content or '[no text]'}")
            for a in msg.attachments:
                lines.append(f"  [Attachment: {a.url}]")
            for e in msg.embeds:
                if e.title:
                    lines.append(f"  [Embed: {e.title}]")

        content = "\n".join(lines)
        buf = io.BytesIO(content.encode("utf-8"))
        file = discord.File(buf, filename=f"transcript-{ch.name}.txt")
        await interaction.followup.send(file=file, ephemeral=True)

    # ────────────────────────────────────────────────────────────────────────────
    # /ticket commands
    # ────────────────────────────────────────────────────────────────────────────

    ticket_group = app_commands.Group(
        name="ticket",
        description="Ticket system management",
        guild_only=True,
    )

    @ticket_group.command(name="panel", description="Create a ticket panel in a channel")
    @app_commands.describe(
        name="Panel name (for internal reference)",
        channel="Channel to post the panel in",
        support_role="Role that can see all tickets",
        category="Category for ticket channels",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel,
        support_role: Optional[discord.Role] = None,
        category: Optional[discord.CategoryChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild.id

        # Check max 15 panels
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM ticket_panels WHERE guild_id=?", (gid,)
        ) as cur:
            count = (await cur.fetchone())[0]
        if count >= 15:
            return await interaction.followup.send(
                embed=error_embed("Maximum 15 ticket panels per server."), ephemeral=True
            )

        # Create the panel embed and button
        embed = discord.Embed(
            title=f"🎫 {name}",
            description="Click the button below to open a ticket.",
            color=COL_BLUE,
        )

        view = discord.ui.View(timeout=None)
        open_btn = discord.ui.Button(
            label="Open Ticket",
            style=discord.ButtonStyle.primary,
            emoji="✨",
            custom_id=f"ticket_panel_{name.replace(' ', '_').lower()}",
        )

        async def panel_callback(inter: discord.Interaction):
            cog: Tickets = inter.client.cogs.get("Tickets")
            if cog:
                async with self.bot.db.execute(
                    "SELECT id FROM ticket_options WHERE panel_id=(SELECT id FROM ticket_panels WHERE guild_id=? AND name=? LIMIT 1) LIMIT 1",
                    (inter.guild.id, name),
                ) as cur:
                    opt = await cur.fetchone()
                await cog._open_ticket(inter, opt["id"] if opt else None)

        open_btn.callback = panel_callback
        view.add_item(open_btn)

        msg = await channel.send(embed=embed, view=view)

        await self.bot.db.execute(
            """INSERT OR REPLACE INTO ticket_panels
               (guild_id, name, channel_id, message_id, support_role_id, category_id)
               VALUES (?,?,?,?,?,?)""",
            (gid, name, channel.id, msg.id,
             support_role.id if support_role else None,
             category.id if category else None),
        )

        # Create default option
        panel_id_row = await self.bot.db.execute(
            "SELECT id FROM ticket_panels WHERE guild_id=? AND name=?", (gid, name)
        )
        panel_row = await panel_id_row.fetchone()
        if panel_row:
            await self.bot.db.execute(
                "INSERT INTO ticket_options (panel_id, label, greeting) VALUES (?,?,?)",
                (panel_row["id"], "Open Ticket", "Welcome! A staff member will be with you shortly."),
            )

        await self.bot.db.commit()
        await interaction.followup.send(
            embed=success_embed(
                f"Ticket panel **{name}** created in {channel.mention}.\n"
                f"Support role: {support_role.mention if support_role else 'None'}\n"
                f"Category: {category.mention if category else 'None'}"
            ),
            ephemeral=True,
        )

    @ticket_group.command(name="delete_panel", description="Delete a ticket panel")
    @app_commands.describe(name="Panel name to delete")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_delete_panel(self, interaction: discord.Interaction, name: str):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT * FROM ticket_panels WHERE guild_id=? AND name=?", (gid, name)
        ) as cur:
            panel = await cur.fetchone()

        if not panel:
            return await interaction.response.send_message(
                embed=error_embed(f"Panel `{name}` not found."), ephemeral=True
            )

        # Delete the panel message
        ch = interaction.guild.get_channel(panel["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(panel["message_id"])
                await msg.delete()
            except discord.HTTPException:
                pass

        # Delete options first (FK constraint), then the panel
        await self.bot.db.execute(
            "DELETE FROM ticket_options WHERE panel_id=?", (panel["id"],)
        )
        await self.bot.db.execute(
            "DELETE FROM ticket_panels WHERE guild_id=? AND name=?", (gid, name)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Panel `{name}` deleted."), ephemeral=True
        )

    @ticket_group.command(name="close", description="Close the current ticket")
    async def ticket_close(self, interaction: discord.Interaction):
        await self.close_ticket(interaction)

    @ticket_group.command(name="reopen", description="Reopen a closed ticket")
    async def ticket_reopen(self, interaction: discord.Interaction):
        await self.reopen_ticket(interaction)

    @ticket_group.command(name="delete", description="Delete the current ticket channel")
    async def ticket_delete(self, interaction: discord.Interaction):
        await self.delete_ticket(interaction)

    @ticket_group.command(name="transcript", description="Generate a transcript of this ticket")
    async def ticket_transcript(self, interaction: discord.Interaction):
        await self.generate_transcript(interaction)

    @ticket_group.command(name="claim", description="Claim this ticket")
    async def ticket_claim(self, interaction: discord.Interaction):
        await self.claim_ticket(interaction)

    @ticket_group.command(name="add", description="Add a member to this ticket")
    @app_commands.describe(member="Member to add")
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute(
            "SELECT 1 FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        ) as cur:
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    embed=error_embed("Not a ticket channel."), ephemeral=True
                )
        await interaction.channel.set_permissions(
            member, read_messages=True, send_messages=True
        )
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} added to ticket.")
        )

    @ticket_group.command(name="remove", description="Remove a member from this ticket")
    @app_commands.describe(member="Member to remove")
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute(
            "SELECT user_id FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        ) as cur:
            ticket = await cur.fetchone()
        if not ticket:
            return await interaction.response.send_message(
                embed=error_embed("Not a ticket channel."), ephemeral=True
            )
        if ticket["user_id"] == member.id:
            return await interaction.response.send_message(
                embed=error_embed("Cannot remove the ticket owner."), ephemeral=True
            )
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(
            embed=success_embed(f"{member.mention} removed from ticket.")
        )

    @ticket_group.command(name="list", description="View open tickets")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM tickets WHERE guild_id=? AND status='open' ORDER BY opened_at DESC",
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No open tickets."), ephemeral=True
            )

        lines = []
        for r in rows:
            ch = interaction.guild.get_channel(r["channel_id"])
            ch_str = ch.mention if ch else f"`#{r['channel_id']}`"
            claimed = f" (claimed by <@{r['claimed_by']}>)" if r["claimed_by"] else ""
            lines.append(f"{ch_str} — <@{r['user_id']}>{claimed}")

        embed = discord.Embed(
            title=f"Open Tickets ({len(rows)})",
            description="\n".join(lines[:25]),
            color=COL_BLUE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
