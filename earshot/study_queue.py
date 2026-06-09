"""Study queue export — regenerates ``study_queue.md`` from the glossary table.

Single source of truth is the SQLite ``glossary`` table; this module exports
a human-friendly Markdown rendering grouped by category. Re-running is
idempotent: the file is fully rewritten each call.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from earshot.db import utcnow_iso


CATEGORY_ORDER = [
    "concept",
    "tool",
    "model",
    "paper",
    "person",
    "company",
    "acronym",
    "other",
]


def render(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT term, category, definition, why_it_matters,
               first_seen_video_id, first_seen_at
          FROM glossary
         ORDER BY first_seen_at DESC, term ASC
        """
    ).fetchall()

    total = len(rows)
    out: list[str] = []
    out.append("# Earshot Study Queue")
    out.append("")
    out.append(
        f"*{total} term{'s' if total != 1 else ''}, regenerated {utcnow_iso()}.*"
    )
    out.append("")
    if total == 0:
        out.append("_No terms yet. Run `earshot analyze` after transcribing an episode._")
        out.append("")
        return "\n".join(out) + "\n"

    by_category: dict[str, list] = {}
    for r in rows:
        cat = (r["category"] or "other").lower()
        by_category.setdefault(cat, []).append(r)

    ordered = [c for c in CATEGORY_ORDER if c in by_category]
    ordered += [c for c in by_category if c not in CATEGORY_ORDER]

    for cat in ordered:
        items = by_category[cat]
        out.append(f"## {cat.capitalize()} ({len(items)})")
        out.append("")
        for r in items:
            term = r["term"]
            definition = (r["definition"] or "").strip()
            why = (r["why_it_matters"] or "").strip()
            video_ref = r["first_seen_video_id"] or ""
            ref = ""
            if video_ref:
                ref = f"  \n  _first seen in_ [{video_ref}](https://www.youtube.com/watch?v={video_ref})"
            out.append(f"- **{term}**: {definition}")
            if why:
                out.append(f"  \n  _why:_ {why}{ref}")
            elif ref:
                out.append(ref.lstrip())
        out.append("")
    return "\n".join(out) + "\n"


def write_file(conn: sqlite3.Connection, path: Path) -> int:
    """Render and write study_queue.md. Returns number of terms exported."""
    text = render(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
