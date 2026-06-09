"""Digest builder — assembles daily and instant payloads from the DB.

Two delivery paths:
  * **Daily digest**: summarized videos + scored news above
    ``DIGEST_MIN_NEWS_SCORE``, bundled into a single payload.
  * **Instant alerts**: priority videos (is_priority=1) + score-5 news, one
    payload per item, fires immediately. Items emitted as instant are NOT
    included in the next daily digest — ``notified_at`` is set per item, and
    daily selectors all filter ``notified_at IS NULL``.

Output: per-payload markdown + HTML strings. The notifier (module 7) consumes
these. For dry-runs and audit, payloads are also archived under
``data/digests/<date>/...``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from earshot.config import Config
from earshot.db import utcnow_iso


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #

@dataclass
class DigestVideo:
    video_id: str
    channel_name: str
    channel_handle: str
    title: str
    url: str
    is_priority: bool
    summary: str
    key_takeaways: list[str]
    notable_quotes: list[str]
    new_concepts: list[dict[str, Any]]  # full glossary rows for this video


@dataclass
class DigestNewsItem:
    id: int
    source: str
    title: str
    url: str
    importance_score: int
    summary: str


@dataclass
class DigestPayload:
    digest_type: str           # 'daily' | 'instant_video' | 'instant_news'
    date_local: str            # YYYY-MM-DD in configured timezone
    subject: str
    videos: list[DigestVideo] = field(default_factory=list)
    news_items: list[DigestNewsItem] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def today_in_tz(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _safe_list(x: Any) -> list:
    return x if isinstance(x, list) else []


def _normalize_string_list(items: list) -> list[str]:
    """Same defense pattern from cli.py: tolerate char-fragmented arrays and
    legacy {quote: ...} dict shapes from earlier Claude calls."""
    if not items:
        return []
    str_items = [x for x in items if isinstance(x, str)]
    if len(str_items) == len(items) and len(items) > 20:
        tiny = sum(1 for s in str_items if len(s) <= 2)
        if tiny / len(items) > 0.5:
            import re
            joined = "".join(str_items)
            cleaned = re.sub(r"<[^>]+>", "", joined).strip()
            return [cleaned] if cleaned else []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
            if s:
                out.append(s)
        elif isinstance(it, dict):
            for key in ("quote", "text", "takeaway", "content"):
                v = it.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
    return out


def _video_to_digest(conn: sqlite3.Connection, video_row: sqlite3.Row) -> DigestVideo:
    a = json.loads(video_row["summary_json"]) if video_row["summary_json"] else {}
    channel = conn.execute(
        "SELECT name, handle FROM channels WHERE channel_id = ?",
        (video_row["channel_id"],),
    ).fetchone()
    new_concepts = [
        dict(r) for r in conn.execute(
            """
            SELECT term, category, definition, why_it_matters
              FROM glossary
             WHERE first_seen_video_id = ?
             ORDER BY category, term
            """,
            (video_row["video_id"],),
        ).fetchall()
    ]
    return DigestVideo(
        video_id=video_row["video_id"],
        channel_name=channel["name"] if channel else "Unknown",
        channel_handle=channel["handle"] if channel else "",
        title=video_row["title"] or "(untitled)",
        url=video_row["url"] or f"https://www.youtube.com/watch?v={video_row['video_id']}",
        is_priority=bool(video_row["is_priority"]),
        summary=str(a.get("summary") or ""),
        key_takeaways=_normalize_string_list(_safe_list(a.get("key_takeaways"))),
        notable_quotes=_normalize_string_list(_safe_list(a.get("notable_quotes"))),
        new_concepts=new_concepts,
    )


def _news_to_digest(news_row: sqlite3.Row) -> DigestNewsItem:
    return DigestNewsItem(
        id=int(news_row["id"]),
        source=news_row["source"] or "?",
        title=news_row["title"] or "(no title)",
        url=news_row["url"] or "",
        importance_score=int(news_row["importance_score"] or 0),
        summary=news_row["summary"] or "",
    )


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #

def _select_unnotified_videos(
    conn: sqlite3.Connection, *, priority_only: bool = False,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM videos WHERE state = 'summarized' AND notified_at IS NULL"
    if priority_only:
        sql += " AND is_priority = 1"
    sql += " ORDER BY detected_at ASC"
    return list(conn.execute(sql).fetchall())


def _select_unnotified_news(
    conn: sqlite3.Connection, *, min_score: int,
) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM news_items
         WHERE state = 'scored'
           AND notified_at IS NULL
           AND importance_score >= ?
         ORDER BY importance_score DESC, seen_at DESC
        """,
        (min_score,),
    ).fetchall())


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def build_daily(conn: sqlite3.Connection, cfg: Config) -> DigestPayload | None:
    """Build a single daily payload. Returns None if nothing to include."""
    video_rows = _select_unnotified_videos(conn, priority_only=False)
    news_rows = _select_unnotified_news(conn, min_score=cfg.digest_min_news_score)
    if not video_rows and not news_rows:
        return None
    videos = [_video_to_digest(conn, r) for r in video_rows]
    news_items = [_news_to_digest(r) for r in news_rows]
    date_local = today_in_tz(cfg.digest_timezone)
    subject = (
        f"Earshot daily digest — {date_local} "
        f"({len(videos)} episode{'s' if len(videos)!=1 else ''}, "
        f"{len(news_items)} news item{'s' if len(news_items)!=1 else ''})"
    )
    return DigestPayload(
        digest_type="daily",
        date_local=date_local,
        subject=subject,
        videos=videos,
        news_items=news_items,
    )


def build_instant_alerts(conn: sqlite3.Connection, cfg: Config) -> list[DigestPayload]:
    """One payload per instant-worthy item. Empty list if none."""
    payloads: list[DigestPayload] = []
    date_local = today_in_tz(cfg.digest_timezone)

    for r in _select_unnotified_videos(conn, priority_only=True):
        v = _video_to_digest(conn, r)
        subject = f"Earshot ALERT — {v.channel_handle}: {v.title}"
        payloads.append(DigestPayload(
            digest_type="instant_video",
            date_local=date_local,
            subject=subject,
            videos=[v],
        ))

    for r in _select_unnotified_news(conn, min_score=cfg.instant_min_news_score):
        n = _news_to_digest(r)
        subject = f"Earshot ALERT [{n.importance_score}] — {n.source}: {n.title[:80]}"
        payloads.append(DigestPayload(
            digest_type="instant_news",
            date_local=date_local,
            subject=subject,
            news_items=[n],
        ))
    return payloads


# --------------------------------------------------------------------------- #
# Mark notified
# --------------------------------------------------------------------------- #

def mark_notified(conn: sqlite3.Connection, payload: DigestPayload) -> None:
    now = utcnow_iso()
    for v in payload.videos:
        conn.execute(
            "UPDATE videos SET state='notified', notified_at=? WHERE video_id=?",
            (now, v.video_id),
        )
    for n in payload.news_items:
        conn.execute(
            "UPDATE news_items SET state='notified', notified_at=? WHERE id=?",
            (now, n.id),
        )


# --------------------------------------------------------------------------- #
# Renderers — markdown
# --------------------------------------------------------------------------- #

def render_markdown(p: DigestPayload) -> str:
    out: list[str] = []
    if p.digest_type == "daily":
        out.append(f"# Earshot Daily Digest — {p.date_local}\n")
    elif p.digest_type == "instant_video":
        out.append(f"# Earshot Instant Alert — {p.date_local}\n")
        out.append("**High-priority episode just landed.**\n")
    else:  # instant_news
        out.append(f"# Earshot Instant Alert — {p.date_local}\n")
        out.append("**Landmark AI news item just landed.**\n")

    if p.videos:
        out.append(f"## Episodes ({len(p.videos)})\n")
        for v in p.videos:
            tag = " [PRIORITY]" if v.is_priority else ""
            out.append(f"### {v.channel_handle} — {v.title}{tag}")
            out.append(f"{v.url}\n")
            if v.summary:
                out.append(f"> {v.summary}\n")
            if v.key_takeaways:
                out.append("**Key takeaways:**")
                for t in v.key_takeaways:
                    out.append(f"- {t}")
                out.append("")
            if v.notable_quotes:
                out.append("**Notable quotes:**")
                for q in v.notable_quotes:
                    out.append(f"> \"{q}\"")
                out.append("")
            if v.new_concepts:
                out.append(f"**New concepts to study ({len(v.new_concepts)}):**")
                for c in v.new_concepts:
                    out.append(
                        f"- **{c['term']}** ({c['category']}) — {c['definition']}"
                    )
                out.append("")
            out.append("---\n")

    if p.news_items:
        out.append(f"## AI News ({len(p.news_items)})\n")
        for n in p.news_items:
            out.append(f"### [{n.importance_score}] {n.source} — {n.title}")
            out.append(f"{n.url}\n")
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Renderers — HTML
# --------------------------------------------------------------------------- #

def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
    )


_HTML_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 720px; margin: 0 auto; padding: 1em; color: #222; }
  h1 { color: #111; border-bottom: 2px solid #444; padding-bottom: .25em; }
  h2 { color: #333; margin-top: 1.5em; border-bottom: 1px solid #ccc; padding-bottom: .15em; }
  h3 { color: #222; margin-top: 1em; margin-bottom: .25em; }
  blockquote { border-left: 3px solid #888; margin: .5em 0; padding: .25em .75em;
               background: #f6f6f6; color: #333; }
  .priority { color: #b00; font-weight: bold; }
  .score { display:inline-block; min-width:1.5em; padding:.05em .35em;
           background:#eef; border-radius:3px; font-weight:bold; color:#225; }
  .source { color: #666; font-size: .92em; }
  ul.concepts li { margin-bottom: .35em; }
  hr { border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }
  a { color: #1a5fb4; }
</style>
""".strip()


def render_html(p: DigestPayload) -> str:
    out: list[str] = ["<html><head>", _HTML_STYLE, "</head><body>"]
    if p.digest_type == "daily":
        out.append(f"<h1>Earshot Daily Digest — {_esc(p.date_local)}</h1>")
    elif p.digest_type == "instant_video":
        out.append(f"<h1>Earshot Instant Alert — {_esc(p.date_local)}</h1>")
        out.append("<p><span class='priority'>High-priority episode just landed.</span></p>")
    else:
        out.append(f"<h1>Earshot Instant Alert — {_esc(p.date_local)}</h1>")
        out.append("<p><span class='priority'>Landmark AI news item just landed.</span></p>")

    if p.videos:
        out.append(f"<h2>Episodes ({len(p.videos)})</h2>")
        for v in p.videos:
            tag = " <span class='priority'>[PRIORITY]</span>" if v.is_priority else ""
            out.append(
                f"<h3>{_esc(v.channel_handle)} — {_esc(v.title)}{tag}</h3>"
            )
            out.append(
                f"<p class='source'><a href=\"{_esc(v.url)}\">{_esc(v.url)}</a></p>"
            )
            if v.summary:
                out.append(f"<blockquote>{_esc(v.summary)}</blockquote>")
            if v.key_takeaways:
                out.append("<p><b>Key takeaways:</b></p><ul>")
                for t in v.key_takeaways:
                    out.append(f"<li>{_esc(t)}</li>")
                out.append("</ul>")
            if v.notable_quotes:
                out.append("<p><b>Notable quotes:</b></p>")
                for q in v.notable_quotes:
                    out.append(f"<blockquote>\"{_esc(q)}\"</blockquote>")
            if v.new_concepts:
                out.append(
                    f"<p><b>New concepts to study ({len(v.new_concepts)}):</b></p>"
                    "<ul class='concepts'>"
                )
                for c in v.new_concepts:
                    out.append(
                        f"<li><b>{_esc(c['term'])}</b> "
                        f"<span class='source'>({_esc(c['category'])})</span> — "
                        f"{_esc(c['definition'])}</li>"
                    )
                out.append("</ul>")
            out.append("<hr/>")

    if p.news_items:
        out.append(f"<h2>AI News ({len(p.news_items)})</h2>")
        for n in p.news_items:
            out.append(
                f"<h3><span class='score'>{n.importance_score}</span> "
                f"{_esc(n.source)} — <a href=\"{_esc(n.url)}\">{_esc(n.title)}</a></h3>"
            )

    out.append("</body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Archive to disk
# --------------------------------------------------------------------------- #

def archive_payload(payload: DigestPayload, data_dir: Path) -> Path:
    """Write rendered markdown + html to data/digests/<date>/<type>-<n>.{md,html}.

    Returns the path to the markdown file. Multiple instant payloads for the
    same date land as instant_video-1.md, instant_video-2.md, ...
    """
    day_dir = data_dir / "digests" / payload.date_local
    day_dir.mkdir(parents=True, exist_ok=True)
    base = payload.digest_type
    if payload.digest_type.startswith("instant"):
        # Avoid clobber by numbering
        existing = sorted(day_dir.glob(f"{base}-*.md"))
        n = len(existing) + 1
        stem = f"{base}-{n:02d}"
    else:
        stem = base
    md_path = day_dir / f"{stem}.md"
    html_path = day_dir / f"{stem}.html"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    return md_path
