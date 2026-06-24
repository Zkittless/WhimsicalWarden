"""
cogs/automod.py — Full AutoMod engine
Spam, caps, mass mentions, emoji floods, duplicate messages,
link/invite filtering, phishing detection, regex word filter,
per-rule punishment escalation, ignore lists.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from typing import Optional, Union
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from utils import (
    success_embed, error_embed, info_embed, warn_embed,
    parse_duration, seconds_to_human, utcnow, dispatch_mod_log,
    safe_send, try_dm, create_case, build_pages, Paginator,
    COL_RED, COL_YELLOW,
)

log = logging.getLogger("modbot.automod")

# Regex patterns
URL_REGEX     = re.compile(r"https?://[^\s]+|www\.[^\s]+|\b[\w-]+\.[a-z]{2,}\b", re.IGNORECASE)
INVITE_REGEX  = re.compile(r"discord(?:\.gg|app\.com/invite|\.com/invite)/[\w-]+", re.IGNORECASE)


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # In-memory spam tracking: {guild_id: {user_id: [timestamps]}}
        self._msg_timestamps: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        # Duplicate message tracking: {guild_id: {user_id: [content]}}
        self._recent_messages: dict[int, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))

    # ── Helpers ─────────────────────────────────────────────────────────────────

    async def get_config(self, guild_id: int) -> dict:
        async with self.bot.db.execute(
            "SELECT * FROM automod_config WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def is_ignored(self, guild_id: int, member: discord.Member) -> bool:
        """Check if this member/role/channel should be ignored by automod."""
        if member.guild_permissions.administrator:
            return True
        if member.guild_permissions.manage_messages:
            return True

        ids_to_check = [member.id] + [r.id for r in member.roles]
        placeholders = ",".join("?" * len(ids_to_check))
        async with self.bot.db.execute(
            f"SELECT 1 FROM automod_ignore WHERE guild_id=? AND target_id IN ({placeholders}) LIMIT 1",
            (guild_id, *ids_to_check),
        ) as cur:
            return await cur.fetchone() is not None

    async def is_channel_ignored(self, guild_id: int, channel_id: int) -> bool:
        async with self.bot.db.execute(
            "SELECT 1 FROM automod_ignore WHERE guild_id=? AND target_id=? AND target_type='channel' LIMIT 1",
            (guild_id, channel_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def apply_action(
        self,
        message: discord.Message,
        action: str,
        reason: str,
        duration: int = None,
    ):
        """Apply an automod action to a message author."""
        member = message.author
        guild  = message.guild

        if action == "delete":
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

        # Always delete the offending message for non-delete actions
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        if action == "warn":
            case_num = await create_case(
                self.bot.db, guild.id, member.id, self.bot.user.id, "warn", f"[AutoMod] {reason}"
            )
            await safe_send(
                message.channel,
                embed=discord.Embed(
                    description=f"⚠️ {member.mention} — **{reason}**",
                    color=COL_YELLOW,
                ),
            )
        elif action == "mute":
            async with self.bot.db.execute(
                "SELECT mute_role FROM guild_config WHERE guild_id=?", (guild.id,)
            ) as cur:
                cfg = await cur.fetchone()
            if cfg and cfg["mute_role"]:
                role = guild.get_role(cfg["mute_role"])
                if role:
                    try:
                        await member.add_roles(role, reason=f"[AutoMod] {reason}")
                    except discord.Forbidden:
                        pass
        elif action == "kick":
            try:
                await guild.kick(member, reason=f"[AutoMod] {reason}")
            except discord.Forbidden:
                pass
        elif action == "ban":
            try:
                await guild.ban(member, reason=f"[AutoMod] {reason}", delete_message_days=0)
            except discord.Forbidden:
                pass

        # Log to mod log
        embed = discord.Embed(
            title=f"✨ AutoMod — {reason}",
            description=(
                f"**User:** {member.mention} `{member}` ({member.id})\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Action:** `{action}`\n"
                f"**Content:** {message.content[:200] or '[no text]'}"
            ),
            color=COL_RED,
        )
        await dispatch_mod_log(self.bot, guild, embed)

    # ────────────────────────────────────────────────────────────────────────────
    # Main message listener
    # ────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        guild_id   = message.guild.id
        member     = message.author
        content    = message.content

        if await self.is_ignored(guild_id, member):
            return
        if await self.is_channel_ignored(guild_id, message.channel.id):
            return

        cfg = await self.get_config(guild_id)
        if not cfg:
            return

        # ── Phishing check (highest priority, always on by default) ───────────
        if cfg.get("phishing_on", 1):
            if await self._check_phishing(message, cfg):
                return

        # ── Word filter ───────────────────────────────────────────────────────
        if await self._check_word_filter(message):
            return

        # ── Invite filter ─────────────────────────────────────────────────────
        if cfg.get("invites_on") and INVITE_REGEX.search(content):
            await self.apply_action(message, cfg.get("invites_action", "delete"), "Discord invite link")
            return

        # ── Link filter ───────────────────────────────────────────────────────
        if cfg.get("links_on") and URL_REGEX.search(content):
            blocked = await self._is_link_blocked(guild_id, content)
            if blocked:
                await self.apply_action(message, cfg.get("links_action", "delete"), "Blocked link")
                return

        # ── Mass mention ──────────────────────────────────────────────────────
        if cfg.get("mention_on"):
            mention_count = len(message.mentions) + len(message.role_mentions)
            if mention_count >= cfg.get("mention_threshold", 5):
                await self.apply_action(message, cfg.get("mention_action", "mute"),
                    f"Mass mention ({mention_count} mentions)")
                return

        # ── Emoji flood ───────────────────────────────────────────────────────
        if cfg.get("emoji_on"):
            emoji_count = self._count_emojis(content)
            if emoji_count >= cfg.get("emoji_threshold", 10):
                await self.apply_action(message, cfg.get("emoji_action", "warn"),
                    f"Emoji flood ({emoji_count} emojis)")
                return

        # ── Caps check ────────────────────────────────────────────────────────
        if cfg.get("caps_on") and len(content) >= cfg.get("caps_min_length", 10):
            caps_ratio = sum(1 for c in content if c.isupper()) / max(1, sum(1 for c in content if c.isalpha()))
            if caps_ratio >= cfg.get("caps_percent", 70) / 100:
                await self.apply_action(message, cfg.get("caps_action", "warn"),
                    f"Excessive caps ({int(caps_ratio * 100)}%)")
                return

        # ── Duplicate messages ────────────────────────────────────────────────
        if cfg.get("duplicate_on") and content:
            recent = self._recent_messages[guild_id][member.id]
            recent.append(content.lower().strip())
            # Keep only last N+1 messages
            max_dup = cfg.get("duplicate_count", 3)
            if len(recent) > max_dup + 1:
                recent.pop(0)
            if recent.count(content.lower().strip()) >= max_dup:
                self._recent_messages[guild_id][member.id].clear()
                await self.apply_action(message, cfg.get("duplicate_action", "warn"),
                    "Duplicate messages")
                return

        # ── Spam (rate limit) ─────────────────────────────────────────────────
        if cfg.get("spam_on"):
            now        = time.time()
            timestamps = self._msg_timestamps[guild_id][member.id]
            timestamps.append(now)
            window     = cfg.get("spam_interval", 5)
            threshold  = cfg.get("spam_threshold", 5)
            timestamps[:] = [t for t in timestamps if now - t < window]
            self._msg_timestamps[guild_id][member.id] = timestamps

            if len(timestamps) >= threshold:
                self._msg_timestamps[guild_id][member.id].clear()
                await self.apply_action(message, cfg.get("spam_action", "mute"),
                    f"Spam ({len(timestamps)} msgs in {window}s)")
                return

    async def _check_phishing(self, message: discord.Message, cfg: dict) -> bool:
        """Check message URLs against phishing domains. Returns True if caught."""
        from cogs.security import fetch_phishing_domains
        domains = await fetch_phishing_domains()
        if not domains:
            return False

        urls = URL_REGEX.findall(message.content)
        for url in urls:
            try:
                parsed = urlparse(url if "://" in url else f"http://{url}")
                domain = parsed.netloc.lower().lstrip("www.")
                if any(domain == d or domain.endswith("." + d) for d in domains):
                    await self.apply_action(
                        message,
                        cfg.get("phishing_action", "ban"),
                        f"Phishing link detected (`{domain}`)",
                    )
                    return True
            except Exception:
                pass
        return False

    async def _check_word_filter(self, message: discord.Message) -> bool:
        """Check message content against the word filter. Returns True if caught."""
        async with self.bot.db.execute(
            "SELECT * FROM automod_word_filter WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            filters = await cur.fetchall()

        content_lower = message.content.lower()
        for f in filters:
            try:
                if f["is_regex"]:
                    if re.search(f["pattern"], message.content, re.IGNORECASE):
                        await self.apply_action(message, f["action"], f"Filtered word/pattern")
                        return True
                else:
                    if f["pattern"].lower() in content_lower:
                        await self.apply_action(message, f["action"], f"Filtered word")
                        return True
            except re.error:
                pass
        return False

    async def _is_link_blocked(self, guild_id: int, content: str) -> bool:
        """Check if a link matches the blacklist (or is not on the whitelist if links_on)."""
        urls = URL_REGEX.findall(content)
        for url in urls:
            try:
                parsed = urlparse(url if "://" in url else f"http://{url}")
                domain = parsed.netloc.lower().lstrip("www.")

                # Check whitelist — if domain is whitelisted, allow
                async with self.bot.db.execute(
                    "SELECT 1 FROM automod_link_whitelist WHERE guild_id=? AND domain=? LIMIT 1",
                    (guild_id, domain),
                ) as cur:
                    if await cur.fetchone():
                        continue

                # Check blacklist
                async with self.bot.db.execute(
                    "SELECT 1 FROM automod_link_blacklist WHERE guild_id=? AND domain=? LIMIT 1",
                    (guild_id, domain),
                ) as cur:
                    if await cur.fetchone():
                        return True
            except Exception:
                pass
        return False

    def _count_emojis(self, content: str) -> int:
        """Count Unicode and custom Discord emojis in content."""
        custom_emojis = re.findall(r"<a?:[^:]+:\d+>", content)
        # Simple unicode emoji approximation
        unicode_emojis = re.findall(
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
            r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251]+",
            content,
        )
        return len(custom_emojis) + len(unicode_emojis)

    # ────────────────────────────────────────────────────────────────────────────
    # /automod commands
    # ────────────────────────────────────────────────────────────────────────────

    automod_group = app_commands.Group(
        name="automod",
        description="Configure the AutoMod system",
        guild_only=True,
    )

    async def _ensure_config(self, guild_id: int):
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", (guild_id,)
        )
        await self.bot.db.commit()

    @automod_group.command(name="spam", description="Configure spam detection")
    @app_commands.describe(
        enabled="Enable or disable",
        threshold="Messages per interval before action",
        interval="Time window in seconds",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("warn", "mute", "kick", "ban")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_spam(
        self,
        interaction: discord.Interaction,
        enabled: int,
        threshold: Optional[int] = 5,
        interval: Optional[int] = 5,
        action: Optional[str] = "mute",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            """UPDATE automod_config SET spam_on=?, spam_threshold=?, spam_interval=?, spam_action=?
               WHERE guild_id=?""",
            (enabled, threshold, interval, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                f"Spam detection **{status}**.\n"
                f"Threshold: `{threshold} msgs/{interval}s` · Action: `{action}`"
            ),
            ephemeral=True,
        )

    @automod_group.command(name="caps", description="Configure caps/uppercase detection")
    @app_commands.describe(
        enabled="Enable or disable",
        percent="Minimum caps percentage to trigger (0-100)",
        min_length="Minimum message length to check",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("warn", "delete", "mute")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_caps(
        self,
        interaction: discord.Interaction,
        enabled: int,
        percent: Optional[int] = 70,
        min_length: Optional[int] = 10,
        action: Optional[str] = "warn",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            """UPDATE automod_config SET caps_on=?, caps_percent=?, caps_min_length=?, caps_action=?
               WHERE guild_id=?""",
            (enabled, percent, min_length, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                f"Caps filter **{status}**.\n"
                f"Threshold: `{percent}%` (min {min_length} chars) · Action: `{action}`"
            ),
            ephemeral=True,
        )

    @automod_group.command(name="mentions", description="Configure mass mention detection")
    @app_commands.describe(
        enabled="Enable or disable",
        threshold="Max mentions before action",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("warn", "delete", "mute", "kick", "ban")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_mentions(
        self,
        interaction: discord.Interaction,
        enabled: int,
        threshold: Optional[int] = 5,
        action: Optional[str] = "mute",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET mention_on=?, mention_threshold=?, mention_action=? WHERE guild_id=?",
            (enabled, threshold, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Mass mention detection **{status}**. Threshold: `{threshold}` · Action: `{action}`"),
            ephemeral=True,
        )

    @automod_group.command(name="emoji", description="Configure emoji flood detection")
    @app_commands.describe(
        enabled="Enable or disable",
        threshold="Max emojis per message",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("warn", "delete", "mute")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_emoji(
        self,
        interaction: discord.Interaction,
        enabled: int,
        threshold: Optional[int] = 10,
        action: Optional[str] = "warn",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET emoji_on=?, emoji_threshold=?, emoji_action=? WHERE guild_id=?",
            (enabled, threshold, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Emoji flood detection **{status}**. Threshold: `{threshold}` · Action: `{action}`"),
            ephemeral=True,
        )

    @automod_group.command(name="links", description="Configure link filtering")
    @app_commands.describe(
        enabled="Enable or disable",
        action="What to do with blocked links",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("delete", "warn", "mute")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_links(
        self,
        interaction: discord.Interaction,
        enabled: int,
        action: Optional[str] = "delete",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET links_on=?, links_action=? WHERE guild_id=?",
            (enabled, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Link filtering **{status}**. Action: `{action}`"),
            ephemeral=True,
        )

    @automod_group.command(name="invites", description="Block Discord invite links")
    @app_commands.describe(enabled="Enable or disable", action="Punishment")
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("delete", "warn", "mute", "kick")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_invites(
        self,
        interaction: discord.Interaction,
        enabled: int,
        action: Optional[str] = "delete",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET invites_on=?, invites_action=? WHERE guild_id=?",
            (enabled, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Invite filter **{status}**. Action: `{action}`"),
            ephemeral=True,
        )

    @automod_group.command(name="phishing", description="Configure phishing link detection")
    @app_commands.describe(enabled="Enable or disable", action="Punishment for phishing links")
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("ban", "kick", "mute", "warn")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_phishing(
        self,
        interaction: discord.Interaction,
        enabled: int,
        action: Optional[str] = "ban",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET phishing_on=?, phishing_action=? WHERE guild_id=?",
            (enabled, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Phishing detection **{status}**. Action: `{action}`"),
            ephemeral=True,
        )

    @automod_group.command(name="duplicate", description="Detect repeated/duplicate messages")
    @app_commands.describe(
        enabled="Enable or disable",
        count="How many duplicates before action",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("warn", "delete", "mute")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_duplicate(
        self,
        interaction: discord.Interaction,
        enabled: int,
        count: Optional[int] = 3,
        action: Optional[str] = "warn",
    ):
        gid = interaction.guild.id
        await self._ensure_config(gid)
        await self.bot.db.execute(
            "UPDATE automod_config SET duplicate_on=?, duplicate_count=?, duplicate_action=? WHERE guild_id=?",
            (enabled, count, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Duplicate detection **{status}**. Count: `{count}` · Action: `{action}`"),
            ephemeral=True,
        )

    # ── Word filter commands ─────────────────────────────────────────────────────

    filter_group = app_commands.Group(
        name="filter",
        description="Manage the word/pattern filter",
        guild_only=True,
    )

    @filter_group.command(name="add", description="Add a word or regex pattern to the filter")
    @app_commands.describe(
        pattern="Word or pattern to block",
        is_regex="Treat as a regex pattern",
        action="What to do when triggered",
    )
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in ("delete", "warn", "mute", "kick", "ban")],
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def filter_add(
        self,
        interaction: discord.Interaction,
        pattern: str,
        is_regex: Optional[bool] = False,
        action: Optional[str] = "delete",
    ):
        if is_regex:
            try:
                re.compile(pattern)
            except re.error as e:
                return await interaction.response.send_message(
                    embed=error_embed(f"Invalid regex: `{e}`"), ephemeral=True
                )

        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO automod_word_filter (guild_id, pattern, is_regex, action) VALUES (?,?,?,?)",
                (interaction.guild.id, pattern, int(is_regex), action),
            )
            await self.bot.db.commit()
        except Exception:
            return await interaction.response.send_message(
                embed=error_embed("Pattern already exists."), ephemeral=True
            )

        await interaction.response.send_message(
            embed=success_embed(
                f"Pattern `{pattern}` added ({'regex' if is_regex else 'exact'}) · Action: `{action}`"
            ),
            ephemeral=True,
        )

    @filter_group.command(name="remove", description="Remove a word or pattern from the filter")
    @app_commands.describe(pattern="Pattern to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def filter_remove(self, interaction: discord.Interaction, pattern: str):
        await self.bot.db.execute(
            "DELETE FROM automod_word_filter WHERE guild_id=? AND pattern=?",
            (interaction.guild.id, pattern),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Pattern `{pattern}` removed."), ephemeral=True
        )

    @filter_group.command(name="list", description="View all filtered words/patterns")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def filter_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT * FROM automod_word_filter WHERE guild_id=?", (interaction.guild.id,)
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No filters set. Use `/filter add` to add one."), ephemeral=True
            )

        items = [
            f"`{r['pattern']}` {'(regex)' if r['is_regex'] else ''} → `{r['action']}`"
            for r in rows
        ]
        pages = build_pages(items, title="Word Filter", per_page=15)
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
        else:
            view = Paginator(pages, interaction.user)
            await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    @filter_group.command(name="clear", description="Clear all word filters")
    @app_commands.checks.has_permissions(administrator=True)
    async def filter_clear(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM automod_word_filter WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("All word filters cleared."), ephemeral=True
        )

    # ── Link whitelist/blacklist ──────────────────────────────────────────────

    @automod_group.command(name="whitelist", description="Add/remove a domain from the link whitelist")
    @app_commands.describe(domain="Domain to whitelist (e.g. youtube.com)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def link_whitelist(self, interaction: discord.Interaction, domain: str):
        domain = domain.lower().lstrip("www.")
        async with self.bot.db.execute(
            "SELECT 1 FROM automod_link_whitelist WHERE guild_id=? AND domain=?",
            (interaction.guild.id, domain),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM automod_link_whitelist WHERE guild_id=? AND domain=?",
                (interaction.guild.id, domain),
            )
            msg = f"`{domain}` removed from link whitelist."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO automod_link_whitelist (guild_id, domain) VALUES (?,?)",
                (interaction.guild.id, domain),
            )
            msg = f"`{domain}` added to link whitelist."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @automod_group.command(name="blacklist", description="Add/remove a domain from the link blacklist")
    @app_commands.describe(domain="Domain to blacklist (e.g. badsite.com)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def link_blacklist(self, interaction: discord.Interaction, domain: str):
        domain = domain.lower().lstrip("www.")
        async with self.bot.db.execute(
            "SELECT 1 FROM automod_link_blacklist WHERE guild_id=? AND domain=?",
            (interaction.guild.id, domain),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM automod_link_blacklist WHERE guild_id=? AND domain=?",
                (interaction.guild.id, domain),
            )
            msg = f"`{domain}` removed from link blacklist."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO automod_link_blacklist (guild_id, domain) VALUES (?,?)",
                (interaction.guild.id, domain),
            )
            msg = f"`{domain}` added to link blacklist."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    # ── AutoMod ignore list ────────────────────────────────────────────────────

    @automod_group.command(name="ignore", description="Ignore a channel, role, or user from AutoMod")
    @app_commands.describe(target="Channel, role, or member to ignore/unignore")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_ignore(
        self,
        interaction: discord.Interaction,
        target: Union[discord.TextChannel, discord.Role, discord.Member],
    ):
        gid = interaction.guild.id
        if isinstance(target, discord.TextChannel):
            target_type = "channel"
        elif isinstance(target, discord.Role):
            target_type = "role"
        else:
            target_type = "user"

        async with self.bot.db.execute(
            "SELECT 1 FROM automod_ignore WHERE guild_id=? AND target_id=? AND target_type=?",
            (gid, target.id, target_type),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM automod_ignore WHERE guild_id=? AND target_id=? AND target_type=?",
                (gid, target.id, target_type),
            )
            msg = f"{target.mention} **removed** from AutoMod ignore list."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO automod_ignore (guild_id, target_id, target_type) VALUES (?,?,?)",
                (gid, target.id, target_type),
            )
            msg = f"{target.mention} **added** to AutoMod ignore list."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @automod_group.command(name="status", description="View AutoMod configuration summary")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def automod_status(self, interaction: discord.Interaction):
        cfg = await self.get_config(interaction.guild.id)
        if not cfg:
            return await interaction.response.send_message(
                embed=info_embed("AutoMod not configured yet. Use `/automod spam`, `/automod caps`, etc."),
                ephemeral=True,
            )

        def module_str(on_key, label, extra=""):
            on = cfg.get(on_key, 0)
            icon = "✅" if on else "❌"
            return f"{icon} **{label}**{': ' + extra if extra and on else ''}"

        embed = discord.Embed(title="🤖 AutoMod Status", color=0x5865F2)
        embed.add_field(name="Modules", value="\n".join([
            module_str("spam_on",      "Spam",       f"`{cfg.get('spam_threshold',5)} msgs/{cfg.get('spam_interval',5)}s`"),
            module_str("caps_on",      "Caps",       f"`{cfg.get('caps_percent',70)}%`"),
            module_str("mention_on",   "Mentions",   f"`{cfg.get('mention_threshold',5)} max`"),
            module_str("emoji_on",     "Emoji",      f"`{cfg.get('emoji_threshold',10)} max`"),
            module_str("duplicate_on", "Duplicates", f"`{cfg.get('duplicate_count',3)} repeats`"),
            module_str("links_on",     "Links",      f"`{cfg.get('links_action','delete')}`"),
            module_str("invites_on",   "Invites",    f"`{cfg.get('invites_action','delete')}`"),
            module_str("phishing_on",  "Phishing",   f"`{cfg.get('phishing_action','ban')}`"),
        ]), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
