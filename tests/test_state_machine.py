"""State machine tests — Interaction.current_phase / current_item progression."""
from __future__ import annotations

from earshot.agent.state import Interaction, ItemRef, Phase, Turn


def _ix(item_refs: list[ItemRef], turns: list[Turn]) -> Interaction:
    return Interaction(
        id=1, call_sid=None, started_at="2026-06-11T00:00:00Z",
        status="started", item_refs=item_refs, turns=turns,
    )


def test_empty_interaction_starts_at_opener():
    ix = _ix([], [])
    assert ix.current_phase() == Phase.OPENER
    assert ix.current_item() is None


def test_opener_with_no_items_goes_to_closer():
    ix = _ix([], [_opener_turn("yes")])
    assert ix.current_phase() == Phase.CLOSER


def test_opener_with_items_goes_to_first_item():
    refs = [ItemRef("news", "1"), ItemRef("news", "2")]
    ix = _ix(refs, [_opener_turn("yes")])
    assert ix.current_phase() == Phase.ITEM
    assert ix.current_item().ref_id == "1"


def test_item_advances_to_next_item():
    refs = [ItemRef("news", "1"), ItemRef("news", "2"), ItemRef("news", "3")]
    ix = _ix(refs, [
        _opener_turn("yes"),
        _item_turn(refs[0], "add_to_study"),
    ])
    assert ix.current_phase() == Phase.ITEM
    assert ix.current_item().ref_id == "2"


def test_last_item_goes_to_closer():
    refs = [ItemRef("news", "1"), ItemRef("news", "2")]
    ix = _ix(refs, [
        _opener_turn("yes"),
        _item_turn(refs[0], "yes"),
        _item_turn(refs[1], "no"),
    ])
    assert ix.current_phase() == Phase.CLOSER
    assert ix.current_item() is None


def test_closer_goes_to_done():
    refs = [ItemRef("news", "1")]
    ix = _ix(refs, [
        _opener_turn("yes"),
        _item_turn(refs[0], "yes"),
        Turn(turn=3, phase=Phase.CLOSER.value, item_ref=None,
             question="journal?", answer_text="study MoE", action="captured"),
    ])
    assert ix.current_phase() == Phase.DONE


# --- helpers ---------------------------------------------------------------- #

def _opener_turn(action: str) -> Turn:
    return Turn(
        turn=1, phase=Phase.OPENER.value, item_ref=None,
        question="opener?", answer_text="answer", action=action,
    )


def _item_turn(ref: ItemRef, action: str) -> Turn:
    return Turn(
        turn=99, phase=Phase.ITEM.value, item_ref=ref,
        question="briefing + followup?", answer_text="answer", action=action,
    )
