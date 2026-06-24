"""
utils.py — shared helpers, converters, decorators and embed builders
imported by every cog.
"""

from __future__ import annotations

import re
import json
import time
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands


# ── Colour constants — Whimsy mythical palette ──────────────────────────────────
COL_GREEN   = 0xF4C542  # soft gold  — success
COL_RED     = 0x9B2335  # deep crimson — error
COL_BLUE    = 0x7B4FA6  # arcane purple — info
COL_YELLOW  = 0xD4820A  # amber — warning
COL_PURPLE  = 0x5B2D8E  # deep violet
COL_ORANGE  = 0xC0622A  # ember orange
COL_BLURPLE = 0x7B4FA6  # matches info


# ── Embed helpers ───────────────────────────────────────────────────────────────
def success_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"✨  {description}", color=COL_GREEN)
    if title:
        e.title = title
    return e


def error_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"🔮  {description}", color=COL_RED)
    if title:
        e.title = title
    return e


def info_embed(description: str, title: str = None, color: int = COL_BLUE) -> discord.Embed:
    e = discord.Embed(description=description, color=color)
    if title:
        e.title = title
    return e


def warn_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"⚠️  {description}", color=COL_YELLOW)
    if title:
        e.title = title
    return e


def log_embed(
    action: str,
    user: discord.Member | discord.User,
    moderator: discord.Member | discord.User,
    reason: str = None,
    duration: str = None,
    case: int = None,
    color: int = COL_RED,
    extra_fields: list[tuple[str, str, bool]] = None,
) -> discord.Embed:
    """Standard mod-action log embed."""
    e = discord.Embed(
        title=f"{action}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name="User", value=f"{user.mention} `{user}` ({user.id})", inline=False)
    e.add_field(name="Moderator", value=f"{moderator.mention} `{moderator}`", inline=False)
    if duration:
        e.add_field(name="Duration", value=duration, inline=True)
    if reason:
        e.add_field(name="Reason", value=reason, inline=False)
    if case:
        e.set_footer(text=f"Case #{case}")
    if extra_fields:
        for name, value, inline in extra_fields:
            e.add_field(name=name, value=value, inline=inline)
    e.set_thumbnail(url=user.display_avatar.url)
    return e


# ── Duration parsing ────────────────────────────────────────────────────────────
DURATION_REGEX = re.compile(
    r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?",
    re.IGNORECASE,
)

def parse_duration(raw: str) -> Optional[int]:
    """
    Parse a human duration string (e.g. '1h30m', '7d', '2w3d') → seconds.
    Returns None if the string is empty or zero.
    """
    raw = raw.strip()
    m = DURATION_REGEX.fullmatch(raw)
    if not m or not any(m.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    total = (weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds)
    return total if total > 0 else None


def seconds_to_human(seconds: int) -> str:
    """Convert seconds → human-readable string (e.g. '2 days, 3 hours')."""
    if seconds <= 0:
        return "0 seconds"
    parts = []
    units = [
        (604800, "week"),
        (86400,  "day"),
        (3600,   "hour"),
        (60,     "minute"),
        (1,      "second"),
    ]
    for unit_sec, name in units:
        val = seconds // unit_sec
        seconds %= unit_sec
        if val:
            parts.append(f"{val} {name}{'s' if val != 1 else ''}")
    return ", ".join(parts)


def utcnow() -> int:
    """Current UTC timestamp as int."""
    return int(time.time())


def discord_timestamp(ts: int, style: str = "R") -> str:
    """Format a Unix timestamp as a Discord timestamp mention."""
    return f"<t:{ts}:{style}>"


# ── Permission helpers ──────────────────────────────────────────────────────────
PERMISSION_MAP = {
    "administrator":      "administrator",
    "ban_members":        "ban_members",
    "kick_members":       "kick_members",
    "manage_guild":       "manage_guild",
    "manage_channels":    "manage_channels",
    "manage_roles":       "manage_roles",
    "manage_messages":    "manage_messages",
    "manage_webhooks":    "manage_webhooks",
    "manage_expressions": "manage_emojis_and_stickers",
    "manage_nicknames":   "manage_nicknames",
    "moderate_members":   "moderate_members",
    "mention_everyone":   "mention_everyone",
    "view_audit_log":     "view_audit_log",
}

def has_bot_perms(**perms) -> bool:
    """Check if the bot has the required permissions."""
    # Used inside command checks
    async def predicate(interaction: discord.Interaction) -> bool:
        missing = [
            perm for perm, value in perms.items()
            if not getattr(interaction.guild.me.guild_permissions, perm, False) == value
        ]
        if missing:
            raise app_commands.BotMissingPermissions(missing)
        return True
    return app_commands.check(predicate)


async def check_fake_perms(
    db,
    interaction: discord.Interaction,
    permission: str,
) -> bool:
    """
    Check if the user has the given permission either natively or via
    the fake permissions system.
    Returns True if allowed, False if not.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    # Server owner always allowed
    if member.id == interaction.guild.owner_id:
        return True

    # Native Discord permission
    native_perm = PERMISSION_MAP.get(permission, permission)
    if getattr(member.guild_permissions, native_perm, False):
        return True

    # Check fake permissions
    role_ids = [r.id for r in member.roles]
    if not role_ids:
        return False

    placeholders = ",".join("?" * len(role_ids))
    async with db.execute(
        f"""SELECT 1 FROM fake_permissions
            WHERE guild_id=? AND permission=? AND role_id IN ({placeholders})
            LIMIT 1""",
        (interaction.guild.id, permission, *role_ids),
    ) as cur:
        row = await cur.fetchone()
    return row is not None


def require_fake_perm(permission: str):
    """
    Decorator: user must have `permission` either natively or via fake_permissions.
    Use on slash commands where granular control is needed.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        bot = interaction.client
        allowed = await check_fake_perms(bot.db, interaction, permission)
        if not allowed:
            raise app_commands.CheckFailure(
                f"You need the `{permission}` permission to use this."
            )
        return True
    return app_commands.check(predicate)


def is_staff():
    """Check: user has a staff role set via /bind staff."""
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.id == interaction.guild.owner_id:
            return True
        if member.guild_permissions.administrator:
            return True
        bot = interaction.client
        role_ids = [r.id for r in member.roles]
        if not role_ids:
            return False
        placeholders = ",".join("?" * len(role_ids))
        async with bot.db.execute(
            f"SELECT 1 FROM staff_roles WHERE guild_id=? AND role_id IN ({placeholders}) LIMIT 1",
            (interaction.guild.id, *role_ids),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise app_commands.CheckFailure("You must be a staff member to use this.")
        return True
    return app_commands.check(predicate)


def is_antinuke_admin():
    """Check: user is server owner or antinuke admin."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.guild.owner_id:
            return True
        bot = interaction.client
        async with bot.db.execute(
            "SELECT 1 FROM antinuke_admins WHERE guild_id=? AND user_id=? LIMIT 1",
            (interaction.guild.id, interaction.user.id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise app_commands.CheckFailure("Only the server owner or antinuke admins can use this.")
        return True
    return app_commands.check(predicate)


def guild_only():
    """Ensure command runs in a guild."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command can only be used in a server.")
        return True
    return app_commands.check(predicate)


# ── Variable scripting engine ───────────────────────────────────────────────────
def resolve_variables(
    template: str,
    member: discord.Member | discord.User = None,
    guild: discord.Guild = None,
    extra: dict = None,
) -> str:
    """
    Resolve {variable} placeholders in message templates.
    Compatible with Bleed's variable system.
    """
    vars_map: dict[str, str] = {}

    if member:
        vars_map.update({
            "{user}":               str(member),
            "{user.mention}":       member.mention,
            "{user.name}":          member.name,
            "{user.display_name}":  getattr(member, "display_name", member.name),
            "{user.id}":            str(member.id),
            "{user.avatar}":        str(member.display_avatar.url),
            "{user.created_at}":    f"<t:{int(member.created_at.timestamp())}:D>",
            "{user.joined_at}":     f"<t:{int(member.joined_at.timestamp())}:D>" if hasattr(member, "joined_at") and member.joined_at else "Unknown",
        })

    if guild:
        vars_map.update({
            "{guild}":              guild.name,
            "{guild.name}":         guild.name,
            "{guild.id}":           str(guild.id),
            "{guild.member_count}": str(guild.member_count),
            "{guild.owner}":        str(guild.owner) if guild.owner else "Unknown",
            "{guild.icon}":         str(guild.icon.url) if guild.icon else "",
            "{guild.created_at}":   f"<t:{int(guild.created_at.timestamp())}:D>",
        })

    if extra:
        for k, v in extra.items():
            vars_map[k] = str(v)

    result = template
    for placeholder, value in vars_map.items():
        result = result.replace(placeholder, value)
    return result


def parse_embed_script(raw: str) -> Optional[discord.Embed]:
    """
    Parse Bleed-style embed script:
    {embed}$v{title: ...}$v{description: ...}$v{color: #hex}$v{field: name | value | inline}
    Returns a discord.Embed or None if not an embed script.
    """
    if not raw.startswith("{embed}"):
        return None

    raw = raw[len("{embed}"):]
    parts = raw.split("$v")
    embed = discord.Embed(color=COL_BLUE)

    for part in parts:
        part = part.strip().strip("{}")
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "title":
            embed.title = value
        elif key == "description":
            embed.description = value
        elif key == "color" or key == "colour":
            try:
                embed.color = int(value.lstrip("#"), 16)
            except ValueError:
                pass
        elif key == "footer":
            embed.set_footer(text=value)
        elif key == "image":
            embed.set_image(url=value)
        elif key == "thumbnail":
            embed.set_thumbnail(url=value)
        elif key == "author":
            embed.set_author(name=value)
        elif key == "field":
            segments = [s.strip() for s in value.split("|")]
            if len(segments) >= 2:
                inline = len(segments) >= 3 and segments[2].lower() in ("true", "yes", "1")
                embed.add_field(name=segments[0], value=segments[1], inline=inline)
        elif key == "message":
            # {message: @user} — mention, not part of embed itself
            pass

    return embed


# ── Pagination helper ───────────────────────────────────────────────────────────
class Paginator(discord.ui.View):
    """Simple embed paginator with Previous / Next buttons."""

    def __init__(self, embeds: list[discord.Embed], author: discord.User, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.author = author
        self.page = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page == len(self.embeds) - 1
        for embed in self.embeds:
            embed.set_footer(text=f"Page {self.page + 1}/{len(self.embeds)}")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This isn't your paginator.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(self.embeds) - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)


def chunk_list(lst: list, size: int) -> list[list]:
    """Split a list into chunks of given size."""
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def build_pages(
    items: list[str],
    title: str,
    per_page: int = 10,
    color: int = COL_BLUE,
) -> list[discord.Embed]:
    """Build a list of embeds from a flat list of item strings."""
    if not items:
        return [discord.Embed(title=title, description="Nothing here yet.", color=color)]
    pages = []
    for chunk in chunk_list(items, per_page):
        e = discord.Embed(title=title, description="\n".join(chunk), color=color)
        pages.append(e)
    return pages


# ── Confirmation dialog ─────────────────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    """Two-button confirm/cancel dialog. Stores result in .value."""

    def __init__(self, author: discord.User, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.author = author
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Not your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()


# ── Case number helper ──────────────────────────────────────────────────────────
async def next_case_number(db, guild_id: int) -> int:
    """Get the next available case number for a guild."""
    async with db.execute(
        "SELECT COALESCE(MAX(case_number), 0) + 1 FROM cases WHERE guild_id=?",
        (guild_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0]


async def create_case(
    db,
    guild_id: int,
    user_id: int,
    mod_id: int,
    action: str,
    reason: str = None,
    duration: int = None,
) -> int:
    """Insert a new moderation case and return its case number."""
    case_num = await next_case_number(db, guild_id)
    await db.execute(
        """INSERT INTO cases
           (guild_id, case_number, user_id, mod_id, action, reason, duration, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (guild_id, case_num, user_id, mod_id, action, reason, duration, utcnow()),
    )
    await db.commit()
    return case_num


# ── Audit log helpers ───────────────────────────────────────────────────────────
async def fetch_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    within_seconds: int = 5,
) -> Optional[discord.AuditLogEntry]:
    """Fetch the most recent audit log entry matching action + target."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        async for entry in guild.audit_logs(limit=10, action=action):
            if entry.created_at < cutoff:
                break
            if entry.target and entry.target.id == target_id:
                return entry
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None


# ── Safe send helper ────────────────────────────────────────────────────────────
async def safe_send(
    destination: discord.abc.Messageable,
    content: str = None,
    embed: discord.Embed = None,
    **kwargs,
) -> Optional[discord.Message]:
    """Send a message, silently swallowing permission errors."""
    try:
        return await destination.send(content=content, embed=embed, **kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return None


async def try_dm(
    user: discord.User | discord.Member,
    embed: discord.Embed = None,
    content: str = None,
) -> bool:
    """Attempt to DM a user. Returns True on success."""
    try:
        await user.send(content=content, embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ── Mod-log dispatcher ──────────────────────────────────────────────────────────
async def dispatch_mod_log(
    bot,
    guild: discord.Guild,
    embed: discord.Embed,
):
    """Send embed to the guild's configured mod log channel."""
    async with bot.db.execute(
        "SELECT mod_log_channel FROM guild_config WHERE guild_id=?",
        (guild.id,),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["mod_log_channel"]:
        return
    ch = guild.get_channel(row["mod_log_channel"])
    if ch:
        await safe_send(ch, embed=embed)


# ── JSON helpers for DB storage ─────────────────────────────────────────────────
def to_json(obj) -> str:
    return json.dumps(obj)

def from_json(raw: str | None, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


# ── Target resolver ─────────────────────────────────────────────────────────────
async def resolve_member_or_user(
    guild: discord.Guild,
    user_id: int,
) -> Union[discord.Member, discord.User, None]:
    """Try to get a Member first, fall back to User fetch."""
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        pass
    try:
        return await guild.request_offline_members()
    except Exception:
        pass
    return None
