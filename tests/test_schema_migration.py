"""Schema migration tests — v1 → v2 must be idempotent and additive."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from earshot import db as db_mod


# The v1 schema (pre-interactions). Copy-pasted from the git history of
# earshot/db.py at commit bc016fa. If the production schema grows, only
# the v1-→ part needs preserving here so we keep validating the migration.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY, handle TEXT NOT NULL, name TEXT,
    priority INTEGER NOT NULL DEFAULT 1, keywords TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
    title TEXT, url TEXT, published_at TEXT,
    state TEXT NOT NULL DEFAULT 'detected', is_priority INTEGER NOT NULL DEFAULT 0,
    transcript_path TEXT, transcript_source TEXT, summary_json TEXT,
    detected_at TEXT NOT NULL, transcribed_at TEXT, summarized_at TEXT,
    notified_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT, term TEXT NOT NULL UNIQUE,
    normalized_term TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
    definition TEXT, why_it_matters TEXT, category TEXT,
    first_seen_video_id TEXT, first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, published_at TEXT,
    summary TEXT, importance_score INTEGER,
    state TEXT NOT NULL DEFAULT 'seen', seen_at TEXT NOT NULL, notified_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
    videos_processed INTEGER NOT NULL DEFAULT 0,
    news_processed INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0, error TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
    channel TEXT NOT NULL, recipient TEXT, subject TEXT,
    payload_path TEXT, status TEXT NOT NULL, sent_at TEXT NOT NULL,
    error TEXT, run_id INTEGER
);
"""


def test_v1_to_v2_migration_adds_interactions(tmp_path: Path):
    """Migrating an existing v1 DB to v2 adds the interactions table and bumps version."""
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (1, '2026-06-10T00:00:00Z')"
    )
    conn.commit()

    # Now run the current init_schema — should add interactions + bump version.
    db_mod.init_schema(conn)
    assert db_mod.get_schema_version(conn) == 2
    # interactions table exists
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_init_schema_is_idempotent(tmp_db):
    """Calling init_schema multiple times on a v2 DB doesn't error or change version."""
    assert db_mod.get_schema_version(tmp_db) == 2
    db_mod.init_schema(tmp_db)
    db_mod.init_schema(tmp_db)
    assert db_mod.get_schema_version(tmp_db) == 2


def test_fresh_db_lands_at_current_version(tmp_db):
    """A freshly initialized DB reports the current schema version."""
    assert db_mod.get_schema_version(tmp_db) == db_mod.SCHEMA_VERSION
