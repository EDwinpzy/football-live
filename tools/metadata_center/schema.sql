-- MyTV metadata centre schema (schema_version 3).
-- Identifiers are MyTV-owned: media.id is a permanent mytv_id.  Third-party
-- identifiers live in external_id so they can be replaced without touching
-- favourites, history or playback bindings.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Monotonic mytv_id allocator; the current value is the next id to hand out.
CREATE TABLE IF NOT EXISTS id_allocator (
    next_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY,
    media_type     TEXT NOT NULL,
    title          TEXT NOT NULL,
    original_title TEXT,
    year           INTEGER,
    runtime        INTEGER,
    overview       TEXT,
    status         TEXT,
    language       TEXT,
    country        TEXT,
    release_date   TEXT,
    episodes       INTEGER,
    season         INTEGER,
    popularity     REAL NOT NULL DEFAULT 0,
    created_at     INTEGER,
    updated_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_media_type_year ON media(media_type, year);
CREATE INDEX IF NOT EXISTS idx_media_popularity ON media(popularity DESC);

CREATE TABLE IF NOT EXISTS external_id (
    media_id    INTEGER NOT NULL,
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (provider, external_id)
);

CREATE INDEX IF NOT EXISTS idx_external_media ON external_id(media_id);

CREATE TABLE IF NOT EXISTS media_title (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id             INTEGER NOT NULL,
    title                TEXT NOT NULL,
    language             TEXT,
    title_type           TEXT,
    normalized           TEXT,
    pinyin               TEXT,
    pinyin_alts          TEXT,
    pinyin_initials      TEXT,
    pinyin_initials_alts TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_title_media ON media_title(media_id);
CREATE INDEX IF NOT EXISTS idx_media_title_norm ON media_title(normalized);
CREATE INDEX IF NOT EXISTS idx_media_title_pinyin ON media_title(pinyin);

CREATE TABLE IF NOT EXISTS person (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    original_name TEXT,
    profile_url   TEXT
);

CREATE INDEX IF NOT EXISTS idx_person_name ON person(name);

CREATE TABLE IF NOT EXISTS credit (
    media_id       INTEGER NOT NULL,
    person_id      INTEGER NOT NULL,
    department     TEXT NOT NULL,
    role           TEXT NOT NULL,
    character_name TEXT,
    credit_order   INTEGER,
    PRIMARY KEY (media_id, person_id, department, role)
);

CREATE INDEX IF NOT EXISTS idx_credit_person ON credit(person_id);

CREATE TABLE IF NOT EXISTS genre (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS media_genre (
    media_id INTEGER NOT NULL,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (media_id, genre_id)
);

CREATE TABLE IF NOT EXISTS region (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS media_region (
    media_id INTEGER NOT NULL,
    region_id INTEGER NOT NULL,
    PRIMARY KEY (media_id, region_id)
);

CREATE TABLE IF NOT EXISTS rating (
    media_id   INTEGER NOT NULL,
    provider   TEXT NOT NULL,
    score      REAL,
    vote_count INTEGER,
    updated_at INTEGER,
    PRIMARY KEY (media_id, provider)
);

CREATE TABLE IF NOT EXISTS image (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id     INTEGER NOT NULL,
    image_type   TEXT NOT NULL,
    original_url TEXT,
    cached_url   TEXT,
    width        INTEGER,
    height       INTEGER,
    UNIQUE (media_id, image_type)
);

-- Refresh priorities are data, not code: the update model decides which rows
-- are worth re-fetching first.
CREATE TABLE IF NOT EXISTS sync_state (
    media_id         INTEGER PRIMARY KEY,
    metadata_updated INTEGER,
    rating_updated   INTEGER,
    image_updated    INTEGER,
    priority         INTEGER NOT NULL DEFAULT 2
);

-- Flat search document: the device-side reader can reproduce the same ranking
-- without FTS5, and the server uses it for the LIKE fallback.
CREATE TABLE IF NOT EXISTS search_doc (
    media_id      INTEGER PRIMARY KEY,
    title_norm    TEXT NOT NULL,
    aliases_norm  TEXT NOT NULL,
    pinyin        TEXT NOT NULL,
    pinyin_alts   TEXT NOT NULL,
    initials      TEXT NOT NULL,
    initials_alts TEXT NOT NULL,
    people_norm   TEXT NOT NULL,
    people_pinyin TEXT NOT NULL,
    genres_norm   TEXT NOT NULL,
    popularity    REAL NOT NULL DEFAULT 0,
    year          INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    media_id UNINDEXED,
    title,
    aliases,
    pinyin,
    initials,
    people,
    genres,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3 4'
);

-- Pairs the matcher refused to merge or split automatically (score 60..80).
CREATE TABLE IF NOT EXISTS review_queue (
    left_key   TEXT NOT NULL,
    right_key  TEXT NOT NULL,
    score      REAL NOT NULL,
    reason     TEXT,
    created_at INTEGER,
    PRIMARY KEY (left_key, right_key)
);
