# ModBot — Full-Featured Discord Moderation Bot

A modern, slash-command-only Discord moderation bot built to compete with and surpass paid bots like Bleed.

## Features

### 🛡️ Security
- **Antinuke** — Protects against mass bans, kicks, role deletions, channel deletions, emoji purges, webhook spam, unauthorized bot adds, and vanity URL changes. Configurable thresholds and punishments (ban/kick/timeout/stripstaff) per module.
- **Antiraid** — Mass join detection with auto-lockdown, avatar requirement, account age gates, per-module punishments.
- **VPN/Proxy detection** — Phishing URL lookups via live threat intel feeds.
- **Fake Permissions** — Restrict moderators to bot commands only. Grant/revoke granular permissions per role without touching Discord's native permission system.

### ⚔️ Moderation
- Full command suite: ban, softban, hardban (by ID), tempban, unban, kick, timeout, untimeout
- 3 mute types: text mute, image mute, reaction mute — all with optional durations
- Jail system (strips all roles, restricts to jail channel)
- Warn system with auto-escalation chains (warn → mute → kick → ban)
- Case management: view, edit reason, delete cases
- Purge: by count, user, bots, contains text, attachments, embeds
- Channel tools: slowmode, lock, unlock, server-wide lockdown/unlockdown
- `/setup moderation` — auto-creates mod-log, jail role, jail channel
- `/setup mute` — auto-creates Muted, Image Muted, Reaction Muted roles with channel overwrites

### 🤖 AutoMod
- **Spam** — Rolling window rate limiter
- **Caps** — Configurable percentage threshold
- **Mass mentions** — Per-message mention count limit
- **Emoji flood** — Unicode + custom emoji counting
- **Duplicate messages** — Repeated message detection
- **Link filtering** — Domain whitelist/blacklist
- **Invite filtering** — Block Discord invite links
- **Phishing** — Real-time lookup against threat intel feeds (auto-ban by default)
- **Word filter** — Exact match or regex patterns, per-rule punishment
- **Ignore lists** — Exempt channels, roles, or users from all AutoMod checks

### 📋 Logging
7 event types, each routable to different channels:
- `messages` — Deletions and edits
- `members` — Join/leave/role changes/nickname changes
- `voice` — Voice channel join/move/leave
- `roles` — Role create/delete/update
- `channels` — Channel create/delete
- `invites` — Invite create/delete
- `emojis` — Emoji add/remove

### ⚙️ Configuration
- Welcome, goodbye, and boost messages with variable engine (`{user.mention}`, `{guild.name}`, etc.)
- Embed script support (`{embed}$v{title:...}$v{description:...}`) — Bleed-compatible
- Auto-responders with flags: not_strict, self_destruct, delete_trigger, reply
- Reaction roles (emoji → role on any message)
- Button roles (click button → toggle role)
- Starboard with configurable threshold and emoji
- Stat counter channels (members, bots, humans, online, channels, roles, boosts)
- Bump reminder (Disboard integration — auto-reminds after 2h)
- Auto-messages (timed recurring messages)
- Reaction triggers (auto-react to keywords)

### 🎙️ VoiceMaster
- "Join to Create" hub channel
- Interactive control panel with buttons (lock, unlock, ghost, rename, limit, permit, reject, transfer, claim)
- Temp channel auto-cleanup when empty
- Default name template, bitrate, region configuration
- Join role (given when in any temp VC)

### 🎫 Tickets
- Up to 15 ticket panels per server
- Button-based opening
- Ticket lifecycle: open → claim → close → reopen → delete
- Transcript export (full chat history as .txt)
- Add/remove members from tickets
- Auto-close on inactivity
- Support role configuration per panel

### ⭐ Leveling
- XP gain on messages with configurable min/max and cooldown
- Bleed-compatible XP formula
- Level-up messages: in-channel, DM, specific channel, or none
- Role rewards at specific levels (with optional stacking)
- `/rank` card with progress bar
- `/leaderboard` (top 100, paginated)
- Admin: setlevel, setxp, resetxp
- Ignore channels and roles from XP gain

### 🎉 Giveaways
- Full lifecycle: start, end, cancel, reroll, edit
- Entry via button (toggle in/out)
- Required roles gate
- Min/max level gate (integrates with leveling system)
- Thumbnail and image support
- Auto-end background task

### 🔧 Utility
- Snipe and editsnipe (last deleted/edited message per channel)
- Polls (up to 5 options, button voting, timed auto-end, live recount)
- Reminders (DM on timer, up to 30 days)
- /userinfo, /serverinfo, /roleinfo
- /avatar, /banner
- /ping

---

## Setup

### Requirements
```
Python 3.11+
discord.py >= 2.3.0
aiosqlite
aiohttp
```

### Installation (Railway)
1. Create a new project on [Railway](https://railway.app)
2. Connect your GitHub repo or upload the files directly
3. In Railway → your service → **Variables**, add:
   - `MODBOT_TOKEN` = your bot token
   - `OWNER_ID` = your Discord user ID
4. Set the start command to `python main.py`
5. Deploy — Railway handles the rest

### Installation (local/VPS)
```bash
cd modbot
pip install -r requirements.txt
python main.py
```
Set `MODBOT_TOKEN` and `OWNER_ID` as environment variables in whatever way your host supports.

### First-time Discord setup
1. Invite the bot with these permissions: `Administrator` (recommended for full functionality)
2. In Discord, run `!sync` (owner only) to register all slash commands globally
   - Or `!sync <guild_id>` to sync to a specific server instantly (for testing)
3. Run `/setup moderation` in your server to create the mod-log and jail system
4. Run `/setup mute` to create the mute roles
5. Configure antinuke: `/antinuke enable`, then `/antinuke module ban on` etc.

### Environment Variables
| Variable | Required | Description |
|---|---|---|
| `MODBOT_TOKEN` | ✅ | Your bot's token from Discord Developer Portal |
| `OWNER_ID` | ✅ | Your Discord user ID (for owner-only commands) |

### Bot Permissions Required
The bot needs the following permissions in each server:
- Administrator (easiest, covers all)
- OR: Ban Members, Kick Members, Manage Roles, Manage Channels, Manage Messages, Moderate Members, View Audit Log, Manage Webhooks, Move Members

### Required Intents (enable in Discord Developer Portal)
- Server Members Intent ✅
- Message Content Intent ✅
- Presence Intent ✅

---

## File Structure
```
modbot/
├── main.py              # Bot entry point, database setup, cog loader
├── utils.py             # Shared utilities, helpers, decorators
├── requirements.txt
├── data/
│   └── modbot.db        # SQLite database (auto-created)
└── cogs/
    ├── security.py      # Antinuke, antiraid, fake permissions
    ├── moderation.py    # Full mod command suite
    ├── automod.py       # AutoMod engine
    ├── logging.py       # Event logging
    ├── configuration.py # Welcome/goodbye, autoresponders, roles, starboard
    ├── voicemaster.py   # Temp voice channels
    ├── tickets.py       # Ticket system
    ├── leveling.py      # XP and leveling
    ├── giveaways.py     # Giveaway lifecycle
    └── utility.py       # Snipe, polls, reminders, info commands
```

---

## Owner Commands (prefix: `!`)
These are emergency/setup commands, not slash commands:
- `!sync` — Sync all slash commands globally
- `!sync <guild_id>` — Sync to a specific guild (instant, use for testing)
- `!reload <cog>` — Reload a cog without restarting (e.g. `!reload moderation`)

---

## Variable System
Use these in welcome/goodbye/boost messages, autoresponders, level-up messages, and VoiceMaster channel names:

| Variable | Value |
|---|---|
| `{user}` | Full username (user#0000) |
| `{user.mention}` | @mention |
| `{user.name}` | Username only |
| `{user.display_name}` | Server nickname or username |
| `{user.id}` | User ID |
| `{user.avatar}` | Avatar URL |
| `{user.created_at}` | Account creation date |
| `{user.joined_at}` | Server join date |
| `{guild}` | Server name |
| `{guild.name}` | Server name |
| `{guild.id}` | Server ID |
| `{guild.member_count}` | Member count |
| `{guild.owner}` | Server owner username |

## Embed Script Syntax
Compatible with Bleed's embed system:
```
{embed}$v{title: My Title}$v{description: Hello {user.mention}!}$v{color: #5865F2}$v{footer: Bot Footer}
```
Supported keys: `title`, `description`, `color/colour`, `footer`, `image`, `thumbnail`, `author`, `field` (format: `name | value | inline`)
