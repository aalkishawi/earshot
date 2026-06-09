"""Episode summarization + concept extraction via Claude.

Uses Claude Haiku 4.5 (locked decision: cheapest viable model) with
**tool-use structured output** — the most reliable way to get strict JSON
from Claude. The transcript is passed as the user message; Claude is
forced to call the ``submit_episode_analysis`` tool, whose ``input`` is
the structured payload.

Per-call cost tracking goes into the ``runs`` table. A per-run hard cap
(``MAX_LLM_COST_PER_RUN_USD``) prevents a runaway batch.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from earshot.config import Config
from earshot.db import utcnow_iso


log = logging.getLogger("earshot.analyzer")


CLAUDE_MODEL = "claude-haiku-4-5"
# Haiku 4.5 pricing (USD per million tokens). Locked in 2026-06 — verify if
# pricing tiers change. https://www.anthropic.com/pricing
PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00

MAX_OUTPUT_TOKENS = 4096
# Hard upper bound on transcript chars to send. 500K chars ~ 125K tokens which
# still fits comfortably in Haiku's 200K context with headroom for prompt +
# output. Anything bigger is almost certainly an ASR-on-a-lecture edge case.
TRANSCRIPT_CHAR_CAP = 500_000


# --------------------------------------------------------------------------- #
# Prompt + tool schema
# --------------------------------------------------------------------------- #

ANALYZER_TOOL: dict[str, Any] = {
    "name": "submit_episode_analysis",
    "description": "Submit the structured analysis of a podcast episode.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary of what the episode covered.",
            },
            "key_takeaways": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-5 distinct, standalone insights worth remembering.",
            },
            "notable_quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "0-3 striking direct quotes. Verbatim. Plain strings, one quote per array element.",
            },
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "Term as commonly written (e.g. 'RAG', 'Claude Sonnet 4.6').",
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "concept", "tool", "person", "paper",
                                "model", "acronym", "company",
                            ],
                        },
                        "definition": {
                            "type": "string",
                            "description": "One short sentence, accessible to a smart non-expert.",
                        },
                        "why_it_matters": {
                            "type": "string",
                            "description": "Why a curious listener should look this up.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Brief note on how it appeared in the episode.",
                        },
                    },
                    "required": ["term", "category", "definition", "why_it_matters", "context"],
                },
                "description": (
                    "Terms substantively discussed. Skip passing mentions. "
                    "Quality over quantity — 5 high-signal beat 30 noise."
                ),
            },
        },
        "required": ["summary", "key_takeaways", "notable_quotes", "concepts"],
    },
}


SYSTEM_PROMPT = """You are analyzing a podcast episode transcript for a curious listener who wants to:
1. Get a quick read on what the episode covered.
2. Build a study queue of concepts, tools, people, and papers they encountered.

Use the `submit_episode_analysis` tool to return your structured analysis.

CRITICAL FORMATTING RULES:
- `key_takeaways` must be a JSON array of strings. Each array element is ONE full takeaway sentence. Do NOT wrap takeaways in XML tags, do not split them across array elements, do not nest objects.
- `notable_quotes` must be a JSON array of strings. Each array element is ONE verbatim quote. Plain strings only.
- `concepts` must be a JSON array of objects. Each object has term, category, definition, why_it_matters, context.

Example of correctly-shaped output:
{
  "summary": "Sam Altman and Lex Fridman discuss the state of AI safety and the path to AGI...",
  "key_takeaways": [
    "OpenAI's superalignment team is targeting alignment of superhuman models within four years.",
    "Altman believes current models do not yet exhibit meaningful situational awareness."
  ],
  "notable_quotes": [
    "If we get this wrong, the consequences are catastrophic and irreversible."
  ],
  "concepts": [
    {"term": "Superalignment", "category": "concept", "definition": "...", "why_it_matters": "...", "context": "..."}
  ]
}

Content guidelines:
- summary: 2-3 sentences. Cover the topic and participants if relevant.
- key_takeaways: 3-5 concrete insights or claims. Each is a complete sentence.
- notable_quotes: 0-3 verbatim quotes that capture striking ideas. Plain strings.
- concepts: Terms substantively discussed. Skip passing mentions and obvious terms ("Google", "iPhone", "ChatGPT"). Quality over quantity — 5 high-signal concepts beat 30 noisy ones.

For each concept:
- term: the most common written form (e.g. "RAG", "Demis Hassabis", "Attention Is All You Need", "Claude Sonnet 4.6").
- category: one of concept, tool, person, paper, model, acronym, company.
- definition: one accessible sentence for a smart non-expert.
- why_it_matters: why this is worth looking up.
- context: brief note on how it appeared in the episode."""


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #

class AnalyzerError(Exception):
    pass


class CostCapReached(Exception):
    pass


@dataclass
class AnalyzeResult:
    video_id: str
    status: str  # 'summarized' | 'failed' | 'skipped'
    new_concepts: int = 0
    dup_concepts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def calc_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * PRICE_INPUT_PER_MTOK
        + output_tokens * PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000


def normalize_term(term: str) -> str:
    """Normalize a term for glossary dedup. Lowercase, strip surrounding
    whitespace and punctuation, collapse internal whitespace."""
    t = term.strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = t.strip(".,;:!?\"'`()[]{}")
    return t


# --------------------------------------------------------------------------- #
# Claude call
# --------------------------------------------------------------------------- #

def call_claude(
    client: anthropic.Anthropic,
    transcript: str,
    channel_name: str,
    video_title: str,
) -> tuple[dict[str, Any], int, int]:
    """Single Claude call. Returns (analysis_dict, input_tokens, output_tokens)."""
    user_message = (
        f"Channel: {channel_name}\n"
        f"Episode title: {video_title}\n\n"
        f"Transcript:\n\n{transcript}"
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[ANALYZER_TOOL],
        tool_choice={"type": "tool", "name": "submit_episode_analysis"},
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_episode_analysis":
            return (
                dict(block.input),
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
    raise AnalyzerError(
        f"Claude did not call submit_episode_analysis. stop_reason={response.stop_reason}"
    )


# --------------------------------------------------------------------------- #
# Glossary persistence
# --------------------------------------------------------------------------- #

def upsert_concept(
    conn: sqlite3.Connection,
    concept: dict[str, Any],
    video_id: str,
) -> bool:
    """Insert concept if not already in glossary (by normalized_term).

    Returns True if newly inserted, False if it was already known."""
    term = concept["term"]
    normalized = normalize_term(term)
    if not normalized:
        return False
    existing = conn.execute(
        "SELECT id FROM glossary WHERE normalized_term = ?", (normalized,)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT INTO glossary
            (term, normalized_term, aliases, definition, why_it_matters, category,
             first_seen_video_id, first_seen_at)
        VALUES (?, ?, '[]', ?, ?, ?, ?, ?)
        """,
        (
            term, normalized,
            concept.get("definition", ""),
            concept.get("why_it_matters", ""),
            concept.get("category", "concept"),
            video_id, utcnow_iso(),
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Run accounting
# --------------------------------------------------------------------------- #

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
        (utcnow_iso(),),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, error=? WHERE id=?",
        (utcnow_iso(), status, error, run_id),
    )


def run_cost(conn: sqlite3.Connection, run_id: int) -> float:
    row = conn.execute("SELECT cost_usd FROM runs WHERE id = ?", (run_id,)).fetchone()
    return float(row["cost_usd"]) if row else 0.0


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def analyze_video(
    conn: sqlite3.Connection,
    video_row: sqlite3.Row,
    cfg: Config,
    client: anthropic.Anthropic | None,
    run_id: int | None = None,
    dry_run: bool = False,
) -> AnalyzeResult:
    video_id = video_row["video_id"]
    transcript_path_str = video_row["transcript_path"]
    if not transcript_path_str:
        return AnalyzeResult(
            video_id=video_id, status="failed",
            error="no transcript_path on video row — run `earshot transcribe` first",
        )
    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        return AnalyzeResult(
            video_id=video_id, status="failed",
            error=f"transcript file missing: {transcript_path}",
        )

    transcript = transcript_path.read_text(encoding="utf-8")
    truncated = False
    if len(transcript) > TRANSCRIPT_CHAR_CAP:
        transcript = transcript[:TRANSCRIPT_CHAR_CAP]
        truncated = True

    channel_row = conn.execute(
        "SELECT handle, name FROM channels WHERE channel_id = ?",
        (video_row["channel_id"],),
    ).fetchone()
    channel_name = channel_row["name"] if channel_row else "Unknown channel"
    video_title = video_row["title"] or "(untitled)"

    if dry_run:
        log.info(
            "dry-run analyze: %s  transcript_chars=%d truncated=%s",
            video_id, len(transcript), truncated,
        )
        return AnalyzeResult(
            video_id=video_id, status="skipped",
            error=f"dry-run: would send {len(transcript):,} chars to {CLAUDE_MODEL}",
        )

    if client is None:
        return AnalyzeResult(
            video_id=video_id, status="failed",
            error="ANTHROPIC_API_KEY not set",
        )

    # Pre-call cap check.
    if run_id is not None and run_cost(conn, run_id) >= cfg.max_llm_cost_per_run_usd:
        raise CostCapReached(
            f"run {run_id} reached cost cap ${cfg.max_llm_cost_per_run_usd:.2f}"
        )

    try:
        analysis, in_tok, out_tok = call_claude(client, transcript, channel_name, video_title)
    except anthropic.APIError as e:
        # Treat API errors as retriable: don't flip state to 'failed', so the
        # next analyze run picks the video up again. Billing / rate-limit /
        # auth / 5xx all belong here. We still record the error on the row.
        conn.execute(
            "UPDATE videos SET error=? WHERE video_id=?",
            (f"analyzer (retriable): {e}", video_id),
        )
        return AnalyzeResult(video_id=video_id, status="failed", error=str(e))
    except AnalyzerError as e:
        # Tool-use parse failure or similar — also retriable; could be a flake.
        conn.execute(
            "UPDATE videos SET error=? WHERE video_id=?",
            (f"analyzer (retriable): {e}", video_id),
        )
        return AnalyzeResult(video_id=video_id, status="failed", error=str(e))

    cost = calc_cost(in_tok, out_tok)

    new_n = dup_n = 0
    for concept in analysis.get("concepts", []) or []:
        if not isinstance(concept, dict) or "term" not in concept:
            continue
        if upsert_concept(conn, concept, video_id):
            new_n += 1
        else:
            dup_n += 1

    conn.execute(
        """
        UPDATE videos
           SET state='summarized',
               summary_json=?,
               summarized_at=?,
               error=NULL
         WHERE video_id=?
        """,
        (json.dumps(analysis, sort_keys=True, ensure_ascii=False), utcnow_iso(), video_id),
    )

    if run_id is not None:
        conn.execute(
            """
            UPDATE runs
               SET tokens_in        = tokens_in + ?,
                   tokens_out       = tokens_out + ?,
                   cost_usd         = cost_usd + ?,
                   videos_processed = videos_processed + 1
             WHERE id = ?
            """,
            (in_tok, out_tok, cost, run_id),
        )

    return AnalyzeResult(
        video_id=video_id, status="summarized",
        new_concepts=new_n, dup_concepts=dup_n,
        input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
    )


def select_pending(
    conn: sqlite3.Connection,
    limit: int | None = None,
    video_id: str | None = None,
) -> list[sqlite3.Row]:
    if video_id:
        return list(conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchall())
    sql = "SELECT * FROM videos WHERE state = 'transcribed' ORDER BY detected_at ASC"
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql).fetchall())


def make_client(cfg: Config) -> anthropic.Anthropic | None:
    if not cfg.anthropic_api_key:
        return None
    return anthropic.Anthropic(api_key=cfg.anthropic_api_key)
