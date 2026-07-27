-- sonde base schema. Migrations are applied at startup (see store.migrate).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Everything known about a person, whether they currently follow us or not.
CREATE TABLE IF NOT EXISTS actors (
    did                     TEXT PRIMARY KEY,
    handle                  TEXT,
    display_name            TEXT,
    avatar_url              TEXT,
    description             TEXT,
    account_created_at      TEXT,
    labels                  TEXT,     -- JSON array of label values
    followers_count         INTEGER,
    follows_count           INTEGER,
    posts_count             INTEGER,
    verified_status         TEXT,     -- valid / invalid / none
    trusted_verifier_status TEXT,     -- valid / invalid / none
    verifications           TEXT,     -- JSON array of verificationView
    affinity_sampled        INTEGER,
    affinity_exact          INTEGER,
    last_post_at            TEXT,
    profile_fetched_at      TEXT,
    unservable_since        TEXT,     -- requested from getProfiles, not returned
    institution_id          INTEGER,  -- best-evidence match, M6
    institution_name        TEXT,
    institution_score       REAL,
    institution_confidence  REAL,
    institution_method      TEXT,
    institution_role        TEXT,
    verified_affinity       INTEGER,
    enriched_at             TEXT,
    influence_score         REAL,
    score_components        TEXT,     -- JSON
    first_indexed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actors_score      ON actors (influence_score DESC);
CREATE INDEX IF NOT EXISTS idx_actors_followers  ON actors (followers_count DESC);
CREATE INDEX IF NOT EXISTS idx_actors_verified   ON actors (verified_status);
CREATE INDEX IF NOT EXISTS idx_actors_handle     ON actors (handle);
CREATE INDEX IF NOT EXISTS idx_actors_stale      ON actors (profile_fetched_at);

-- Whether an actor follows us, and since when.
CREATE TABLE IF NOT EXISTS follower_state (
    did           TEXT PRIMARY KEY REFERENCES actors (did) ON DELETE CASCADE,
    is_current    INTEGER NOT NULL DEFAULT 1,
    backfilled    INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    missed_sweeps INTEGER NOT NULL DEFAULT 0,
    lost_at       TEXT,
    list_rank     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_fs_current ON follower_state (is_current);
CREATE INDEX IF NOT EXISTS idx_fs_first   ON follower_state (first_seen_at DESC);

-- Append-only history. Never pruned; this is the table the backup exists for.
CREATE TABLE IF NOT EXISTS follow_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    did         TEXT NOT NULL,
    event       TEXT NOT NULL,   -- followed / departed / returned / handle_changed
    reason      TEXT,            -- unfollow / gone / unknown
    detail      TEXT,
    detected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_did  ON follow_events (did);
CREATE INDEX IF NOT EXISTS idx_events_time ON follow_events (detected_at DESC);

-- Accounts we follow, for mutual detection and as the affinity index source.
CREATE TABLE IF NOT EXISTS my_follows (
    did          TEXT PRIMARY KEY,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    day                 TEXT PRIMARY KEY,
    followers_tracked   INTEGER,
    followers_reported  INTEGER,
    verified_total      INTEGER,
    mutuals_total       INTEGER,
    gained              INTEGER,
    lost                INTEGER
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    completed         INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running',
    pages_fetched     INTEGER NOT NULL DEFAULT 0,
    actors_seen       INTEGER NOT NULL DEFAULT 0,
    new_followers     INTEGER NOT NULL DEFAULT 0,
    lost_followers    INTEGER NOT NULL DEFAULT 0,
    profiles_hydrated INTEGER NOT NULL DEFAULT 0,
    api_calls         INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON sync_runs (started_at DESC);

-- Small key/value store for counters that outlive a run.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- M6

-- Institutions are editorial: what a masthead is worth is the user's call.
-- Seeded from SEED_INSTITUTIONS plus every verification issuer seen in a sweep.
CREATE TABLE IF NOT EXISTS institutions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    weight        REAL NOT NULL DEFAULT 1.0,
    verifier_did  TEXT,
    domains       TEXT,     -- JSON array
    aliases       TEXT,     -- JSON array
    discovered_at TEXT,
    roster_fetched_at TEXT,
    notes         TEXT
);

-- Who an institution has verified — from listRecords against the verifier's repo.
CREATE TABLE IF NOT EXISTS institution_roster (
    institution_id INTEGER NOT NULL REFERENCES institutions (id) ON DELETE CASCADE,
    did            TEXT NOT NULL,
    handle         TEXT,
    created_at     TEXT,
    PRIMARY KEY (institution_id, did)
);

CREATE INDEX IF NOT EXISTS idx_roster_did ON institution_roster (did);

-- The accounts whose follow lists seed the affinity index. Kept so the sample
-- is reproducible and its drift visible.
CREATE TABLE IF NOT EXISTS affinity_sources (
    did           TEXT PRIMARY KEY,
    handle        TEXT,
    follows_count INTEGER,
    weight        REAL,      -- selectivity weight applied to each of its hits
    is_verified   INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER,
    fetched_at    TEXT
);
