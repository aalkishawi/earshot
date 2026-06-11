"""SQLite schema and connection helpers for Earshot."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Configured YouTube channels we monitor.
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,           -- UC... YouTube channel ID
    handle       TEXT NOT NULL,              -- @handle as written in channels.yaml
    name         TEXT,                       -- human-readable channel title
    priority     INTEGER NOT NULL DEFAULT 1, -- 1 = digest only, 2 = also instant alert
    keywords     TEXT NOT NULL DEFAULT '[]', -- JSON array of watch keywords
    active       INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL
);

-- One row per detected video. State machine drives the pipeline.
-- States: detected, transcribed, summarized, notified, skipped, failed
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL REFERENCES channels(channel_id),
    title           TEXT,
    url             TEXT,
    published_at    TEXT,
    state           TEXT NOT NULL DEFAULT 'detected',
    is_priority     INTEGER NOT NULL DEFAULT 0,
    transcript_path TEXT,
    transcript_source TEXT,                  -- 'captions' | 'auto_captions' | 'asr'
    summary_json    TEXT,                    -- JSON: { summary, takeaways, quotes }
    detected_at     TEXT NOT NULL,
    transcribed_at  TEXT,
    summarized_at   TEXT,
    notified_at     TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);

-- Persistent glossary of concepts/tools/people we've already alerted on.
CREATE TABLE IF NOT EXISTS glossary (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    term                TEXT NOT NULL UNIQUE,
    normalized_term     TEXT NOT NULL,        -- lowercased + stripped for dedup
    aliases             TEXT NOT NULL DEFAULT '[]',
    definition          TEXT,
    why_it_matters      TEXT,
    category            TEXT,                 -- concept | tool | person | paper | acronym
    first_seen_video_id TEXT REFERENCES videos(video_id),
    first_seen_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_normalized ON glossary(normalized_term);

-- AI news items collected by the scout.
CREATE TABLE IF NOT EXISTS news_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    published_at     TEXT,
    summary          TEXT,
    importance_score INTEGER,                 -- 1..5
    state            TEXT NOT NULL DEFAULT 'seen',  -- seen | notified | skipped
    seen_at          TEXT NOT NULL,
    notified_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_state ON news_items(state);

-- One row per pipeline run for observability + cost tracking.
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running', -- running | ok | failed
    videos_processed INTEGER NOT NULL DEFAULT 0,
    news_processed   INTEGER NOT NULL DEFAULT 0,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0.0,
    error            TEXT
);

-- Audit log of every alert sent (digest or instant).
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,    -- digest | instant
    channel      TEXT NOT NULL,    -- email | stdout | (future) phone
    recipient    TEXT,
    subject      TEXT,
    payload_path TEXT,             -- path to the rendered markdown/html on disk
    status       TEXT NOT NULL,    -- sent | failed | dry_run
    sent_at      TEXT NOT NULL,
    error        TEXT,
    run_id       INTEGER REFERENCES runs(id)
);

-- v2 interactive call sessions. One row per outbound interactive call.
-- Schema v2.
CREATE TABLE IF NOT EXISTS interactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid         TEXT UNIQUE,                       -- Twilio CallSid; NULL during dry-run
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    status           TEXT NOT NULL DEFAULT 'started',   -- started | completed | failed | abandoned
    -- JSON array of {kind: 'video'|'news', ref_id: str|int} — the items this call covers.
    item_refs        TEXT NOT NULL DEFAULT '[]',
    -- JSON array of turns: {turn: int, phase: str, item_ref: {...}|null,
    --                       question: str, answer_text: str|null, action: str|null}
    turns            TEXT NOT NULL DEFAULT '[]',
    -- Free-text journal capture from the closer turn (transcribed).
    journal_note     TEXT,
    duration_seconds INTEGER,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0.0,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_interactions_started ON interactions(started_at);
"""


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp suitable for SQLite TEXT columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
    current = row[0] if row else 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow_iso()),
        )


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
    return row[0] if row else None
