"""Tests for the new replay-tooling store helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from earshot import agent as agent_mod


def test_list_recent_interactions_orders_by_started_desc(tmp_db):
    refs = [agent_mod.ItemRef("news", "1")]
    id1 = agent_mod.create_interaction(tmp_db, refs)
    # Backdate id1 so id2 should be newer.
    tmp_db.execute(
        "UPDATE interactions SET started_at = ? WHERE id = ?",
        ("2026-06-10T00:00:00Z", id1),
    )
    id2 = agent_mod.create_interaction(tmp_db, refs)
    tmp_db.execute(
        "UPDATE interactions SET started_at = ? WHERE id = ?",
        ("2026-06-11T12:00:00Z", id2),
    )

    rows = agent_mod.list_recent_interactions(tmp_db, limit=5)
    assert [r.id for r in rows] == [id2, id1]


def test_list_recent_interactions_status_filter(tmp_db):
    refs = [agent_mod.ItemRef("news", "1")]
    id1 = agent_mod.create_interaction(tmp_db, refs)
    id2 = agent_mod.create_interaction(tmp_db, refs)
    agent_mod.finalize_interaction(tmp_db, id2, status="completed", duration_seconds=30)

    completed = agent_mod.list_recent_interactions(tmp_db, status_filter="completed")
    assert [r.id for r in completed] == [id2]
    started = agent_mod.list_recent_interactions(tmp_db, status_filter="started")
    assert [r.id for r in started] == [id1]


def test_sweep_marks_old_started_rows_abandoned(tmp_db):
    refs = [agent_mod.ItemRef("news", "1")]
    old_id = agent_mod.create_interaction(tmp_db, refs)
    # Backdate to two hours ago.
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_db.execute(
        "UPDATE interactions SET started_at = ? WHERE id = ?",
        (two_hours_ago, old_id),
    )
    fresh_id = agent_mod.create_interaction(tmp_db, refs)  # just now

    swept = agent_mod.sweep_orphaned_interactions(tmp_db, older_than_seconds=3600)
    assert swept == 1
    old = agent_mod.get_interaction_by_id(tmp_db, old_id)
    assert old.status == "abandoned"
    assert old.ended_at is not None
    assert "no status callback" in (old.error or "")
    fresh = agent_mod.get_interaction_by_id(tmp_db, fresh_id)
    assert fresh.status == "started"


def test_sweep_does_not_touch_finalized(tmp_db):
    refs = [agent_mod.ItemRef("news", "1")]
    iid = agent_mod.create_interaction(tmp_db, refs)
    agent_mod.finalize_interaction(tmp_db, iid, status="completed", duration_seconds=10)
    # Backdate so the row is "old" — but it's already finalized.
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_db.execute(
        "UPDATE interactions SET started_at = ? WHERE id = ?",
        (two_hours_ago, iid),
    )
    swept = agent_mod.sweep_orphaned_interactions(tmp_db, older_than_seconds=3600)
    assert swept == 0
    ix = agent_mod.get_interaction_by_id(tmp_db, iid)
    assert ix.status == "completed"
