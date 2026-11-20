# AjBot

A self-hosted Discord bot that keeps four message-count leaderboards (Daily, Weekly, Monthly,
All-Time) in one channel, hands a reward role to each period's winner, and has a set of
moderation tools. Everything is stored in one SQLite file.

Python 3.11+, discord.py 2.7.1, aiosqlite. Only the non-privileged `guilds` and `guild_messages`
intents are used.

## Setup

1. Create an app at <https://discord.com/developers/applications>, go to **Bot**, reset and copy
   the token. Leave all privileged intents off.
2. Invite it with the `bot` and `applications.commands` scopes and Administrator
   (`permissions=8`), or least privilege (`permissions=1099780156422`).
3. In **Server Settings → Roles**, drag the bot's role above the reward roles and above everyone
   it should moderate. Administrator does not bypass this.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then set DISCORD_TOKEN
python -m bot.main
```

Or with Docker (keep `/data` on a persistent volume, it holds the database and logs):

```bash
docker build -t leaderboard-bot .
docker run -d --name leaderboard-bot --restart unless-stopped --stop-timeout 30 \
  --env-file .env -v leaderboard-data:/data leaderboard-bot
```

A systemd unit is included in `leaderboard-bot.service` (assumes `/opt/discord-leaderboard`).

Then in your server:

1. `/setup channel:#leaderboards` to post the boards.
2. `/setrole period:daily role:@...` (and `weekly`, `monthly`) for the reward roles.

## Config

Set in `.env` (see `.env.example`). Only `DISCORD_TOKEN` is required.

| Variable | Default |
|---|---|
| `DISCORD_TOKEN` | required |
| `DB_PATH` | `data/leaderboard.db` |
| `FLUSH_INTERVAL_SECONDS` | `5` |
| `RESET_GRACE_SECONDS` | `120` |
| `BOARD_UPDATE_SECONDS` | `300` |
| `LOG_DIR` | `logs` |
| `LOG_LEVEL` | `INFO` |
| `STAFF_ROLE_IDS` | the server's 3 staff roles (`off` disables the role gate) |

## Commands

Every command except `/stats` needs one of the staff roles (the server owner always can).

**Leaderboard:** `/setup`, `/setrole`, `/setbanner set|clear`, `/exclude add|remove|list`,
`/forcereset` (testing), `/winnerdm on|off`, `/lbban`, `/lbunban`, `/lbbans`, `/resetcounts`,
`/countcooldown`, `/stats`

**Moderation:** `/timeout`, `/untimeout`, `/mute`, `/unmute`, `/warn`, `/warnings`, `/clearwarn`,
`/clearwarns`, `/kick`, `/ban` (optional `duration` for a tempban), `/unban`, `/purge`,
`/warnconfig`, `/case view|reason`, `/modlog set|clear`. Right-click menus: Warn, Timeout,
Delete + warn.

Resets happen at UTC midnight (weekly on Monday, monthly on the 1st). Winners show up in each
board's "Previous" field; nothing is announced.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No Discord token or network needed.

## Backups

The SQLite file is the only state. Back it up while running with:

```bash
sqlite3 /data/leaderboard.db ".backup '/backups/leaderboard.db'"
```
