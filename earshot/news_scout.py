"""AI news scout — fetches from configured sources, dedupes, scores via Claude.

Two source types defined in news_sources.yaml:
    rss : fetch via feedparser (handles RSS 2.0, Atom 1.0, RDF)
    hn  : query Hacker News Algolia API filtered by keywords + min_points

Items land in ``news_items`` with state='seen'. A second pass batch-scores
them via Haiku 4.5 (1-5 importance), flipping state to 'scored'. The digest
builder (module 6) reads scored items.

Dedup is by ``url`` (UNIQUE column). INSERT OR IGNORE makes the fetcher
idempotent — re-running scout in the same hour is a no-op for known URLs.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests

import anthropic

from earshot.analyzer import (
    CLAUDE_MODEL, MAX_OUTPUT_TOKENS, PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK,
    AnalyzerError, calc_cost,
)
from earshot.config import Config
from earshot.db import utcnow_iso


log = logging.getLogger("earshot.news_scout")

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
USER_AGENT = "Mozilla/5.0 (compatible; Earshot/0.1)"


# --------------------------------------------------------------------------- #
# Normalized item
# --------------------------------------------------------------------------- #

@dataclass
class NewsItem:
    source: str
    url: str
    title: str
    published_at: str  # ISO-8601 UTC if available, else empty
    summary: str       # short text snippet — may be empty


@dataclass
class FetchResult:
    source_name: str
    fetched: int
    inserted: int
    baselined: int
    already_seen: int
    error: str | None = None


@dataclass
class ScoutSummary:
    total_inserted: int
    total_baselined: int
    total_already_seen: int
    total_scored: int
    scoring_cost_usd: float
    scoring_tokens_in: int
    scoring_tokens_out: int


# --------------------------------------------------------------------------- #
# RSS fetcher (uses feedparser)
# --------------------------------------------------------------------------- #

def _to_iso(struct_time: time.struct_time | None) -> str:
    if not struct_time:
        return ""
    dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_html(text: str, max_chars: int = 800) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def fetch_rss(source: dict[str, Any]) -> list[NewsItem]:
    name = source.get("name", "?")
    url = source.get("url")
    if not url:
        raise ValueError(f"rss source '{name}' has no url")
    parsed = feedparser.parse(url, agent=USER_AGENT)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(
            f"feedparser failed for {url}: {parsed.bozo_exception}"
        )
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        title = entry.get("title", "").strip() or "(no title)"
        published = _to_iso(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        raw_summary = entry.get("summary") or entry.get("description") or ""
        items.append(NewsItem(
            source=name,
            url=link,
            title=title,
            published_at=published,
            summary=_clean_html(raw_summary),
        ))
    return items


# --------------------------------------------------------------------------- #
# Hacker News fetcher (Algolia API)
# --------------------------------------------------------------------------- #

def fetch_hn(source: dict[str, Any], days_back: int = 7) -> list[NewsItem]:
    """Search HN for stories matching any keyword above a points threshold."""
    name = source.get("name", "Hacker News")
    keywords: list[str] = source.get("keywords") or []
    if not keywords:
        return []
    min_points = int(source.get("min_points", 50))
    since_ts = int(time.time()) - days_back * 86400

    seen_urls: set[str] = set()
    items: list[NewsItem] = []

    for kw in keywords:
        params = {
            "query": kw,
            "tags": "story",
            "numericFilters": f"points>={min_points},created_at_i>={since_ts}",
            "hitsPerPage": 30,
        }
        try:
            resp = requests.get(
                HN_ALGOLIA_URL, params=params,
                headers={"User-Agent": USER_AGENT}, timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("HN keyword '%s' failed: %s", kw, e)
            continue
        for hit in resp.json().get("hits", []):
            link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            if link in seen_urls:
                continue
            seen_urls.add(link)
            published = ""
            if hit.get("created_at"):
                # already ISO-8601, normalize to Z
                published = hit["created_at"].replace("+00:00", "Z")
            items.append(NewsItem(
                source=name,
                url=link,
                title=(hit.get("title") or "").strip(),
                published_at=published,
                summary=f"{hit.get('points', 0)} points / {hit.get('num_comments', 0)} comments on Hacker News",
            ))
    return items


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _normalize_url(url: str) -> str:
    """Light normalization: strip trailing slashes, lowercase scheme+host.
    Keeps query strings intact (might carry article ids)."""
    try:
        p = urlparse(url)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/") or "/"
        rebuilt = f"{scheme}://{netloc}{path}"
        if p.query:
            rebuilt += f"?{p.query}"
        return rebuilt
    except Exception:
        return url


def _is_first_sync(conn: sqlite3.Connection, source_name: str) -> bool:
    """A source's first sync = no rows in news_items for that source yet."""
    row = conn.execute(
        "SELECT 1 FROM news_items WHERE source = ? LIMIT 1", (source_name,)
    ).fetchone()
    return row is None


def persist_items(
    conn: sqlite3.Connection,
    items: list[NewsItem],
    source_name: str,
) -> tuple[int, int, int]:
    """Persist items for one source. Returns (inserted_new, baselined, already_seen).

    On a source's first sync, current items are marked state='skipped' so we
    do not LLM-score the backlog. Subsequent runs insert as state='seen'.
    """
    inserted = baselined = already = 0
    now = utcnow_iso()
    first_sync = _is_first_sync(conn, source_name)
    state = "skipped" if first_sync else "seen"

    for it in items:
        url = _normalize_url(it.url)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO news_items
                (source, url, title, published_at, summary, state, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (it.source, url, it.title, it.published_at, it.summary, state, now),
        )
        if cur.rowcount:
            if first_sync:
                baselined += 1
            else:
                inserted += 1
        else:
            already += 1
    return inserted, baselined, already


def fetch_all_sources(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[FetchResult]:
    results: list[FetchResult] = []
    for src in sources:
        kind = src.get("type")
        name = src.get("name", "?")
        try:
            if kind == "rss":
                items = fetch_rss(src)
            elif kind == "hn":
                items = fetch_hn(src)
            else:
                results.append(FetchResult(
                    source_name=name, fetched=0, inserted=0, already_seen=0,
                    error=f"unknown source type: {kind!r}",
                ))
                continue
        except Exception as e:
            results.append(FetchResult(
                source_name=name, fetched=0, inserted=0, baselined=0, already_seen=0,
                error=str(e),
            ))
            continue

        if dry_run:
            results.append(FetchResult(
                source_name=name, fetched=len(items),
                inserted=0, baselined=0, already_seen=0,
            ))
            continue

        ins, baselined, already = persist_items(conn, items, name)
        results.append(FetchResult(
            source_name=name, fetched=len(items),
            inserted=ins, baselined=baselined, already_seen=already,
        ))
    return results


# --------------------------------------------------------------------------- #
# Claude relevance scoring
# --------------------------------------------------------------------------- #

SCORE_TOOL: dict[str, Any] = {
    "name": "submit_news_scores",
    "description": "Submit importance scores for a batch of news items.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "The id of the news item being scored.",
                        },
                        "importance_score": {
                            "type": "integer",
                            "description": "Importance 1-5. See guidelines.",
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence on why this score.",
                        },
                    },
                    "required": ["id", "importance_score", "reason"],
                },
            }
        },
        "required": ["scores"],
    },
}

SCORE_SYSTEM_PROMPT = """You are scoring AI news items for a curious listener who follows AI research, product launches, policy shifts, and notable industry moves.

Use the `submit_news_scores` tool. Return one entry per item, keyed by `id`.

Importance scale:
  1 = noise — promotional, vague, off-topic, or a rumor without substance.
  2 = minor — small product update, niche story, repackaged announcement.
  3 = noteworthy — interesting paper, industry move, executive change, secondary feature launch.
  4 = significant — major product launch, important paper, real policy shift, capability milestone.
  5 = landmark — breakthrough capability, major regulatory event, defining industry moment.

Calibration tips:
- Most items should land at 2 or 3. Reserve 4 for the day's standouts and 5 for genuine landmarks.
- Heavy promo / "X is hiring" / VC-funding-of-the-week posts are usually 1.
- An interesting paper from a known lab is usually 3; a results-redefining paper is 4.
- For every item return the id you were given. Do not invent ids.

CRITICAL FORMATTING:
- `scores` MUST be a JSON array of objects with fields {id, importance_score, reason}.
- Do not wrap in XML tags. Plain JSON objects only.
"""


def _select_unscored(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, source, title, summary, published_at
          FROM news_items
         WHERE state = 'seen'
         ORDER BY seen_at DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return list(rows)


def _build_score_user_message(rows: list[sqlite3.Row]) -> str:
    lines: list[str] = ["Score each of the following news items:\n"]
    for r in rows:
        snippet = (r["summary"] or "").strip()
        if len(snippet) > 400:
            snippet = snippet[:399] + "…"
        published = r["published_at"] or "unknown date"
        lines.append(
            f"--- id={r['id']} | source={r['source']} | published={published}\n"
            f"Title: {r['title']}\n"
            f"Summary: {snippet or '(none)'}\n"
        )
    return "\n".join(lines)


def score_batch(
    client: anthropic.Anthropic,
    rows: list[sqlite3.Row],
) -> tuple[dict[int, dict[str, Any]], int, int]:
    """Send a batch to Claude. Returns (scores_by_id, in_tok, out_tok)."""
    user_message = _build_score_user_message(rows)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SCORE_SYSTEM_PROMPT,
        tools=[SCORE_TOOL],
        tool_choice={"type": "tool", "name": "submit_news_scores"},
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_news_scores":
            payload = dict(block.input)
            scores_by_id: dict[int, dict[str, Any]] = {}
            for entry in payload.get("scores") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    eid = int(entry.get("id"))
                except (TypeError, ValueError):
                    continue
                score = entry.get("importance_score")
                if not isinstance(score, int):
                    continue
                score = max(1, min(5, score))
                scores_by_id[eid] = {
                    "importance_score": score,
                    "reason": (entry.get("reason") or "").strip(),
                }
            return (
                scores_by_id,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
    raise AnalyzerError(
        f"Claude did not return submit_news_scores tool_use (stop_reason={response.stop_reason})"
    )


def score_unscored(
    conn: sqlite3.Connection,
    cfg: Config,
    client: anthropic.Anthropic,
    batch_size: int = 50,
    max_batches: int = 5,
    run_id: int | None = None,
) -> tuple[int, int, int, float]:
    """Score up to ``batch_size * max_batches`` unscored items.

    Returns (scored_count, total_in_tok, total_out_tok, cost_usd).
    """
    total_in = total_out = total_scored = 0
    total_cost = 0.0
    for _ in range(max_batches):
        rows = _select_unscored(conn, limit=batch_size)
        if not rows:
            break
        try:
            scores_by_id, in_tok, out_tok = score_batch(client, rows)
        except (anthropic.APIError, AnalyzerError) as e:
            log.warning("score_batch failed: %s", e)
            return total_scored, total_in, total_out, total_cost
        cost = calc_cost(in_tok, out_tok)
        total_in += in_tok
        total_out += out_tok
        total_cost += cost

        for row in rows:
            entry = scores_by_id.get(int(row["id"]))
            if entry is None:
                # Claude omitted this item — leave state='seen' so a future
                # run retries it.
                continue
            conn.execute(
                """
                UPDATE news_items
                   SET importance_score = ?,
                       state = 'scored'
                 WHERE id = ?
                """,
                (entry["importance_score"], row["id"]),
            )
            total_scored += 1

        if run_id is not None:
            conn.execute(
                """
                UPDATE runs
                   SET tokens_in      = tokens_in + ?,
                       tokens_out     = tokens_out + ?,
                       cost_usd       = cost_usd + ?,
                       news_processed = news_processed + ?
                 WHERE id = ?
                """,
                (in_tok, out_tok, cost, len(scores_by_id), run_id),
            )
    return total_scored, total_in, total_out, total_cost


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def scout(
    conn: sqlite3.Connection,
    cfg: Config,
    client: anthropic.Anthropic | None,
    dry_run: bool = False,
    skip_scoring: bool = False,
    run_id: int | None = None,
) -> tuple[list[FetchResult], ScoutSummary]:
    fetch_results = fetch_all_sources(conn, cfg.news_sources, dry_run=dry_run)
    total_inserted = sum(r.inserted for r in fetch_results)
    total_baselined = sum(r.baselined for r in fetch_results)
    total_already = sum(r.already_seen for r in fetch_results)

    scoring_cost = 0.0
    scoring_in = scoring_out = 0
    scored_n = 0
    if not dry_run and not skip_scoring and client is not None:
        scored_n, scoring_in, scoring_out, scoring_cost = score_unscored(
            conn, cfg, client, run_id=run_id,
        )

    return fetch_results, ScoutSummary(
        total_inserted=total_inserted,
        total_baselined=total_baselined,
        total_already_seen=total_already,
        total_scored=scored_n,
        scoring_cost_usd=scoring_cost,
        scoring_tokens_in=scoring_in,
        scoring_tokens_out=scoring_out,
    )
