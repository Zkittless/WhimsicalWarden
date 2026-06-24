import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiosqlite
import logging
import os
import sys
from datetime import datetime

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("modbot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("modbot")

# ── Config ─────────────────────────────────────────────────────────────────────
# Set MODBOT_TOKEN and OWNER_ID in Railway's Variables tab (no .env file needed)
TOKEN    = os.getenv("MODBOT_TOKEN")
DB_PATH  = "data/modbot.db"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

COGS = [
    "cogs.security",
    "cogs.moderation",
    "cogs.automod",
    "cogs.logging",
    "cogs.configuration",
    "cogs.voicemaster",
    "cogs.tickets",
    "cogs.leveling",
    "cogs.giveaways",
    "cogs.utility",
]

# ── Bot class ──────────────────────────────────────────────────────────────────
class ModBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(
            command_prefix="!",   # fallback prefix, not used — slash only
            intents=intents,
            owner_id=OWNER_ID,
            help_command=None,
        )
        self.db: aiosqlite.Connection | None = None
        self.start_time = datetime.utcnow()

    # ── Database ───────────────────────────────────────────────────────────────
    async def setup_db(self):
        os.makedirs("data", exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self.db.commit()
        log.info("Database ready.")

    async def _create_tables(self):
        await self.db.executescript("""
        -- ── Guild configuration ───────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id        INTEGER PRIMARY KEY,
            prefix          TEXT    DEFAULT '/',
            mod_log_channel INTEGER,
            jail_channel    INTEGER,
            jail_role       INTEGER,
            mute_role       INTEGER,
            image_mute_role INTEGER,
            reaction_mute_role INTEGER,
            setup_done      INTEGER DEFAULT 0
        );

        -- ── Staff / fake permissions ──────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS staff_roles (
            guild_id INTEGER,
            role_id  INTEGER,
            PRIMARY KEY (guild_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS fake_permissions (
            guild_id    INTEGER,
            role_id     INTEGER,
            permission  TEXT,
            PRIMARY KEY (guild_id, role_id, permission)
        );

        -- ── Moderation cases ──────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS cases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            case_number INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            mod_id      INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            reason      TEXT,
            duration    INTEGER,           -- seconds, NULL = permanent
            created_at  INTEGER NOT NULL,
            active      INTEGER DEFAULT 1,
            UNIQUE(guild_id, case_number)
        );

        -- ── Warn escalation config ────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS warn_escalation (
            guild_id    INTEGER,
            warn_count  INTEGER,
            action      TEXT,              -- mute / kick / ban / tempban
            duration    INTEGER,           -- seconds for mute/tempban, NULL = permanent
            PRIMARY KEY (guild_id, warn_count)
        );

        -- ── Active temp punishments ───────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS temp_punishments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            action      TEXT    NOT NULL,  -- tempmute / tempban
            expires_at  INTEGER NOT NULL,
            case_id     INTEGER
        );

        -- ── Antinuke config ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS antinuke_config (
            guild_id    INTEGER PRIMARY KEY,
            enabled     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS antinuke_modules (
            guild_id    INTEGER,
            module      TEXT,              -- ban/kick/role/channel/emoji/webhook/botadd/vanity
            enabled     INTEGER DEFAULT 1,
            threshold   INTEGER DEFAULT 3,
            punishment  TEXT    DEFAULT 'ban',
            count_cmds  INTEGER DEFAULT 1,
            PRIMARY KEY (guild_id, module)
        );

        CREATE TABLE IF NOT EXISTS antinuke_whitelist (
            guild_id INTEGER,
            user_id  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS antinuke_admins (
            guild_id INTEGER,
            user_id  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        -- Tracks recent actions for threshold counting
        CREATE TABLE IF NOT EXISTS antinuke_actions (
            guild_id    INTEGER,
            user_id     INTEGER,
            module      TEXT,
            action_time INTEGER,
            PRIMARY KEY (guild_id, user_id, module, action_time)
        );

        -- ── Antiraid config ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS antiraid_config (
            guild_id        INTEGER PRIMARY KEY,
            massjoin_on     INTEGER DEFAULT 0,
            massjoin_thresh INTEGER DEFAULT 5,
            massjoin_action TEXT    DEFAULT 'kick',
            massjoin_lock   INTEGER DEFAULT 0,
            massjoin_punish INTEGER DEFAULT 1,
            avatar_on       INTEGER DEFAULT 0,
            avatar_action   TEXT    DEFAULT 'kick',
            age_on          INTEGER DEFAULT 0,
            age_threshold   INTEGER DEFAULT 7,
            age_action      TEXT    DEFAULT 'kick',
            raid_state      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS antiraid_whitelist (
            guild_id INTEGER,
            user_id  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        -- Tracks recent joins for massjoin detection
        CREATE TABLE IF NOT EXISTS recent_joins (
            guild_id   INTEGER,
            user_id    INTEGER,
            joined_at  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        -- ── AutoMod config ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS automod_config (
            guild_id            INTEGER PRIMARY KEY,
            -- Spam
            spam_on             INTEGER DEFAULT 0,
            spam_threshold      INTEGER DEFAULT 5,
            spam_interval       INTEGER DEFAULT 5,
            spam_action         TEXT    DEFAULT 'mute',
            -- Caps
            caps_on             INTEGER DEFAULT 0,
            caps_percent        INTEGER DEFAULT 70,
            caps_min_length     INTEGER DEFAULT 10,
            caps_action         TEXT    DEFAULT 'warn',
            -- Mass mentions
            mention_on          INTEGER DEFAULT 0,
            mention_threshold   INTEGER DEFAULT 5,
            mention_action      TEXT    DEFAULT 'mute',
            -- Emoji spam
            emoji_on            INTEGER DEFAULT 0,
            emoji_threshold     INTEGER DEFAULT 10,
            emoji_action        TEXT    DEFAULT 'warn',
            -- Duplicate messages
            duplicate_on        INTEGER DEFAULT 0,
            duplicate_count     INTEGER DEFAULT 3,
            duplicate_action    TEXT    DEFAULT 'warn',
            -- Links
            links_on            INTEGER DEFAULT 0,
            links_action        TEXT    DEFAULT 'delete',
            -- Invites
            invites_on          INTEGER DEFAULT 0,
            invites_action      TEXT    DEFAULT 'delete',
            -- Phishing
            phishing_on         INTEGER DEFAULT 1,
            phishing_action     TEXT    DEFAULT 'ban'
        );

        CREATE TABLE IF NOT EXISTS automod_word_filter (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  INTEGER,
            pattern   TEXT,
            is_regex  INTEGER DEFAULT 0,
            action    TEXT    DEFAULT 'delete',
            UNIQUE(guild_id, pattern)
        );

        CREATE TABLE IF NOT EXISTS automod_link_whitelist (
            guild_id INTEGER,
            domain   TEXT,
            PRIMARY KEY (guild_id, domain)
        );

        CREATE TABLE IF NOT EXISTS automod_link_blacklist (
            guild_id INTEGER,
            domain   TEXT,
            PRIMARY KEY (guild_id, domain)
        );

        CREATE TABLE IF NOT EXISTS automod_ignore (
            guild_id    INTEGER,
            target_id   INTEGER,
            target_type TEXT,    -- channel / role / user
            PRIMARY KEY (guild_id, target_id, target_type)
        );

        CREATE TABLE IF NOT EXISTS automod_escalation (
            guild_id   INTEGER,
            warn_count INTEGER,
            action     TEXT,
            duration   INTEGER,
            PRIMARY KEY (guild_id, warn_count)
        );

        -- ── Logging config ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id   INTEGER,
            channel_id INTEGER,
            event      TEXT,    -- messages/members/roles/channels/invites/emojis/voice
            PRIMARY KEY (guild_id, channel_id, event)
        );

        CREATE TABLE IF NOT EXISTS log_ignore (
            guild_id    INTEGER,
            target_id   INTEGER,
            target_type TEXT,   -- member / channel
            PRIMARY KEY (guild_id, target_id, target_type)
        );

        -- ── Welcome / goodbye / boost messages ───────────────────────────────
        CREATE TABLE IF NOT EXISTS system_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id      INTEGER,
            event_type    TEXT,    -- welcome / goodbye / boost
            channel_id    INTEGER,
            message       TEXT,
            self_destruct INTEGER DEFAULT 0,
            UNIQUE(guild_id, event_type, channel_id)
        );

        -- ── Auto-responders ───────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS autoresponders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id            INTEGER,
            trigger             TEXT,
            response            TEXT,
            not_strict          INTEGER DEFAULT 0,
            self_destruct       INTEGER DEFAULT 0,
            delete_trigger      INTEGER DEFAULT 0,
            reply               INTEGER DEFAULT 0,
            ignore_cmd_check    INTEGER DEFAULT 0,
            UNIQUE(guild_id, trigger)
        );

        CREATE TABLE IF NOT EXISTS autoresponder_restrictions (
            ar_id       INTEGER,
            target_id   INTEGER,
            target_type TEXT,    -- channel / role
            PRIMARY KEY (ar_id, target_id)
        );

        CREATE TABLE IF NOT EXISTS autoresponder_roles (
            ar_id   INTEGER,
            role_id INTEGER,
            action  TEXT,        -- add / remove
            PRIMARY KEY (ar_id, role_id)
        );

        -- ── Reaction roles ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS reaction_roles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            message_id  INTEGER,
            emoji       TEXT,
            role_id     INTEGER,
            UNIQUE(guild_id, message_id, emoji)
        );

        -- ── Button roles ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS button_roles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            message_id  INTEGER,
            label       TEXT,
            emoji       TEXT,
            role_id     INTEGER,
            style       INTEGER DEFAULT 2   -- discord button style
        );

        -- ── Starboard ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS starboard_config (
            guild_id    INTEGER PRIMARY KEY,
            channel_id  INTEGER,
            threshold   INTEGER DEFAULT 3,
            emoji       TEXT    DEFAULT '⭐',
            self_star   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS starboard_entries (
            guild_id        INTEGER,
            original_msg_id INTEGER PRIMARY KEY,
            star_msg_id     INTEGER,
            star_count      INTEGER DEFAULT 0
        );

        -- ── Counters ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS counters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            counter_type TEXT,   -- members / bots / humans / online / channels / roles / boosts
            format      TEXT,
            UNIQUE(guild_id, channel_id)
        );

        -- ── Bump reminder ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS bump_config (
            guild_id        INTEGER PRIMARY KEY,
            channel_id      INTEGER,
            role_id         INTEGER,
            message         TEXT,
            last_bump       INTEGER,
            reminder_sent   INTEGER DEFAULT 0
        );

        -- ── Auto-messages (timers) ────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS auto_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            message     TEXT,
            interval    INTEGER,  -- seconds
            last_sent   INTEGER
        );

        -- ── Reaction triggers ─────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS reaction_triggers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            trigger     TEXT,
            emoji       TEXT,
            not_strict  INTEGER DEFAULT 0,
            UNIQUE(guild_id, trigger)
        );

        -- ── Vanity roles ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS vanity_roles (
            guild_id    INTEGER,
            role_id     INTEGER,
            keyword     TEXT,
            PRIMARY KEY (guild_id, keyword)
        );

        -- ── Booster roles ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS booster_roles (
            guild_id    INTEGER,
            user_id     INTEGER,
            role_id     INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        -- ── VoiceMaster ───────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS voicemaster_config (
            guild_id        INTEGER PRIMARY KEY,
            hub_channel_id  INTEGER,
            interface_channel_id INTEGER,
            category_id     INTEGER,
            default_name    TEXT    DEFAULT '{user.display_name}''s vc',
            default_bitrate INTEGER DEFAULT 64,
            default_region  TEXT,
            join_role_id    INTEGER
        );

        CREATE TABLE IF NOT EXISTS temp_channels (
            channel_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER,
            owner_id    INTEGER,
            created_at  INTEGER
        );

        -- ── Tickets ───────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS ticket_panels (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER,
            name            TEXT,
            channel_id      INTEGER,
            panel_type      TEXT DEFAULT 'buttons',  -- buttons / dropdown
            message_id      INTEGER,
            support_role_id INTEGER,
            category_id     INTEGER,
            log_channel_id  INTEGER,
            UNIQUE(guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS ticket_options (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id        INTEGER,
            label           TEXT,
            emoji           TEXT,
            description     TEXT,
            category_id     INTEGER,
            close_category_id INTEGER,
            greeting        TEXT,
            required_role_id INTEGER,
            auto_close_hours INTEGER,
            active          INTEGER DEFAULT 1,
            FOREIGN KEY(panel_id) REFERENCES ticket_panels(id)
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER,
            channel_id      INTEGER UNIQUE,
            user_id         INTEGER,
            panel_id        INTEGER,
            option_id       INTEGER,
            status          TEXT DEFAULT 'open',  -- open / closed / deleted
            claimed_by      INTEGER,
            opened_at       INTEGER,
            closed_at       INTEGER
        );

        CREATE TABLE IF NOT EXISTS ticket_forms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            name        TEXT,
            UNIQUE(guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS ticket_form_fields (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            form_id     INTEGER,
            label       TEXT,
            placeholder TEXT,
            required    INTEGER DEFAULT 1,
            style       INTEGER DEFAULT 1,  -- 1=short, 2=long
            FOREIGN KEY(form_id) REFERENCES ticket_forms(id)
        );

        -- ── Leveling ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS leveling_config (
            guild_id        INTEGER PRIMARY KEY,
            enabled         INTEGER DEFAULT 0,
            xp_min          INTEGER DEFAULT 15,
            xp_max          INTEGER DEFAULT 25,
            xp_cooldown     INTEGER DEFAULT 60,
            xp_rate         REAL    DEFAULT 1.0,
            stack_roles     INTEGER DEFAULT 1,
            message_mode    TEXT    DEFAULT 'context',
            message_channel INTEGER,
            level_message   TEXT
        );

        CREATE TABLE IF NOT EXISTS user_xp (
            guild_id    INTEGER,
            user_id     INTEGER,
            xp          INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 0,
            last_xp     INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS level_rewards (
            guild_id INTEGER,
            level    INTEGER,
            role_id  INTEGER,
            PRIMARY KEY (guild_id, level)
        );

        CREATE TABLE IF NOT EXISTS level_ignore (
            guild_id    INTEGER,
            target_id   INTEGER,
            target_type TEXT,   -- channel / role
            PRIMARY KEY (guild_id, target_id)
        );

        -- ── Giveaways ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS giveaways (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER,
            channel_id      INTEGER,
            message_id      INTEGER,
            host_id         INTEGER,
            prize           TEXT,
            description     TEXT,
            thumbnail       TEXT,
            image           TEXT,
            winners         INTEGER DEFAULT 1,
            ends_at         INTEGER,
            ended           INTEGER DEFAULT 0,
            required_roles  TEXT,   -- JSON list of role IDs
            min_level       INTEGER,
            max_level       INTEGER
        );

        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER,
            user_id     INTEGER,
            PRIMARY KEY (giveaway_id, user_id)
        );

        -- ── Snipe / edit snipe ────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS snipe_cache (
            channel_id  INTEGER PRIMARY KEY,
            user_id     INTEGER,
            content     TEXT,
            attachment  TEXT,
            deleted_at  INTEGER
        );

        CREATE TABLE IF NOT EXISTS edit_snipe_cache (
            channel_id  INTEGER PRIMARY KEY,
            user_id     INTEGER,
            before      TEXT,
            after       TEXT,
            edited_at   INTEGER
        );

        -- ── Polls ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS polls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            message_id  INTEGER,
            question    TEXT,
            options     TEXT,   -- JSON list of option strings
            ends_at     INTEGER,
            ended       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id     INTEGER,
            user_id     INTEGER,
            option_idx  INTEGER,
            PRIMARY KEY (poll_id, user_id)
        );

        -- ── Reminders ─────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            guild_id    INTEGER,
            channel_id  INTEGER,
            message     TEXT,
            remind_at   INTEGER
        );
        """)

    # ── Cog loading ───────────────────────────────────────────────────────────
    async def setup_hook(self):
        await self.setup_db()
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.error(f"Failed to load cog {cog}: {e}", exc_info=True)

    # ── Events ────────────────────────────────────────────────────────────────
    async def on_ready(self):
        log.info(f"Logged in as {self.user} ({self.user.id})")
        log.info(f"Serving {len(self.guilds)} guild(s)")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} servers | /help"
            )
        )

    async def on_guild_join(self, guild: discord.Guild):
        log.info(f"Joined guild: {guild.name} ({guild.id})")
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)",
            (guild.id,)
        )
        await self.db.commit()

    async def on_guild_remove(self, guild: discord.Guild):
        log.info(f"Left guild: {guild.name} ({guild.id})")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                embed=error_embed("You don't have permission to use this command."),
                ephemeral=True,
            )
        elif isinstance(error, app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                embed=error_embed(f"I'm missing permissions: `{', '.join(error.missing_permissions)}`"),
                ephemeral=True,
            )
        elif isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                embed=error_embed(f"Slow down! Try again in **{error.retry_after:.1f}s**."),
                ephemeral=True,
            )
        elif isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                embed=error_embed("You don't have access to this command."),
                ephemeral=True,
            )
        else:
            log.error(f"Unhandled app command error: {error}", exc_info=True)
            try:
                await interaction.response.send_message(
                    embed=error_embed("An unexpected error occurred."),
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                pass

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()


# ── Embed helpers (shared across all cogs) ─────────────────────────────────────
def success_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"✅ {description}", color=0x2ecc71)
    if title:
        e.title = title
    return e

def error_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"❌ {description}", color=0xe74c3c)
    if title:
        e.title = title
    return e

def info_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=description, color=0x5865F2)
    if title:
        e.title = title
    return e

def warn_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=f"⚠️ {description}", color=0xf39c12)
    if title:
        e.title = title
    return e


# ── Owner-only sync command (hybrid for emergency use) ─────────────────────────
bot = ModBot()

@bot.command(name="sync", hidden=True)
@commands.is_owner()
async def sync_commands(ctx, guild_id: int = None):
    """Owner-only: sync slash commands globally or to a specific guild."""
    if guild_id:
        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        await ctx.send(f"Synced {len(synced)} commands to guild `{guild_id}`.")
    else:
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} global commands.")
    log.info(f"Synced {len(synced)} commands.")

@bot.command(name="reload", hidden=True)
@commands.is_owner()
async def reload_cog(ctx, cog: str):
    """Owner-only: reload a specific cog without restarting."""
    try:
        await bot.reload_extension(f"cogs.{cog}")
        await ctx.send(f"✅ Reloaded `cogs.{cog}`.")
    except Exception as e:
        await ctx.send(f"❌ {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
