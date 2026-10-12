# Discord Message Leaderboard Bot

A small, self-hosted Discord bot that keeps four automatically updating
message-count leaderboards in one channel: **Daily**, **Weekly**, **Monthly** and
**All-Time**. At each reset the winner shows up in each board's "Previous Member of the Day /
Week / Month" field (nothing separate is posted) and a configurable reward role moves to them.

It is built for one community server, runs anywhere Python runs, and stores
everything in a single SQLite file.

- Python 3.11+, [discord.py 2.7.1](https://pypi.org/project/discord.py/2.7.1/), aiosqlite, SQLite
- Only the non-privileged `guilds` and `guild_messages` gateway intents
- No web dashboard, XP, economy or other extras

---

## Contents

1. [Features](#features)
2. [How counting and ranking work](#how-counting-and-ranking-work)
3. [Installing Python and dependencies](#installing-python-and-dependencies)
4. [Discord Developer Portal setup](#discord-developer-portal-setup)
5. [Configuration](#configuration)
6. [Running locally](#running-locally)
7. [Running in Docker](#running-in-docker)
8. [Running under systemd](#running-under-systemd)
9. [Persistent storage and backups](#persistent-storage-and-backups)
10. [Slash commands](#slash-commands)
11. [Logging and troubleshooting](#logging-and-troubleshooting)
12. [Reliability design](#reliability-design)
13. [Known limitations](#known-limitations)
14. [Running the tests](#running-the-tests)
15. [Manual test checklist](#manual-test-checklist)

---

## Features

- **Four boards** posted as four messages in one channel, in this order: Daily, Weekly, Monthly, All-Time.
  Each board shows the top 10 (rank, mention, plain-text name, message count; medals for the top
  three), the UTC day, week or month it is counting, the previous period's winner (not on All-Time),
  the next reset as a Discord relative and absolute timestamp (not on All-Time), an optional banner
  image and a "last updated" timestamp.
- **Previous winner** fields are labelled *Previous Member of the Day / Week / Month*. This is where
  winners are shown: there is no separate winner post. They always
  describe the period immediately before the current one, with its key: its winner and recorded
  count, "No eligible winner" if nobody qualified, or "Tallying results…" while that period's reset
  is still being decided (grace period, retries, catch-up after downtime). An older period's winner
  is never shown in its place, and `/forcereset` test runs are never used.
- **Show 11–100** button on every board: an ephemeral, paginated view of ranks 11–100 (15 per page,
  Prev/Next). The ranking is recomputed on every click, and the buttons keep working after restarts.
- **Automatic resets** at UTC boundaries (daily at 00:00, weekly on Monday 00:00 using ISO weeks,
  monthly on the 1st), each after a short grace period.
- **No winner announcements.** Resets don't post anything; the boards are edited in place right after
  a reset so the Previous field shows the new winner, and nobody is pinged.
- **Reward roles**, one per period. The role is moved from the previous holder to the new winner, and
  the result is checked against the actual Discord roles.
- **Missed-reset catch-up**: if the bot was offline across one or more boundaries, every missed period
  is recorded with its correct historical winner, but only the latest of each type is given the role.
- **Crash safety**: resets and board reposts are persisted step by step and resume correctly after a
  crash or restart at any point.
- **`/stats`** shows anyone's counts and ranks, using exactly the same ordering as the boards.

## How counting and ranking work

**What counts.** Every guild message from a human (not a bot, not a webhook) whose type is `default`
or `reply`, in a channel that isn't excluded. That includes attachment-only and sticker-only
messages. Message content is never read. One message is one point, with no cooldown.

**What doesn't count.**
- DMs, bot and webhook messages, and system messages (joins, pins, boosts, thread-created notices, etc.).
- Messages in excluded channels or excluded categories.
- Messages in threads or forum posts whose parent channel or category is excluded.

Edits never add points, and deleting a message does not remove its point.

**Timestamps.** Messages are bucketed by their own creation time (`message.created_at`) in UTC, not by
when the event arrives. A message sent at 23:59:59 UTC counts for that day even if it is processed
after midnight.

**Ranking.** Higher total first. On a tie, the user who reached that total first ranks higher. If two
users reached it in the same millisecond, the lower Discord user ID ranks higher.

Because every counted message adds exactly one point and points are never removed, the message that
brought a user to their final total for a period is their latest counted message *inside that
period*. Period boundaries are UTC midnights, so the per-day buckets
(`daily_counts.last_message_id`) cover each period exactly. The tie-break therefore uses the largest
counted message snowflake within the period's own days.

Messages from after the period, uncounted messages, and the member's global "last seen" time are
never used. The comparison uses the snowflake's millisecond timestamp (`id >> 22`), so a
same-millisecond tie falls through to the user-ID rule.

`tests/test_ranking.py` checks this against an independent brute-force replay over randomised
message streams.

**Departed members** stay on the live boards with their last-known name and a "(left)" label. Their
counts are never deleted. They are skipped when choosing a reward winner: the bot walks down the
ranking to the first person confirmed to still be in the server. A temporary API error never
disqualifies anyone; the reset just retries later.

If the frozen winner leaves between being chosen and receiving the role, the role is removed from
the previous holder and stays unassigned until the next reset. The winner is never silently swapped
for someone else.

## Installing Python and dependencies

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) or your package
manager. Then:

```bash
git clone <this repository> discord-leaderboard
cd discord-leaderboard
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # runtime only
pip install -r requirements-dev.txt  # runtime + pytest (for development)
```

All direct dependencies are pinned. discord.py 2.7.1 was chosen because it provides `DynamicItem`
(added in 2.4), `Client.add_dynamic_items`, application commands and partial-message edits with
views, all of which the bot relies on.

## Discord Developer Portal setup

1. Open <https://discord.com/developers/applications> and click **New Application**. Give it a name.
2. Go to **Bot**:
   - Click **Reset Token** and copy the token. It goes in `DISCORD_TOKEN`. Treat it like a password.
   - Under **Privileged Gateway Intents**, leave **Presence**, **Server Members** and **Message Content**
     all **off**. The bot uses only the non-privileged `guilds` and `guild_messages` intents.
3. Go to **OAuth2 → URL Generator**:
   - Scopes: **`bot`** and **`applications.commands`**.
   - Bot permissions: **Administrator** (as planned for this server). If you prefer least privilege, grant
     **View Channels, Send Messages, Embed Links, Read Message History, Manage Roles** instead.
   - Open the generated URL and add the bot to your server. The URL looks like:
     ```
     https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=8
     ```
     (`permissions=8` is Administrator; the least-privilege set above is `permissions=268520448`.)

### Administrator does not bypass role hierarchy

Even with Administrator, a bot can only assign or remove roles that sit **below its own highest
role**. In **Server Settings → Roles**, drag the bot's role **above** the three reward roles (daily,
weekly, monthly). Otherwise role changes fail with "Missing Permissions".

The bot handles that gracefully: the error is logged and recorded on the reset, the reset retries
with backoff, and nothing crashes. `/setrole` warns you immediately if it can't manage the role.

## Configuration

Settings come from environment variables. A `.env` file in the working directory is loaded
automatically, and real environment variables take precedence over it.

```bash
cp .env.example .env
# edit .env and set DISCORD_TOKEN
```

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | — | Bot token. Never logged. |
| `DB_PATH` | no | `data/leaderboard.db` | SQLite database file. Parent directories are created. Use a persistent volume path in production. |
| `FLUSH_INTERVAL_SECONDS` | no | `5` | How often buffered counts are written to SQLite (0.5–300). |
| `RESET_GRACE_SECONDS` | no | `120` | Wait after each boundary before computing the winner, so late events can still land (0–3600). |
| `BOARD_UPDATE_SECONDS` | no | `300` | How often the boards are edited (60–86400). |
| `LOG_DIR` | no | `logs` | Directory for the rotating log file `leaderboard.log`. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Applies to discord.py's own log lines too. |

Configuration is validated at startup. Any missing or invalid value stops the bot immediately with
a message listing every problem (exit code 2). This includes storage: the database and log
directories are created and checked for write access before anything else happens.

**Database location.** The default is `data/leaderboard.db` relative to the working directory.
Docker sets `DB_PATH=/data/leaderboard.db` (the `/data` volume). The systemd unit uses
`/var/lib/discord-leaderboard/leaderboard.db`.

## Running locally

```bash
source .venv/bin/activate
python -m bot.main
```

On first start the bot creates the database, syncs its slash commands, and logs
`Connected as ...`. Then, in your server:

1. `/setup channel:#leaderboards` posts the four boards. A dedicated, read-only channel is recommended.
2. `/setrole period:daily role:@Member of the Day`, and the same for `weekly` and `monthly`.
3. Optionally `/setbanner set url:https://...`, and `/exclude add target:#bot-spam` for channels that
   shouldn't count.

Stop it with Ctrl+C. It flushes buffered counts and closes the database before exiting.

## Running in Docker

```bash
docker build -t leaderboard-bot .
docker volume create leaderboard-data
docker run -d --name leaderboard-bot \
  --restart unless-stopped \
  --stop-timeout 30 \
  --env-file .env \
  -v leaderboard-data:/data \
  leaderboard-bot
```

- `/data` holds the database and logs. Keep it on a persistent volume.
- `--restart unless-stopped` restarts the bot after crashes and host reboots.
- `--stop-timeout 30` gives the graceful shutdown (final flush) time to finish. The default is only 10s.
- **Volume ownership is handled for you.** The container starts as root only long enough for its
  entrypoint to take ownership of `/data`. That covers root-owned provider volumes and bind mounts.
  It then runs the bot as the unprivileged `bot` user (UID 10001).
- If your platform forces a non-root user (for example `--user` or OpenShift), the volume must already be
  writable by that user. Otherwise the bot exits with a clear "Storage is not usable" error (exit code 2)
  instead of crash-looping.

Docker Compose equivalent:

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    stop_grace_period: 30s
    env_file: .env
    volumes:
      - leaderboard-data:/data
volumes:
  leaderboard-data:
```

Most hosting providers that run a Dockerfile can use the image as is: set `DISCORD_TOKEN` in their
environment settings and attach a persistent volume at `/data`. If their volume is mounted somewhere
else, set both `DB_PATH` and `LOG_DIR` inside it. The automatic ownership fix only covers `/data`, so
that location must be writable by UID 10001.

If Docker Hub rate-limits your builds, build from a mirror of the same official image:
`docker build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim .`

## Running under systemd

```bash
sudo useradd --system --home /opt/discord-leaderboard --shell /usr/sbin/nologin leaderboard
sudo mkdir -p /opt/discord-leaderboard
sudo cp -r bot requirements.txt .env /opt/discord-leaderboard/
cd /opt/discord-leaderboard
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R leaderboard:leaderboard /opt/discord-leaderboard
sudo chmod 600 /opt/discord-leaderboard/.env
sudo cp /path/to/repo/leaderboard-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leaderboard-bot
journalctl -u leaderboard-bot -f
```

The unit file assumes `/opt/discord-leaderboard`. Edit `WorkingDirectory`, `EnvironmentFile` and
`ExecStart` if you install elsewhere.

What the unit does:
- **Restarts automatically:** `Restart=on-failure` restarts after crashes, with `RestartSec=10`.
  It deliberately does *not* restart on exit code 2 (invalid configuration) or 3 (Discord rejected the
  token), because those need a human.
- **Stores state in systemd-managed directories:** the database goes in `/var/lib/discord-leaderboard`
  (`StateDirectory`) and logs in `/var/log/discord-leaderboard` (`LogsDirectory`).
- **Shuts down gracefully:** `systemctl stop` sends SIGTERM, and `TimeoutStopSec=45` allows the final flush.

## Persistent storage and backups

The SQLite file is the only state. Losing it loses all history, configuration and board tracking.

- **Volumes.** On container hosts, mount a persistent volume and set `DB_PATH` inside it. WAL mode
  creates `leaderboard.db-wal` and `leaderboard.db-shm` next to the database; keep them on the same volume.
- **One instance only.** Run exactly one bot process per database file (and per bot token).
- **Backups.** Take a consistent online backup with SQLite's backup API. It is safe while the bot is
  running:
  ```bash
  sqlite3 /data/leaderboard.db ".backup '/backups/leaderboard-$(date +%F).db'"
  # or, without the sqlite3 CLI:
  python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
      /data/leaderboard.db /backups/leaderboard.db
  ```
  Don't just `cp` the `.db` file while the bot is running (the WAL file may hold recent writes). Stop
  the bot first if you copy files. Schedule a daily backup (cron or a systemd timer), keep several
  generations, and store them off the host.
- **Restore.** Stop the bot, replace the database file (and delete any stale `-wal`/`-shm` files),
  then start it.

## Slash commands

All administrative commands require **Manage Server**. They are hidden from members without it
(`default_permissions`) and also checked at runtime. Responses are ephemeral.

| Command | What it does |
|---|---|
| `/setup channel:<text channel>` | Sets the leaderboard channel and creates the four boards if they don't exist. The first run records the installation time, which is never changed afterwards. Safe to rerun: existing boards are reused, and changing the channel moves the boards there. |
| `/exclude add target:<channel or category>` | Stops counting messages there, including in its threads and forum posts. |
| `/exclude remove target:<channel or category>` | Starts counting there again. |
| `/exclude list` | Lists the exclusions. Exclusions of channels that have since been deleted are cleaned up automatically. |
| `/setrole period:<daily\|weekly\|monthly> role:<role>` | Sets that period's reward role. Rejects `@everyone` and integration-managed roles, and warns if the bot can't manage the role (hierarchy or permissions). |
| `/setbanner set url:<https url>` | Sets the banner image. Only `https://` URLs on public hosts are accepted. The URL is never fetched by the bot. |
| `/setbanner clear` | Removes the banner. |
| `/forcereset period:<daily\|weekly\|monthly>` | **Testing helper.** Runs the real reset state machine on a synthetic period, from the start of the current period until now. Its key looks like `test-2026-03-10-20260310T180000123456`, so it can never collide with a scheduled reset. Nothing is posted, and test runs never appear in the boards' Previous field (that only shows real periods). It does move the real reward role (the next scheduled reset reconciles the role normally). Use it on a test server. |
| `/stats [user]` | Anyone can use it. Shows Daily, Weekly, Monthly and All-Time counts and ranks for you or the given user, ephemerally. |

`/setbanner` is a command group (`set` and `clear`) because Discord does not allow one command to
have both a `url` option and a `clear` subcommand.

## Logging and troubleshooting

Logs go to the console and to a rotating file `LOG_DIR/leaderboard.log` (5 MB × 6 files). Entries
include the guild ID, the reset run (`run#12 guild=… daily:2026-03-10`), state transitions, retry
attempts and the error class (`not_found`, `forbidden`, `rate_limited`, `transient_http`, `network`,
`bad_request`).

Individual messages are not logged. The token is never logged; a redaction filter scrubs it from any
log record as a second layer of protection.

| Symptom | Likely cause and fix |
|---|---|
| Bot exits immediately with "Configuration error" | Fix the listed variables in `.env`, or the storage path or permissions it names (exit code 2). |
| "Discord rejected the token" | `DISCORD_TOKEN` is wrong or was reset (exit code 3). |
| Slash commands don't appear | Make sure the invite URL had `applications.commands`. Commands sync on startup; restart Discord (Ctrl+R). |
| Role errors: `forbidden: role ... is not below the bot's highest role` | Move the bot's role above the reward roles. The reset retries automatically (backoff up to 30 min). |
| `Missing permission to edit board ...` | The bot lost access to the leaderboard channel. Check the channel overrides. |
| A board message was deleted | It is reposted automatically at the next update (only that board, which ends up at the bottom). |
| Counts seem a few seconds behind | Expected: counts are buffered and flushed every `FLUSH_INTERVAL_SECONDS`. |
| A reset seems stuck | Check its `last_error` in the logs. Failing steps retry with backoff, and nothing is marked done until it really is. |

To inspect state directly: `sqlite3 data/leaderboard.db "SELECT id, period_type, period_key, status, winner_user_id, attempts, last_error FROM reset_runs ORDER BY id DESC LIMIT 20;"`

## Reliability design

**Buffered counting.**
- Counts are kept in memory per (guild, user, UTC day) and written in one SQLite transaction every few
  seconds, using an atomic `count = count + excluded.count` upsert that also keeps the maximum message ID.
- A failed flush rolls back, merges its snapshot back into the live buffer (counts added, maximum
  message ID kept, nothing lost from messages that arrived meanwhile), and retries with bounded
  exponential backoff.
- The duplicate check and the increment run without yielding to the event loop, so two concurrent
  deliveries of the same message can't both count.

**Reset state machine.** Each reset is a `reset_runs` row, unique per (guild, period type, period key),
with the exact period boundaries stored. It moves through
`PENDING → COMPUTED → ANNOUNCED → ROLES_DONE → DONE`, or ends in `SKIPPED_EMPTY` or
`ROLES_SUPERSEDED`. Every transition is persisted.
- **PENDING:** before computing a winner, the bot forces a buffer flush. If the flush fails, the run waits.
- **COMPUTED:** once a winner is computed it is frozen.
- **ANNOUNCED:** the announcement step always resolves silently (the state name is kept for database
  compatibility). Upgrade safety: if an older version crashed mid-announce, the channel is scanned once
  for that announcement's hidden marker `reset:{guild}:{type}:{key}` and the message is adopted; if
  the scan fails, the step still resolves silently instead of blocking.
- **ROLES_DONE:** role changes are idempotent (add if missing, remove if present). They are verified
  against the members' real Discord roles before `role_holders` is updated.
- **Failures:** Forbidden, hierarchy, rate-limit and network errors increment `attempts`, record
  `last_error` and retry with bounded backoff, without ever marking the step done.

**Catch-up rules.**
- Missed periods are processed oldest first.
- The role goes to the latest *eligible* winner: an empty latest period leaves the role with the
  previous winner.
- Older runs that never started role work become `ROLES_SUPERSEDED`. A run whose role work was
  interrupted is always finished first, never skipped.
- The boards are edited in place once the latest results are in; resets never repost them.

**Duplicate-safe sends.** Every message is created with a random nonce that Discord enforces. If
discord.py internally retries a request after a lost response (5xx or connection reset), Discord
returns the message it already created instead of making a second one.

**Board reposts.** New boards are posted with a fresh generation marker `board:{guild}:{type}:{gen}`
in the link of the embed's author line (boards posted by older versions carry it in the footer; both
are recognised), and each new message ID is saved immediately. After a crash, the channel is scanned for
that generation's markers and any found are adopted. Old boards are deleted only after all four
replacements exist, so the channel always has at least one working set of boards. Before finishing,
untracked bot board messages from the same or older generations are deleted, so a rerun converges to
exactly four.

**Persistent buttons.** The buttons are `DynamicItem`s matched by custom ID (`lb:more:{board}`,
`lb:pg:{board}:{page}`) and registered at startup, so buttons on old messages keep working after
restarts.

## Known limitations

- **Hard kills lose buffered counts.** A `kill -9`, OOM kill, power loss or forced container stop loses
  the counts still in memory: normally at most about `FLUSH_INTERVAL_SECONDS` (≈5 s) of messages. It can
  be more if the database had been failing and flushes were backing off. A graceful stop
  (SIGTERM/SIGINT) flushes everything first. Zero data loss is not claimed.
- **No backfill.** Messages sent while the bot is offline are never seen, because Discord does not
  deliver them later, and they are not counted.
- **No exactly-once guarantee.** Duplicate deliveries are suppressed with an in-memory cache of the
  last ~10,000 message IDs. That covers gateway resumes within one process, but the cache does not
  survive restarts, so exactly-once counting across restarts is not guaranteed. No permanent table of
  message IDs is kept.
- **Late messages.** A message that arrives after its reset's grace period still counts toward that
  day's history and the all-time board, but the frozen winner for that period does not change.
- **Departures are detected late.** Without the privileged Members intent, a departure is only
  confirmed when the API answers "Unknown Member": during winner selection, role changes, and a
  rate-limited check of users shown in the top 10s (each re-checked at most every 6 hours). A "(left)"
  label can therefore lag. Display names update when the member next sends a counted message.
- **Manual role changes are not fully visible.** The bot tracks who it gave each role to. If an admin
  manually gives a reward role to someone else, the bot cannot see it (listing role members needs the
  Members intent) and will not remove it.
- **Changing a reward role with `/setrole`** does not strip the old role from its previous holder,
  because it might be a role used for other things. Remove it manually if needed.
- **A recreated board lands at the bottom.** When a single board message is deleted by hand, only that
  board is reposted, below the others, and it stays there (resets no longer repost the boards). Moving
  the leaderboard with `/setup` reposts all four in order.
- **Recovery scans are bounded.** They read up to 1,000 messages after a repost (or a legacy
  announcement attempt) started. Untracked duplicate boards are also swept from the newest 200 messages when a repost finishes.
  In an extremely busy channel a crash could still leave one duplicate message. Use a dedicated,
  read-only leaderboard channel.
- **Ephemeral "Show 11–100" pages** only work while Discord still shows the ephemeral message. Clicks on
  expired pages are ignored gracefully.
- **`/forcereset` is not a dry run.** It posts nothing, but it moves the real reward role.
  Use a private test server for experiments. A test run that cannot be computed within a few minutes
  (for example because Discord is unreachable) is closed without a winner, because the day's counts
  would by then include messages sent after the test period.
- **One process per database.** SQLite and the in-memory buffer assume exactly one running instance.
- **All times are UTC.** Boundaries are not configurable per time zone.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite needs no Discord token or network. It uses real temporary SQLite databases, a controllable
clock, and an in-memory fake of the Discord API layer (`tests/fakes.py`) that can inject NotFound,
Forbidden, rate-limit, 5xx and network errors, as well as simulated crashes before or after any
Discord operation. Restarts are simulated with fresh application instances over the same database.

Highlights:
- **`test_resets.py`** crashes the reset flow at every Discord mutation (before and after it takes
  effect, once and twice) and checks that recovery converges to:
  - no announcement posts (an older version's interrupted post is adopted, never duplicated)
  - the frozen winner
  - the right role holder
  - four correctly ordered boards
  - no duplicate runs
- **`test_ranking.py`** checks the tie-break against an independent brute-force replay.
- **`test_interactions.py`** dispatches button clicks through discord.py's real `ViewStore` on a
  freshly constructed client, as after a restart.

The suite covers the logic. It doesn't replace testing against Discord itself, below.

## Manual test checklist

Use a **private test server** for anything destructive (forced resets, broken role hierarchy, killing
the bot mid-reset). Don't run failure experiments in the production community.

1. **Setup:** configure `.env`, start the bot, and run `/setup channel:#leaderboards`.
2. **Board order:** confirm four boards appear in order (Daily, Weekly, Monthly, All-Time), each with a
   "Show 11–100" button.
3. **Counting:** send eligible messages (text, replies, an image with no text, a sticker). Within
   `BOARD_UPDATE_SECONDS` the counts increase. `/stats` reflects them within a few seconds.
4. **Exclusions:** `/exclude add` a channel, a category, and a forum. Messages there, and in threads and
   forum posts under them, do not count. Bot and webhook messages never count.
5. **Pagination:** with more than 10 active users, click **Show 11–100** and step through Prev/Next.
   Prev is disabled on the first page and Next on the last.
6. **Persistent buttons:** restart the bot, then click **Show 11–100** on an existing board. It still works.
7. **Single-board recovery:** delete one board message by hand. At the next update only that board is
   reposted.
8. **`/stats`:** try it for yourself, for another member, and for someone with zero messages. Ranks
   match the boards.
9. **Admin commands:** test `/exclude add/remove/list`, `/setrole` for all three periods, and
   `/setbanner set` and `/setbanner clear`. Invalid banner URLs (`http://…`, `https://localhost/…`) are
   rejected.
10. **Role hierarchy failure:** move a reward role above the bot's role, then run `/forcereset` for that
    period. The reset reports it didn't finish, the logs show a `forbidden` error, the bot keeps running
    and the boards keep updating. Fix the hierarchy and the retry succeeds.
11. **Forced reset:** run `/forcereset period:daily`. Check that nothing was posted in the channel, that
    the role moved to the current leader, and the command's success message. The Previous field does
    *not* change (test runs are never shown there).
12. **Real reset:** after a real UTC midnight (plus the grace period), check that nothing was posted,
    the four boards are the same messages, the daily board's Previous field shows the winner, and the
    role moved.
13. **Crash during a reset:** wait for a real boundary (or start `/forcereset`), stop the bot abruptly
    mid-way (`docker kill` / `kill -9`), restart it, and confirm it finishes: no post, the correct role
    holder, four boards.
14. **Logs:** read `LOG_DIR/leaderboard.log`. Failures name the guild, the run and the error class, and
    the token never appears.
15. **Graceful shutdown:** send messages, stop the bot gracefully within a few seconds (`docker stop` /
    `systemctl stop` / Ctrl+C), restart, and confirm no counts were lost.
