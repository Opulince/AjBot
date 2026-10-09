-- Migration 1: initial schema.
--
-- Conventions:
--   * Every Discord snowflake is stored as INTEGER (signed 64-bit).
--   * Timestamps are TEXT in the canonical ISO-8601 UTC format produced by
--     bot.clock.to_iso(), which sorts lexicographically in time order.
--   * Day keys are canonical UTC dates, 'YYYY-MM-DD'.

CREATE TABLE guild_config (
    guild_id          INTEGER PRIMARY KEY,
    board_channel_id  INTEGER,
    banner_url        TEXT,
    role_daily_id     INTEGER,
    role_weekly_id    INTEGER,
    role_monthly_id   INTEGER,
    installed_at      TEXT,
    -- Set (atomically with an announcement) when the boards must be reposted
    -- below a new announcement; survives crashes so the repost is not lost.
    repost_needed     INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE excluded_channels (
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,   -- a channel, forum, or category ID
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
) WITHOUT ROWID;

CREATE TABLE daily_counts (
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    day              TEXT    NOT NULL CHECK (length(day) = 10),
    count            INTEGER NOT NULL CHECK (count >= 0),
    -- Largest counted message snowflake in this bucket. Because counts only
    -- ever grow by one per counted message, the maximum over a period's buckets
    -- is the message that brought the user to their final period total.
    last_message_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, day)
) WITHOUT ROWID;

-- Range scans for daily/weekly/monthly aggregation (covering index).
CREATE INDEX idx_daily_counts_guild_day
    ON daily_counts (guild_id, day, user_id, count, last_message_id);

CREATE TABLE members (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    display_name  TEXT    NOT NULL,
    username      TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    left_at       TEXT,
    PRIMARY KEY (guild_id, user_id)
) WITHOUT ROWID;

CREATE TABLE board_messages (
    guild_id    INTEGER NOT NULL,
    board_type  TEXT    NOT NULL CHECK (board_type IN ('daily', 'weekly', 'monthly', 'alltime')),
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    generation  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, board_type)
) WITHOUT ROWID;

CREATE TABLE board_repost (
    guild_id      INTEGER NOT NULL,
    generation    INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,           -- channel receiving the new boards
    status        TEXT    NOT NULL CHECK (status IN ('POSTING', 'DELETING', 'DONE')),
    new_ids_json  TEXT    NOT NULL DEFAULT '{}',  -- {"daily": message_id, ...}
    old_ids_json  TEXT    NOT NULL DEFAULT '{}',  -- {"daily": [channel_id, message_id], ...}
    reason        TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, generation)
);

-- At most one unfinished repost operation per guild.
CREATE UNIQUE INDEX idx_board_repost_one_active
    ON board_repost (guild_id) WHERE status <> 'DONE';

CREATE TABLE reset_runs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id                 INTEGER NOT NULL,
    period_type              TEXT    NOT NULL CHECK (period_type IN ('daily', 'weekly', 'monthly')),
    period_key               TEXT    NOT NULL,
    period_start             TEXT    NOT NULL,
    period_end               TEXT    NOT NULL,
    is_test                  INTEGER NOT NULL DEFAULT 0,
    status                   TEXT    NOT NULL CHECK (status IN (
                                 'PENDING', 'COMPUTED', 'ANNOUNCED', 'ROLES_DONE',
                                 'DONE', 'SKIPPED_EMPTY', 'ROLES_SUPERSEDED')),
    winner_user_id           INTEGER,
    winner_count             INTEGER,
    announcement_channel_id  INTEGER,
    announcement_message_id  INTEGER,
    -- Persisted before an announcement is sent: if set without a message ID the
    -- send may have succeeded before a crash, so the channel must be scanned.
    announce_started_at      TEXT,
    -- Persisted before the first role operation; a run that started role work
    -- is always finished, never silently superseded.
    role_started_at          TEXT,
    role_id                  INTEGER,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    last_error               TEXT,
    next_attempt_at          TEXT,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    UNIQUE (guild_id, period_type, period_key)
);

CREATE INDEX idx_reset_runs_guild_type_end
    ON reset_runs (guild_id, period_type, period_end);

CREATE TABLE role_holders (
    guild_id     INTEGER NOT NULL,
    period_type  TEXT    NOT NULL CHECK (period_type IN ('daily', 'weekly', 'monthly')),
    user_id      INTEGER NOT NULL,
    role_id      INTEGER NOT NULL,
    since_key    TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (guild_id, period_type)
) WITHOUT ROWID;
