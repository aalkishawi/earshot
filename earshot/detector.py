"""New-video detection from YouTube channel RSS feeds.

Design notes
------------
- Uses each channel's public Atom feed (no API quota): ``/feeds/videos.xml?channel_id=…``.
- Stdlib XML parsing (``xml.etree.ElementTree``) — no extra dependency.
- Idempotency is enforced at the DB layer via ``INSERT OR IGNORE`` on ``video_id``.
- **First-sync baseline:** When a channel has zero rows in ``videos``, every
  entry currently in its RSS feed is inserted with ``state='skipped'`` and a
  marker in ``error``. This keeps us from LLM-processing the ~15 pre-existing
  episodes at the moment a channel is added. From the next poll onward, any
  truly new ``video_id`` lands as ``state='detected'``.
- Priority computation: high-priority when channel.priority >= 2, OR any
  channel keyword (case-insensitive substring) appears in the video title.
"""
from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

import requests

from earshot.config import ChannelConfig
from earshot.db import utcnow_iso


_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
_USER_AGENT = "Mozilla/5.0 (compatible; Earshot/0.1)"
_BASELINE_MARKER = "__baseline__"


@dataclass
class VideoEntry:
    video_id: str
    channel_id: str
    title: str
    url: str
    published_at: str  # ISO-8601 as provided by the feed


@dataclass
class DetectionResult:
    channel_handle: str
    fetched: int
    inserted_new: int          # state='detected'
    baselined: int             # state='skipped' (first sync)
    already_seen: int          # row existed → no-op
    priority_flagged: int      # subset of inserted_new
    error: str | None = None


def fetch_feed(channel: ChannelConfig, timeout: float = 15.0) -> list[VideoEntry]:
    """Fetch and parse a channel's Atom feed. Raises on HTTP error."""
    resp = requests.get(
        channel.rss_url,
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_feed(resp.content)


def _parse_feed(xml_bytes: bytes) -> list[VideoEntry]:
    root = ET.fromstring(xml_bytes)
    out: list[VideoEntry] = []
    for entry in root.findall("atom:entry", _NS):
        vid_el = entry.find("yt:videoId", _NS)
        cid_el = entry.find("yt:channelId", _NS)
        title_el = entry.find("atom:title", _NS)
        link_el = entry.find("atom:link", _NS)
        pub_el = entry.find("atom:published", _NS)
        if vid_el is None or vid_el.text is None:
            continue
        href = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={vid_el.text}"
        out.append(VideoEntry(
            video_id=vid_el.text,
            channel_id=(cid_el.text if cid_el is not None and cid_el.text else ""),
            title=(title_el.text if title_el is not None and title_el.text else ""),
            url=href or "",
            published_at=(pub_el.text if pub_el is not None and pub_el.text else ""),
        ))
    return out


def is_priority(channel: ChannelConfig, title: str) -> bool:
    if channel.priority >= 2:
        return True
    if not channel.keywords:
        return False
    # Word-boundary match (case-insensitive) so a short keyword like "AI"
    # doesn't fire on "Said" or "Waist".
    for kw in channel.keywords:
        pattern = rf"\b{re.escape(kw)}\b"
        if re.search(pattern, title, re.IGNORECASE):
            return True
    return False


def sync_channels(conn: sqlite3.Connection, channels: Iterable[ChannelConfig]) -> None:
    """Upsert channels from yaml into the channels table.

    Existing rows are updated in place (priority/keywords/name may have changed).
    Channels removed from yaml are NOT deleted — we keep historical FK targets.
    They are flipped to ``active=0`` instead.
    """
    import json
    now = utcnow_iso()
    yaml_ids = set()
    for c in channels:
        yaml_ids.add(c.channel_id)
        conn.execute(
            """
            INSERT INTO channels (channel_id, handle, name, priority, keywords, active, added_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                handle   = excluded.handle,
                name     = excluded.name,
                priority = excluded.priority,
                keywords = excluded.keywords,
                active   = 1
            """,
            (c.channel_id, c.handle, c.name, c.priority, json.dumps(c.keywords), now),
        )
    # Deactivate any channel rows no longer in yaml.
    rows = conn.execute("SELECT channel_id FROM channels WHERE active = 1").fetchall()
    for row in rows:
        if row["channel_id"] not in yaml_ids:
            conn.execute("UPDATE channels SET active = 0 WHERE channel_id = ?", (row["channel_id"],))


def detect_for_channel(
    conn: sqlite3.Connection,
    channel: ChannelConfig,
    dry_run: bool = False,
) -> DetectionResult:
    """Fetch one channel's feed and insert any new entries."""
    try:
        entries = fetch_feed(channel)
    except requests.RequestException as e:
        return DetectionResult(
            channel_handle=channel.handle,
            fetched=0, inserted_new=0, baselined=0, already_seen=0, priority_flagged=0,
            error=f"fetch failed: {e}",
        )

    # Determine whether this is the channel's first sync (no videos rows yet).
    existing_for_channel = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE channel_id = ?",
        (channel.channel_id,),
    ).fetchone()["n"]
    first_sync = existing_for_channel == 0

    now = utcnow_iso()
    inserted = 0
    baselined = 0
    already = 0
    priority_flagged = 0

    for e in entries:
        exists = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (e.video_id,)
        ).fetchone()
        if exists:
            already += 1
            continue

        if first_sync:
            state = "skipped"
            err_note = _BASELINE_MARKER
            is_prio_int = 0
        else:
            state = "detected"
            err_note = None
            is_prio_int = 1 if is_priority(channel, e.title) else 0
            if is_prio_int:
                priority_flagged += 1

        if dry_run:
            if first_sync:
                baselined += 1
            else:
                inserted += 1
            continue

        conn.execute(
            """
            INSERT OR IGNORE INTO videos
                (video_id, channel_id, title, url, published_at,
                 state, is_priority, detected_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (e.video_id, channel.channel_id, e.title, e.url, e.published_at,
             state, is_prio_int, now, err_note),
        )
        if first_sync:
            baselined += 1
        else:
            inserted += 1

    return DetectionResult(
        channel_handle=channel.handle,
        fetched=len(entries),
        inserted_new=inserted,
        baselined=baselined,
        already_seen=already,
        priority_flagged=priority_flagged,
    )


def detect_all(
    conn: sqlite3.Connection,
    channels: list[ChannelConfig],
    dry_run: bool = False,
    handle_filter: str | None = None,
) -> list[DetectionResult]:
    if not dry_run:
        sync_channels(conn, channels)

    results: list[DetectionResult] = []
    for c in channels:
        if handle_filter and c.handle.lower() != handle_filter.lower():
            continue
        if not c.active:
            continue
        results.append(detect_for_channel(conn, c, dry_run=dry_run))
    return results
