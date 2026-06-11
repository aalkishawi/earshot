"""SQL helpers for the ``interactions`` table."""
from __future__ import annotations

import sqlite3

from earshot.agent.state import (
    Interaction, ItemRef, Turn,
    dump_item_refs, dump_turns, parse_item_refs, parse_turns,
)
from earshot.db import utcnow_iso


def create_interaction(
    conn: sqlite3.Connection,
    item_refs: list[ItemRef],
    call_sid: str | None = None,
) -> int:
    """Insert a new interaction row at the moment of dialing. Returns id."""
    cur = conn.execute(
        """INSERT INTO interactions (call_sid, started_at, status, item_refs, turns)
           VALUES (?, ?, 'started', ?, '[]')""",
        (call_sid, utcnow_iso(), dump_item_refs(item_refs)),
    )
    return int(cur.lastrowid)


def _row_to_interaction(row: sqlite3.Row) -> Interaction:
    return Interaction(
        id=row["id"],
        call_sid=row["call_sid"],
        started_at=row["started_at"],
        status=row["status"],
        item_refs=parse_item_refs(row["item_refs"]),
        turns=parse_turns(row["turns"]),
        journal_note=row["journal_note"],
        ended_at=row["ended_at"],
        duration_seconds=row["duration_seconds"],
        tokens_in=row["tokens_in"] or 0,
        tokens_out=row["tokens_out"] or 0,
        cost_usd=row["cost_usd"] or 0.0,
        error=row["error"],
    )


def get_interaction_by_call_sid(
    conn: sqlite3.Connection, call_sid: str,
) -> Interaction | None:
    row = conn.execute(
        "SELECT * FROM interactions WHERE call_sid = ?", (call_sid,),
    ).fetchone()
    return _row_to_interaction(row) if row else None


def get_interaction_by_id(conn: sqlite3.Connection, interaction_id: int) -> Interaction | None:
    row = conn.execute(
        "SELECT * FROM interactions WHERE id = ?", (interaction_id,),
    ).fetchone()
    return _row_to_interaction(row) if row else None


def attach_call_sid(conn: sqlite3.Connection, interaction_id: int, call_sid: str) -> None:
    """Link a started interaction to its Twilio CallSid once Twilio returns it."""
    conn.execute(
        "UPDATE interactions SET call_sid = ? WHERE id = ?",
        (call_sid, interaction_id),
    )


def append_turn(
    conn: sqlite3.Connection,
    interaction_id: int,
    turn: Turn,
    add_tokens_in: int = 0,
    add_tokens_out: int = 0,
    add_cost_usd: float = 0.0,
) -> None:
    """Append a turn and accumulate token/cost counters in one statement."""
    row = conn.execute(
        "SELECT turns FROM interactions WHERE id = ?", (interaction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"interaction {interaction_id} not found")
    turns = parse_turns(row["turns"])
    turns.append(turn)
    conn.execute(
        """UPDATE interactions
              SET turns = ?,
                  tokens_in = tokens_in + ?,
                  tokens_out = tokens_out + ?,
                  cost_usd = cost_usd + ?
            WHERE id = ?""",
        (dump_turns(turns), add_tokens_in, add_tokens_out, add_cost_usd, interaction_id),
    )


def update_last_turn_answer(
    conn: sqlite3.Connection,
    interaction_id: int,
    answer_text: str,
    action: str,
    add_tokens_in: int = 0,
    add_tokens_out: int = 0,
    add_cost_usd: float = 0.0,
) -> None:
    """Fill in the user's response on the most-recently-recorded turn."""
    row = conn.execute(
        "SELECT turns FROM interactions WHERE id = ?", (interaction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"interaction {interaction_id} not found")
    turns = parse_turns(row["turns"])
    if not turns:
        return
    turns[-1].answer_text = answer_text
    turns[-1].action = action
    conn.execute(
        """UPDATE interactions
              SET turns = ?,
                  tokens_in = tokens_in + ?,
                  tokens_out = tokens_out + ?,
                  cost_usd = cost_usd + ?
            WHERE id = ?""",
        (dump_turns(turns), add_tokens_in, add_tokens_out, add_cost_usd, interaction_id),
    )


def set_journal_note(conn: sqlite3.Connection, interaction_id: int, note: str) -> None:
    conn.execute(
        "UPDATE interactions SET journal_note = ? WHERE id = ?",
        (note, interaction_id),
    )


def finalize_interaction(
    conn: sqlite3.Connection,
    interaction_id: int,
    status: str,
    duration_seconds: int | None = None,
    error: str | None = None,
) -> None:
    """Mark the call done. status: 'completed' | 'failed' | 'abandoned'."""
    conn.execute(
        """UPDATE interactions
              SET status = ?, ended_at = ?, duration_seconds = ?, error = ?
            WHERE id = ?""",
        (status, utcnow_iso(), duration_seconds, error, interaction_id),
    )
