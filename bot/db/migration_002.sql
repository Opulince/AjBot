-- Migration 2: moderation, leaderboard bans, warn escalation, tempbans, winner DMs.
--
-- Same conventions as migration 1: snowflakes INTEGER, timestamps TEXT in the
-- canonical ISO-8601 UTC format produced by bot.clock.to_iso().

ALTER TABLE guild_config ADD COLUMN modlog_channel_id INTEGER;
-- Minimum seconds between two counted messages of one user (0 = off).
ALTER TABLE guild_config ADD COLUMN count_cooldown_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN winner_dm INTEGER NOT NULL DEFAULT 0;

-- Set when the winner DM for a run was sent (idempotency across retries).
ALTER TABLE reset_runs ADD COLUMN winner_dm_sent_at TEXT;

CREATE TABLE mod_cases (
    guild_id           INTEGER NOT NULL,
    case_id            INTEGER NOT NULL,   -- per guild: MAX + 1 inside the inserting transaction
    action             TEXT    NOT NULL CHECK (action IN (
                           'warn', 'timeout', 'untimeout', 'kick', 'ban', 'unban', 'purge',
                           'lbban', 'lbunban', 'resetcounts')),
    target_id          INTEGER,            -- NULL for actions without a user target (e.g. purge)
    moderator_id       INTEGER NOT NULL,
    reason             TEXT,
    duration_seconds   INTEGER,
    extra              TEXT,               -- JSON object with action-specific details
    -- Warnings: 0 once cleared (cases are never deleted).
    active             INTEGER NOT NULL DEFAULT 1,
    modlog_channel_id  INTEGER,            -- where the mod-log entry was posted (the channel may change later)
    modlog_message_id  INTEGER,
    created_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, case_id)
);

CREATE INDEX idx_mod_cases_target ON mod_cases (guild_id, target_id, action, active);

CREATE TABLE lb_banned_users (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    moderator_id  INTEGER NOT NULL,
    reason        TEXT,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
) WITHOUT ROWID;

CREATE TABLE warn_thresholds (
    guild_id         INTEGER NOT NULL,
    warn_count       INTEGER NOT NULL CHECK (warn_count > 0),
    timeout_seconds  INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 1 AND 2419200),  -- Discord max: 28 days
    PRIMARY KEY (guild_id, warn_count)
) WITHOUT ROWID;

CREATE TABLE scheduled_unbans (
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    unban_at         TEXT    NOT NULL,
    case_id          INTEGER,              -- the ban case that scheduled it
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    next_attempt_at  TEXT,                 -- backoff after a failed attempt
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX idx_scheduled_unbans_due ON scheduled_unbans (unban_at);
