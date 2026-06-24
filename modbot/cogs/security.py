"""
cogs/security.py — Antinuke, Antiraid, Fake Permissions
The most critical cog. Runs event listeners in near-realtime.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import (
    success_embed, error_embed, info_embed, warn_embed, log_embed,
    parse_duration, seconds_to_human, utcnow, discord_timestamp,
    dispatch_mod_log, safe_send, try_dm,
    is_antinuke_admin, guild_only, PERMISSION_MAP,
)

log = logging.getLogger("modbot.security")

# Antinuke modules tracked
AN_MODULES = ("ban", "kick", "role", "channel", "emoji", "webhook", "botadd", "vanity")

# Punishment actions
PUNISHMENTS = ("ban", "kick", "timeout", "stripstaff")

# Known phishing domains (seed list — augmented by API lookups)
PHISHING_DOMAINS_CACHE: set[str] = set()
PHISHING_CACHE_LAST_UPDATE = 0
PHISHING_CACHE_TTL = 3600  # refresh every hour


# ── Helpers ─────────────────────────────────────────────────────────────────────
async def fetch_phishing_domains() -> set[str]:
    """Fetch known phishing domains from public threat intel feeds."""
    global PHISHING_DOMAINS_CACHE, PHISHING_CACHE_LAST_UPDATE
    now = time.time()
    if now - PHISHING_CACHE_LAST_UPDATE < PHISHING_CACHE_TTL and PHISHING_DOMAINS_CACHE:
        return PHISHING_DOMAINS_CACHE

    domains: set[str] = set()
    urls = [
        "https://raw.githubusercontent.com/nikolaischunk/discord-phishing-links/main/domain-list.json",
        "https://raw.githubusercontent.com/DevSpen/scam-links/master/src/links.txt",
    ]
    try:
        async with aiohttp.ClientSession() as session:
            for url in urls:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            text = await r.text()
                            if url.endswith(".json"):
                                import json
                                data = json.loads(text)
                                if isinstance(data, list):
                                    domains.update(d.lower().strip() for d in data)
                                elif isinstance(data, dict):
                                    domains.update(d.lower().strip() for d in data.get("domains", []))
                            else:
                                for line in text.splitlines():
                                    line = line.strip().lower()
                                    if line and not line.startswith("#"):
                                        domains.add(line)
                except Exception:
                    pass
    except Exception:
        pass

    if domains:
        PHISHING_DOMAINS_CACHE = domains
        PHISHING_CACHE_LAST_UPDATE = now
    return PHISHING_DOMAINS_CACHE


async def apply_punishment(
    guild: discord.Guild,
    target: discord.Member,
    punishment: str,
    reason: str,
    bot,
) -> bool:
    """Apply antinuke punishment to a member. Returns True on success."""
    try:
        if punishment == "ban":
            await guild.ban(target, reason=reason, delete_message_days=0)
        elif punishment == "kick":
            await guild.kick(target, reason=reason)
        elif punishment == "timeout":
            until = datetime.now(timezone.utc) + timedelta(hours=24)
            await target.timeout(until, reason=reason)
        elif punishment == "stripstaff":
            dangerous_perms = [
                "administrator", "ban_members", "kick_members",
                "manage_guild", "manage_channels", "manage_roles",
                "manage_webhooks", "manage_expressions",
            ]
            for role in target.roles:
                if role.is_default():
                    continue
                perms = role.permissions
                has_danger = any(getattr(perms, p, False) for p in dangerous_perms)
                if has_danger:
                    try:
                        await target.remove_roles(role, reason=reason)
                    except discord.HTTPException:
                        pass
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning(f"Failed to apply punishment {punishment} to {target}: {e}")
        return False


class Security(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # In-memory action tracking: {guild_id: {user_id: {module: [timestamps]}}}
        self._action_cache: dict[int, dict[int, dict[str, list[float]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        self._cache_cleanup.start()

    def cog_unload(self):
        self._cache_cleanup.cancel()

    @tasks.loop(minutes=5)
    async def _cache_cleanup(self):
        """Prune old action timestamps from memory."""
        cutoff = time.time() - 60
        for guild_data in self._action_cache.values():
            for user_data in guild_data.values():
                for module in list(user_data.keys()):
                    user_data[module] = [t for t in user_data[module] if t > cutoff]

    # ── Action tracker (core antinuke logic) ────────────────────────────────────
    async def track_action(
        self,
        guild: discord.Guild,
        user_id: int,
        module: str,
    ) -> bool:
        """
        Record an action for (guild, user, module).
        Returns True if the threshold is exceeded → should punish.
        """
        db = self.bot.db

        # Check antinuke is enabled
        async with db.execute(
            "SELECT enabled FROM antinuke_config WHERE guild_id=?", (guild.id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["enabled"]:
            return False

        # Check module is enabled
        async with db.execute(
            "SELECT enabled, threshold FROM antinuke_modules WHERE guild_id=? AND module=?",
            (guild.id, module),
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg or not cfg["enabled"]:
            return False

        threshold = cfg["threshold"]

        # Check whitelist
        async with db.execute(
            "SELECT 1 FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
            (guild.id, user_id),
        ) as cur:
            if await cur.fetchone():
                return False

        # Skip server owner
        if user_id == guild.owner_id:
            return False

        # Track in memory
        now = time.time()
        window = 10.0  # 10-second rolling window
        timestamps = self._action_cache[guild.id][user_id][module]
        timestamps.append(now)
        # Prune old
        timestamps[:] = [t for t in timestamps if now - t < window]
        self._action_cache[guild.id][user_id][module] = timestamps

        return len(timestamps) >= threshold

    async def execute_antinuke(
        self,
        guild: discord.Guild,
        actor_id: int,
        module: str,
        description: str,
    ):
        """Fetch config and punish the actor."""
        db = self.bot.db
        async with db.execute(
            "SELECT punishment FROM antinuke_modules WHERE guild_id=? AND module=?",
            (guild.id, module),
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg:
            return

        punishment = cfg["punishment"]
        member = guild.get_member(actor_id)
        if not member:
            return

        reason = f"[Antinuke] {description} — threshold exceeded"
        success = await apply_punishment(guild, member, punishment, reason, self.bot)

        # Reset their action count
        if actor_id in self._action_cache.get(guild.id, {}):
            self._action_cache[guild.id][actor_id][module].clear()

        # Notify owner
        owner = guild.owner
        if owner and success:
            embed = discord.Embed(
                title="🚨 Antinuke Triggered",
                description=(
                    f"**Module:** `{module}`\n"
                    f"**Actor:** {member.mention} `{member}` ({member.id})\n"
                    f"**Action:** {description}\n"
                    f"**Punishment:** `{punishment}`"
                ),
                color=0xe74c3c,
                timestamp=datetime.now(timezone.utc),
            )
            await try_dm(owner, embed=embed)

        # Log to mod channel
        log_e = log_embed(
            action=f"🚨 Antinuke — {module.title()}",
            user=member,
            moderator=guild.me,
            reason=description,
            color=0xe74c3c,
        )
        await dispatch_mod_log(self.bot, guild, log_e)

    # ────────────────────────────────────────────────────────────────────────────
    # Antinuke event listeners
    # ────────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await asyncio.sleep(0.5)  # let audit log populate
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.ban):
                if e.target and e.target.id == user.id:
                    entry = e
                    break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "ban")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "ban", f"Mass ban — banned {user}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        await asyncio.sleep(0.5)
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.kick):
                if e.target and e.target.id == member.id:
                    entry = e
                    break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "kick")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "kick", f"Mass kick — kicked {member}")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        await asyncio.sleep(0.3)
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.role_delete):
                if e.target and e.target.id == role.id:
                    entry = e
                    break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "role")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "role", f"Mass role deletion — deleted @{role.name}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        await asyncio.sleep(0.3)
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.channel_delete):
                if e.target and e.target.id == channel.id:
                    entry = e
                    break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "channel")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "channel", f"Mass channel deletion — deleted #{channel.name}")

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        removed = set(e.id for e in before) - set(e.id for e in after)
        if not removed:
            return
        await asyncio.sleep(0.3)
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.emoji_delete):
                entry = e
                break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "emoji")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "emoji", f"Mass emoji deletion — {len(removed)} emojis deleted")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.TextChannel):
        guild = channel.guild
        await asyncio.sleep(0.3)
        entry = None
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.webhook_create):
                entry = e
                break
        except discord.Forbidden:
            return
        if not entry:
            return
        actor_id = entry.user.id
        if actor_id == self.bot.user.id:
            return
        triggered = await self.track_action(guild, actor_id, "webhook")
        if triggered:
            await self.execute_antinuke(guild, actor_id, "webhook", "Mass webhook creation")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle both antinuke bot-add and antiraid mass-join."""
        guild = member.guild

        # ── Antinuke: bot add ────────────────────────────────────────────────
        if member.bot:
            await asyncio.sleep(0.3)
            try:
                async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.bot_add):
                    if e.target and e.target.id == member.id:
                        actor_id = e.user.id
                        if actor_id == self.bot.user.id:
                            break
                        triggered = await self.track_action(guild, actor_id, "botadd")
                        if triggered:
                            await self.execute_antinuke(guild, actor_id, "botadd", f"Unauthorized bot add — {member}")
                        break
            except discord.Forbidden:
                pass

        # ── Antiraid ─────────────────────────────────────────────────────────
        await self._handle_antiraid(member)

    async def _handle_antiraid(self, member: discord.Member):
        guild = member.guild
        db = self.bot.db

        async with db.execute(
            "SELECT * FROM antiraid_config WHERE guild_id=?", (guild.id,)
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg:
            return

        # Check whitelist
        async with db.execute(
            "SELECT 1 FROM antiraid_whitelist WHERE guild_id=? AND user_id=?",
            (guild.id, member.id),
        ) as cur:
            if await cur.fetchone():
                return

        # ── Avatar check ─────────────────────────────────────────────────────
        if cfg["avatar_on"] and not member.avatar:
            await self._raid_punish(guild, member, cfg["avatar_action"], "No avatar")
            return

        # ── Account age check ─────────────────────────────────────────────────
        if cfg["age_on"]:
            account_age_days = (datetime.now(timezone.utc) - member.created_at).days
            if account_age_days < cfg["age_threshold"]:
                await self._raid_punish(guild, member, cfg["age_action"],
                    f"Account too new ({account_age_days}d < {cfg['age_threshold']}d)")
                return

        # ── Mass join detection ───────────────────────────────────────────────
        if cfg["massjoin_on"] and not cfg["raid_state"]:
            now = utcnow()
            # Record this join
            await db.execute(
                "INSERT OR REPLACE INTO recent_joins (guild_id, user_id, joined_at) VALUES (?,?,?)",
                (guild.id, member.id, now),
            )
            await db.commit()

            # Count joins in the last 10 seconds
            async with db.execute(
                "SELECT COUNT(*) FROM recent_joins WHERE guild_id=? AND joined_at > ?",
                (guild.id, now - 10),
            ) as cur:
                row = await cur.fetchone()
            join_count = row[0]

            if join_count >= cfg["massjoin_thresh"]:
                # Trigger raid state
                await db.execute(
                    "UPDATE antiraid_config SET raid_state=1 WHERE guild_id=?",
                    (guild.id,),
                )
                await db.commit()

                # Lock channels if configured
                if cfg["massjoin_lock"]:
                    await self._lockdown_guild(guild, True)

                # Notify owner
                owner = guild.owner
                if owner:
                    embed = discord.Embed(
                        title="🚨 Raid Detected",
                        description=(
                            f"**{join_count} accounts** joined within 10 seconds!\n\n"
                            f"Threshold: `{cfg['massjoin_thresh']}`\n"
                            f"Channels {'locked 🔒' if cfg['massjoin_lock'] else 'not locked'}\n\n"
                            f"Use `/antiraid state off` to lift the raid state."
                        ),
                        color=0xe74c3c,
                        timestamp=datetime.now(timezone.utc),
                    )
                    await try_dm(owner, embed=embed)

            # Punish the joining member if raid active or punish flag set
            if cfg["raid_state"] or (join_count >= cfg["massjoin_thresh"] and cfg["massjoin_punish"]):
                await self._raid_punish(guild, member, cfg["massjoin_action"], "Mass join raid")

    async def _raid_punish(
        self,
        guild: discord.Guild,
        member: discord.Member,
        action: str,
        reason: str,
    ):
        try:
            if action == "ban":
                await guild.ban(member, reason=f"[Antiraid] {reason}", delete_message_days=0)
            elif action == "kick":
                await guild.kick(member, reason=f"[Antiraid] {reason}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _lockdown_guild(self, guild: discord.Guild, lock: bool):
        """Lock or unlock all text channels."""
        for channel in guild.text_channels:
            try:
                overwrite = channel.overwrites_for(guild.default_role)
                overwrite.send_messages = not lock if lock else None
                await channel.set_permissions(
                    guild.default_role,
                    overwrite=overwrite,
                    reason="[Antiraid] Automatic lockdown" if lock else "[Antiraid] Raid state lifted",
                )
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        """Detect vanity URL changes."""
        if before.vanity_url_code == after.vanity_url_code:
            return
        guild = after
        async with self.bot.db.execute(
            "SELECT enabled FROM antinuke_modules WHERE guild_id=? AND module='vanity'",
            (guild.id,),
        ) as cur:
            cfg = await cur.fetchone()
        if not cfg or not cfg["enabled"]:
            return
        await asyncio.sleep(0.5)
        try:
            async for e in guild.audit_logs(limit=3, action=discord.AuditLogAction.guild_update):
                actor_id = e.user.id
                if actor_id == self.bot.user.id:
                    return
                await self.execute_antinuke(guild, actor_id, "vanity", "Vanity URL changed")
                break
        except discord.Forbidden:
            pass

    # ────────────────────────────────────────────────────────────────────────────
    # /antinuke commands
    # ────────────────────────────────────────────────────────────────────────────

    antinuke = app_commands.Group(
        name="antinuke",
        description="Configure the antinuke protection system",
        guild_only=True,
    )

    @antinuke.command(name="enable", description="Enable the antinuke system")
    @is_antinuke_admin()
    async def an_enable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO antinuke_config (guild_id, enabled) VALUES (?,1)",
            (interaction.guild.id,),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("Antinuke system **enabled**."), ephemeral=True
        )

    @antinuke.command(name="disable", description="Disable the antinuke system")
    @is_antinuke_admin()
    async def an_disable(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "UPDATE antinuke_config SET enabled=0 WHERE guild_id=?",
            (interaction.guild.id,),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=warn_embed("Antinuke system **disabled**."), ephemeral=True
        )

    @antinuke.command(name="module", description="Enable or disable an antinuke module")
    @app_commands.describe(
        module="Which module to configure",
        enabled="Turn on or off",
        threshold="Actions before punishment (1-10)",
        punishment="What to do to the offender",
        count_commands="Count bot commands toward threshold",
    )
    @app_commands.choices(
        module=[app_commands.Choice(name=m, value=m) for m in AN_MODULES],
        enabled=[
            app_commands.Choice(name="On", value=1),
            app_commands.Choice(name="Off", value=0),
        ],
        punishment=[app_commands.Choice(name=p, value=p) for p in PUNISHMENTS],
    )
    @is_antinuke_admin()
    async def an_module(
        self,
        interaction: discord.Interaction,
        module: str,
        enabled: int,
        threshold: Optional[int] = None,
        punishment: Optional[str] = None,
        count_commands: Optional[bool] = None,
    ):
        gid = interaction.guild.id
        # Ensure antinuke row exists
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antinuke_config (guild_id) VALUES (?)", (gid,)
        )

        # Get current module config or defaults
        async with self.bot.db.execute(
            "SELECT * FROM antinuke_modules WHERE guild_id=? AND module=?",
            (gid, module),
        ) as cur:
            existing = await cur.fetchone()

        thr  = threshold    if threshold    is not None else (existing["threshold"]  if existing else 3)
        pun  = punishment   if punishment   is not None else (existing["punishment"] if existing else "ban")
        cmds = int(count_commands) if count_commands is not None else (existing["count_cmds"] if existing else 1)

        if thr < 1 or thr > 10:
            return await interaction.response.send_message(
                embed=error_embed("Threshold must be between 1 and 10."), ephemeral=True
            )

        await self.bot.db.execute(
            """INSERT OR REPLACE INTO antinuke_modules
               (guild_id, module, enabled, threshold, punishment, count_cmds)
               VALUES (?,?,?,?,?,?)""",
            (gid, module, enabled, thr, pun, cmds),
        )
        await self.bot.db.commit()

        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                f"Module **{module}** {status}.\n"
                f"Threshold: `{thr}` · Punishment: `{pun}`"
            ),
            ephemeral=True,
        )

    @antinuke.command(name="whitelist", description="Exempt a user from antinuke checks")
    @app_commands.describe(user="User to whitelist (or un-whitelist)")
    @is_antinuke_admin()
    async def an_whitelist(self, interaction: discord.Interaction, user: discord.Member):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT 1 FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
            (gid, user.id),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
                (gid, user.id),
            )
            msg = f"{user.mention} removed from antinuke whitelist."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?,?)",
                (gid, user.id),
            )
            msg = f"{user.mention} added to antinuke whitelist."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @antinuke.command(name="admin", description="Grant a user antinuke admin access")
    @app_commands.describe(user="User to grant/revoke antinuke admin")
    async def an_admin(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message(
                embed=error_embed("Only the server owner can manage antinuke admins."),
                ephemeral=True,
            )
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT 1 FROM antinuke_admins WHERE guild_id=? AND user_id=?",
            (gid, user.id),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM antinuke_admins WHERE guild_id=? AND user_id=?", (gid, user.id)
            )
            msg = f"{user.mention} **removed** from antinuke admins."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO antinuke_admins (guild_id, user_id) VALUES (?,?)",
                (gid, user.id),
            )
            msg = f"{user.mention} **granted** antinuke admin."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @antinuke.command(name="config", description="View current antinuke configuration")
    @is_antinuke_admin()
    async def an_config(self, interaction: discord.Interaction):
        gid = interaction.guild.id

        async with self.bot.db.execute(
            "SELECT enabled FROM antinuke_config WHERE guild_id=?", (gid,)
        ) as cur:
            cfg = await cur.fetchone()

        async with self.bot.db.execute(
            "SELECT * FROM antinuke_modules WHERE guild_id=?", (gid,)
        ) as cur:
            modules = await cur.fetchall()

        async with self.bot.db.execute(
            "SELECT user_id FROM antinuke_whitelist WHERE guild_id=?", (gid,)
        ) as cur:
            whitelist = [r["user_id"] for r in await cur.fetchall()]

        status = "✅ Enabled" if (cfg and cfg["enabled"]) else "❌ Disabled"
        embed = discord.Embed(
            title="🛡️ Antinuke Configuration",
            color=0x5865F2,
        )
        embed.add_field(name="Status", value=status, inline=False)

        mod_lines = []
        mod_map = {m["module"]: m for m in modules}
        for module in AN_MODULES:
            m = mod_map.get(module)
            if m and m["enabled"]:
                mod_lines.append(
                    f"✅ **{module}** — threshold `{m['threshold']}` · punish `{m['punishment']}`"
                )
            else:
                mod_lines.append(f"❌ **{module}**")

        embed.add_field(name="Modules", value="\n".join(mod_lines), inline=False)

        wl_text = ", ".join(f"<@{uid}>" for uid in whitelist) if whitelist else "None"
        embed.add_field(name="Whitelist", value=wl_text, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────────────────────────────────────
    # /antiraid commands
    # ────────────────────────────────────────────────────────────────────────────

    antiraid = app_commands.Group(
        name="antiraid",
        description="Configure the antiraid / join gate system",
        guild_only=True,
    )

    async def _ensure_antiraid(self, guild_id: int):
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antiraid_config (guild_id) VALUES (?)", (guild_id,)
        )
        await self.bot.db.commit()

    @antiraid.command(name="massjoin", description="Configure mass join detection")
    @app_commands.describe(
        enabled="Enable or disable",
        threshold="Joins per 10 seconds to trigger (default 5)",
        action="Punishment for raiders",
        lock_channels="Lock all channels when triggered",
        punish_joiners="Punish members who joined during raid",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("ban", "kick")],
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_massjoin(
        self,
        interaction: discord.Interaction,
        enabled: int,
        threshold: Optional[int] = 5,
        action: Optional[str] = "kick",
        lock_channels: Optional[bool] = False,
        punish_joiners: Optional[bool] = True,
    ):
        gid = interaction.guild.id
        await self._ensure_antiraid(gid)
        await self.bot.db.execute(
            """UPDATE antiraid_config SET
               massjoin_on=?, massjoin_thresh=?, massjoin_action=?,
               massjoin_lock=?, massjoin_punish=?
               WHERE guild_id=?""",
            (enabled, threshold, action, int(lock_channels), int(punish_joiners), gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                f"Mass join detection **{status}**.\n"
                f"Threshold: `{threshold}/10s` · Action: `{action}` · "
                f"Lock: `{lock_channels}` · Punish joiners: `{punish_joiners}`"
            ),
            ephemeral=True,
        )

    @antiraid.command(name="avatar", description="Require members to have an avatar")
    @app_commands.describe(enabled="Enable or disable", action="Punishment")
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("ban", "kick")],
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_avatar(
        self,
        interaction: discord.Interaction,
        enabled: int,
        action: Optional[str] = "kick",
    ):
        gid = interaction.guild.id
        await self._ensure_antiraid(gid)
        await self.bot.db.execute(
            "UPDATE antiraid_config SET avatar_on=?, avatar_action=? WHERE guild_id=?",
            (enabled, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(f"Avatar requirement **{status}** · Action: `{action}`"),
            ephemeral=True,
        )

    @antiraid.command(name="age", description="Set minimum account age requirement")
    @app_commands.describe(
        enabled="Enable or disable",
        days="Minimum account age in days",
        action="Punishment",
    )
    @app_commands.choices(
        enabled=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
        action=[app_commands.Choice(name=a, value=a) for a in ("ban", "kick")],
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_age(
        self,
        interaction: discord.Interaction,
        enabled: int,
        days: Optional[int] = 7,
        action: Optional[str] = "kick",
    ):
        gid = interaction.guild.id
        await self._ensure_antiraid(gid)
        await self.bot.db.execute(
            "UPDATE antiraid_config SET age_on=?, age_threshold=?, age_action=? WHERE guild_id=?",
            (enabled, days, action, gid),
        )
        await self.bot.db.commit()
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                f"Account age requirement **{status}**.\n"
                f"Minimum: `{days} days` · Action: `{action}`"
            ),
            ephemeral=True,
        )

    @antiraid.command(name="whitelist", description="Whitelist a user from antiraid checks")
    @app_commands.describe(user="User to whitelist / un-whitelist")
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_whitelist(self, interaction: discord.Interaction, user: discord.Member):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT 1 FROM antiraid_whitelist WHERE guild_id=? AND user_id=?",
            (gid, user.id),
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM antiraid_whitelist WHERE guild_id=? AND user_id=?", (gid, user.id)
            )
            msg = f"{user.mention} **removed** from antiraid whitelist."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO antiraid_whitelist (guild_id, user_id) VALUES (?,?)",
                (gid, user.id),
            )
            msg = f"{user.mention} **added** to antiraid whitelist."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @antiraid.command(name="state", description="Manually lift or trigger the raid state")
    @app_commands.describe(active="on = raid active (locked), off = raid lifted")
    @app_commands.choices(
        active=[app_commands.Choice(name="On", value=1), app_commands.Choice(name="Off", value=0)],
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_state(self, interaction: discord.Interaction, active: int):
        gid = interaction.guild.id
        await self._ensure_antiraid(gid)
        await self.bot.db.execute(
            "UPDATE antiraid_config SET raid_state=? WHERE guild_id=?", (active, gid)
        )
        await self.bot.db.commit()

        if not active:
            # Unlock channels
            await self._lockdown_guild(interaction.guild, False)
            await interaction.response.send_message(
                embed=success_embed("Raid state **lifted**. Channels unlocked and events restored."),
                ephemeral=True,
            )
        else:
            await self._lockdown_guild(interaction.guild, True)
            await interaction.response.send_message(
                embed=warn_embed("Raid state **activated**. Channels locked."),
                ephemeral=True,
            )

    @antiraid.command(name="config", description="View antiraid configuration")
    @app_commands.checks.has_permissions(administrator=True)
    async def ar_config(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT * FROM antiraid_config WHERE guild_id=?", (gid,)
        ) as cur:
            cfg = await cur.fetchone()

        if not cfg:
            return await interaction.response.send_message(
                embed=info_embed("No antiraid configuration yet. Use `/antiraid massjoin`, `/antiraid avatar`, or `/antiraid age` to set up."),
                ephemeral=True,
            )

        embed = discord.Embed(title="🔰 Antiraid Configuration", color=0x5865F2)
        embed.add_field(
            name="Mass Join",
            value=(
                f"{'✅' if cfg['massjoin_on'] else '❌'} Enabled\n"
                f"Threshold: `{cfg['massjoin_thresh']}/10s`\n"
                f"Action: `{cfg['massjoin_action']}`\n"
                f"Lock channels: `{bool(cfg['massjoin_lock'])}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Avatar",
            value=(
                f"{'✅' if cfg['avatar_on'] else '❌'} Enabled\n"
                f"Action: `{cfg['avatar_action']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Account Age",
            value=(
                f"{'✅' if cfg['age_on'] else '❌'} Enabled\n"
                f"Min age: `{cfg['age_threshold']} days`\n"
                f"Action: `{cfg['age_action']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Raid State",
            value="🚨 **ACTIVE**" if cfg["raid_state"] else "✅ Normal",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ────────────────────────────────────────────────────────────────────────────
    # /fakepermissions commands
    # ────────────────────────────────────────────────────────────────────────────

    fakeperm = app_commands.Group(
        name="fakepermissions",
        description="Restrict moderators to use only bot commands",
        guild_only=True,
    )

    @fakeperm.command(name="grant", description="Grant fake permissions to a role")
    @app_commands.describe(
        role="The role to grant permissions to",
        permissions="Comma-separated permissions (e.g. ban_members, kick_members)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def fp_grant(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        permissions: str,
    ):
        gid = interaction.guild.id
        perms = [p.strip().lower() for p in permissions.split(",")]
        valid = [p for p in perms if p in PERMISSION_MAP]
        invalid = [p for p in perms if p not in PERMISSION_MAP]

        for perm in valid:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO fake_permissions (guild_id, role_id, permission) VALUES (?,?,?)",
                (gid, role.id, perm),
            )
        await self.bot.db.commit()

        lines = [f"✅ Granted `{p}` to {role.mention}" for p in valid]
        if invalid:
            lines.append(f"⚠️ Unknown permissions: `{', '.join(invalid)}`")

        await interaction.response.send_message(
            embed=info_embed("\n".join(lines), title="Fake Permissions Updated"),
            ephemeral=True,
        )

    @fakeperm.command(name="revoke", description="Revoke fake permissions from a role")
    @app_commands.describe(
        role="The role to revoke from",
        permissions="Comma-separated permissions to revoke",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def fp_revoke(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        permissions: str,
    ):
        gid = interaction.guild.id
        perms = [p.strip().lower() for p in permissions.split(",")]
        for perm in perms:
            await self.bot.db.execute(
                "DELETE FROM fake_permissions WHERE guild_id=? AND role_id=? AND permission=?",
                (gid, role.id, perm),
            )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(f"Revoked `{', '.join(perms)}` from {role.mention}."),
            ephemeral=True,
        )

    @fakeperm.command(name="list", description="View fake permissions")
    @app_commands.describe(role="Filter by role (optional)")
    @app_commands.checks.has_permissions(administrator=True)
    async def fp_list(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
    ):
        gid = interaction.guild.id
        if role:
            async with self.bot.db.execute(
                "SELECT permission FROM fake_permissions WHERE guild_id=? AND role_id=?",
                (gid, role.id),
            ) as cur:
                rows = await cur.fetchall()
            perms = [r["permission"] for r in rows]
            desc = f"**{role.mention}**: {', '.join(f'`{p}`' for p in perms) if perms else 'No fake permissions'}"
        else:
            async with self.bot.db.execute(
                "SELECT role_id, permission FROM fake_permissions WHERE guild_id=? ORDER BY role_id",
                (gid,),
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                desc = "No fake permissions configured."
            else:
                from collections import defaultdict
                grouped = defaultdict(list)
                for r in rows:
                    grouped[r["role_id"]].append(r["permission"])
                lines = []
                for role_id, perms in grouped.items():
                    r = interaction.guild.get_role(role_id)
                    name = r.mention if r else f"<@&{role_id}>"
                    lines.append(f"{name}: {', '.join(f'`{p}`' for p in perms)}")
                desc = "\n".join(lines)

        await interaction.response.send_message(
            embed=info_embed(desc, title="Fake Permissions"),
            ephemeral=True,
        )

    @fakeperm.command(name="reset", description="Reset ALL fake permissions for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def fp_reset(self, interaction: discord.Interaction):
        await self.bot.db.execute(
            "DELETE FROM fake_permissions WHERE guild_id=?", (interaction.guild.id,)
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed("All fake permissions reset."), ephemeral=True
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /bind staff command
    # ────────────────────────────────────────────────────────────────────────────

    bind = app_commands.Group(
        name="bind",
        description="Bind roles as staff roles",
        guild_only=True,
    )

    @bind.command(name="staff", description="Set or unset a role as a staff role")
    @app_commands.describe(role="The role to toggle as staff")
    @app_commands.checks.has_permissions(administrator=True)
    async def bind_staff(self, interaction: discord.Interaction, role: discord.Role):
        gid = interaction.guild.id
        async with self.bot.db.execute(
            "SELECT 1 FROM staff_roles WHERE guild_id=? AND role_id=?", (gid, role.id)
        ) as cur:
            exists = await cur.fetchone()

        if exists:
            await self.bot.db.execute(
                "DELETE FROM staff_roles WHERE guild_id=? AND role_id=?", (gid, role.id)
            )
            msg = f"{role.mention} is **no longer** a staff role."
        else:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?,?)",
                (gid, role.id),
            )
            msg = f"{role.mention} is now a **staff role**."

        await self.bot.db.commit()
        await interaction.response.send_message(embed=success_embed(msg), ephemeral=True)

    @bind.command(name="stafflist", description="View all configured staff roles")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bind_stafflist(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT role_id FROM staff_roles WHERE guild_id=?", (interaction.guild.id,)
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message(
                embed=info_embed("No staff roles configured. Use `/bind staff @role`."),
                ephemeral=True,
            )
        mentions = [f"<@&{r['role_id']}>" for r in rows]
        await interaction.response.send_message(
            embed=info_embed(", ".join(mentions), title="Staff Roles"),
            ephemeral=True,
        )

    # ────────────────────────────────────────────────────────────────────────────
    # /recentban and /raid cleanup commands
    # ────────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="recentban",
        description="Ban the last N members who joined the server",
    )
    @app_commands.describe(
        amount="How many recent joiners to ban (max 100)",
        reason="Reason for the ban",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def recentban(
        self,
        interaction: discord.Interaction,
        amount: int,
        reason: Optional[str] = "Mass ban — recent joiners",
    ):
        if amount < 1 or amount > 100:
            return await interaction.response.send_message(
                embed=error_embed("Amount must be between 1 and 100."), ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Get recent members sorted by join date
        members = sorted(
            [m for m in guild.members if not m.bot and m != guild.me],
            key=lambda m: m.joined_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:amount]

        banned = 0
        for member in members:
            try:
                await guild.ban(member, reason=f"[Recentban] {reason}", delete_message_days=0)
                banned += 1
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(
            embed=success_embed(f"Banned **{banned}/{amount}** recent joiners.\nReason: {reason}"),
            ephemeral=True,
        )

    @app_commands.command(
        name="raid",
        description="Ban or kick all members who joined within a time window",
    )
    @app_commands.describe(
        duration="How far back to look (e.g. 2h, 30m)",
        action="Ban or kick",
        reason="Reason",
    )
    @app_commands.choices(
        action=[app_commands.Choice(name="ban", value="ban"), app_commands.Choice(name="kick", value="kick")],
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def raid_cleanup(
        self,
        interaction: discord.Interaction,
        duration: str,
        action: str = "ban",
        reason: Optional[str] = "Raid cleanup",
    ):
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                embed=error_embed("Invalid duration. Examples: `2h`, `30m`, `1h30m`"),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=secs)
        guild = interaction.guild

        targets = [
            m for m in guild.members
            if not m.bot and m != guild.me and m.joined_at and m.joined_at > cutoff
        ]

        acted = 0
        for member in targets:
            try:
                if action == "ban":
                    await guild.ban(member, reason=f"[Raid cleanup] {reason}", delete_message_days=0)
                else:
                    await guild.kick(member, reason=f"[Raid cleanup] {reason}")
                acted += 1
            except (discord.Forbidden, discord.HTTPException):
                pass

        human_dur = seconds_to_human(secs)
        await interaction.followup.send(
            embed=success_embed(
                f"**{action.title()}ned** {acted} member(s) who joined in the last {human_dur}.\n"
                f"Reason: {reason}"
            ),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Security(bot))
