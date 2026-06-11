"""Agent store tests — interaction lifecycle persisted in SQLite."""
from __future__ import annotations

from earshot import agent as agent_mod


def test_create_then_get_interaction(tmp_db):
    refs = [agent_mod.ItemRef("news", "42"), agent_mod.ItemRef("video", "abc")]
    iid = agent_mod.create_interaction(tmp_db, refs)
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert ix is not None
    assert ix.status == "started"
    assert ix.call_sid is None
    assert [r.ref_id for r in ix.item_refs] == ["42", "abc"]
    assert ix.turns == []
    assert ix.cost_usd == 0.0


def test_attach_call_sid(tmp_db):
    iid = agent_mod.create_interaction(tmp_db, [agent_mod.ItemRef("news", "1")])
    agent_mod.attach_call_sid(tmp_db, iid, "CA123abc")
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert ix.call_sid == "CA123abc"
    by_sid = agent_mod.get_interaction_by_call_sid(tmp_db, "CA123abc")
    assert by_sid.id == iid


def test_append_turn_accumulates_cost(tmp_db):
    iid = agent_mod.create_interaction(tmp_db, [agent_mod.ItemRef("news", "1")])
    turn = agent_mod.Turn(turn=1, phase="opener", item_ref=None, question="hi?")
    agent_mod.append_turn(
        tmp_db, iid, turn,
        add_tokens_in=100, add_tokens_out=50, add_cost_usd=0.001,
    )
    agent_mod.append_turn(
        tmp_db, iid, agent_mod.Turn(turn=2, phase="item", item_ref=None, question="ok?"),
        add_tokens_in=200, add_tokens_out=80, add_cost_usd=0.002,
    )
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert len(ix.turns) == 2
    assert ix.tokens_in == 300
    assert ix.tokens_out == 130
    assert ix.cost_usd == pytest.approx(0.003)


def test_update_last_turn_answer(tmp_db):
    iid = agent_mod.create_interaction(tmp_db, [agent_mod.ItemRef("news", "1")])
    agent_mod.append_turn(
        tmp_db, iid,
        agent_mod.Turn(turn=1, phase="opener", item_ref=None, question="hi?"),
    )
    agent_mod.update_last_turn_answer(
        tmp_db, iid, answer_text="full story", action="full",
        add_tokens_in=20, add_tokens_out=10, add_cost_usd=0.0001,
    )
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert ix.turns[-1].answer_text == "full story"
    assert ix.turns[-1].action == "full"
    assert ix.tokens_in == 20


def test_journal_note_and_finalize(tmp_db):
    iid = agent_mod.create_interaction(tmp_db, [agent_mod.ItemRef("news", "1")])
    agent_mod.set_journal_note(tmp_db, iid, "remember MoE")
    agent_mod.finalize_interaction(
        tmp_db, iid, status="completed", duration_seconds=42,
    )
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert ix.journal_note == "remember MoE"
    assert ix.status == "completed"
    assert ix.duration_seconds == 42
    assert ix.ended_at is not None


# Late import so we can use pytest.approx inside test bodies.
import pytest  # noqa: E402
