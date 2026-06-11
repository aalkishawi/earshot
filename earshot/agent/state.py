"""Conversation state for interactive calls.

The source of truth lives in the ``interactions`` table; this module
provides typed views over what's stored as JSON in ``item_refs`` and
``turns``.

Phase progression: opener → item per ref → closer → done.

Each item turn bundles the briefing + followup question into one
``<Gather>`` cycle, because Twilio bills speech recognition per use —
splitting them would double the per-item cost for no UX gain.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Phase(str, Enum):
    OPENER = "opener"
    ITEM = "item"          # briefing + followup in a single Gather cycle
    CLOSER = "closer"
    DONE = "done"


@dataclass
class ItemRef:
    """Lightweight handle to a digest item — full content fetched lazily."""
    kind: str        # 'video' | 'news'
    ref_id: str      # video_id (TEXT) or news_items.id (stringified)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref_id": self.ref_id}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ItemRef":
        return cls(kind=str(d["kind"]), ref_id=str(d["ref_id"]))


@dataclass
class Turn:
    """One agent-question + user-response pair."""
    turn: int
    phase: str
    item_ref: ItemRef | None
    question: str
    answer_text: str | None = None
    action: str | None = None     # classified intent (e.g. 'add_to_study', 'skip')

    def to_json(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "phase": self.phase,
            "item_ref": self.item_ref.to_json() if self.item_ref else None,
            "question": self.question,
            "answer_text": self.answer_text,
            "action": self.action,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Turn":
        ir = d.get("item_ref")
        return cls(
            turn=int(d["turn"]),
            phase=str(d["phase"]),
            item_ref=ItemRef.from_json(ir) if ir else None,
            question=str(d.get("question") or ""),
            answer_text=d.get("answer_text"),
            action=d.get("action"),
        )


@dataclass
class Interaction:
    """In-memory view of an ``interactions`` row."""
    id: int
    call_sid: str | None
    started_at: str
    status: str
    item_refs: list[ItemRef]
    turns: list[Turn]
    journal_note: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None

    def current_phase(self) -> Phase:
        """Next phase to enter, based on what's already happened."""
        if not self.turns:
            return Phase.OPENER

        completed_items = sum(1 for t in self.turns if t.phase == Phase.ITEM.value)
        last = self.turns[-1]

        if last.phase == Phase.OPENER.value:
            return Phase.ITEM if self.item_refs else Phase.CLOSER
        if last.phase == Phase.ITEM.value:
            if completed_items >= len(self.item_refs):
                return Phase.CLOSER
            return Phase.ITEM
        if last.phase == Phase.CLOSER.value:
            return Phase.DONE
        return Phase.DONE

    def current_item(self) -> ItemRef | None:
        """The next item to brief (None at opener/closer)."""
        if not self.item_refs:
            return None
        completed = sum(1 for t in self.turns if t.phase == Phase.ITEM.value)
        if completed >= len(self.item_refs):
            return None
        return self.item_refs[completed]


def parse_item_refs(s: str | None) -> list[ItemRef]:
    if not s:
        return []
    return [ItemRef.from_json(x) for x in json.loads(s)]


def parse_turns(s: str | None) -> list[Turn]:
    if not s:
        return []
    return [Turn.from_json(x) for x in json.loads(s)]


def dump_item_refs(refs: list[ItemRef]) -> str:
    return json.dumps([r.to_json() for r in refs], ensure_ascii=False)


def dump_turns(turns: list[Turn]) -> str:
    return json.dumps([t.to_json() for t in turns], ensure_ascii=False)
