"""Interactive call agent (v2).

Drives an outbound conversational call via Twilio ``<Gather>`` turns and
Claude Haiku. Pure-data and prompt-building live here; telephony wiring
lives in ``earshot.webhook`` and ``earshot.notifier.twilio_interactive``.
"""
from earshot.agent.state import (
    ItemRef, Phase, Turn, Interaction,
    parse_item_refs, parse_turns,
)
from earshot.agent.prompts import (
    OpenerResult, BriefingResult, FollowupResult, ResponseClass,
    render_opener, render_briefing, render_followup,
    classify_response,
)
from earshot.agent.store import (
    create_interaction, get_interaction_by_call_sid, get_interaction_by_id,
    attach_call_sid, append_turn, update_last_turn_answer,
    set_journal_note, finalize_interaction,
)

__all__ = [
    "ItemRef", "Phase", "Turn", "Interaction",
    "parse_item_refs", "parse_turns",
    "OpenerResult", "BriefingResult", "FollowupResult", "ResponseClass",
    "render_opener", "render_briefing", "render_followup",
    "classify_response",
    "create_interaction", "get_interaction_by_call_sid", "get_interaction_by_id",
    "attach_call_sid", "append_turn", "update_last_turn_answer",
    "set_journal_note", "finalize_interaction",
]
