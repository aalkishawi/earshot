"""Claude prompt builders for interactive-call turns.

One Claude call per turn, each capped at small token budgets. Costs land
~$0.0005–$0.002 per turn at Haiku 4.5 rates. Each builder uses tool_use
to force structured output, mirroring ``voice_digest.render_voice_script``.

Scripts here are short — every line is read aloud by Twilio's Polly voice
inside ``<Say>``. Twilio's ``<Gather>`` charges per use, so the followup
question goes in the SAME ``<Say>`` as the briefing — one prompt, one
gather, one charge per item.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from earshot.agent.state import ItemRef
from earshot.analyzer import CLAUDE_MODEL, calc_cost


log = logging.getLogger("earshot.agent.prompts")


# Small budgets — these are short conversational lines, not essays.
MAX_OPENER_TOKENS = 200
MAX_BRIEFING_TOKENS = 220
MAX_FOLLOWUP_TOKENS = 120
MAX_CLASSIFY_TOKENS = 100


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclass
class _UsageMixin:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class OpenerResult(_UsageMixin):
    script: str = ""


@dataclass
class BriefingResult(_UsageMixin):
    script: str = ""


@dataclass
class FollowupResult(_UsageMixin):
    script: str = ""
    intent: str = ""            # 'add_to_study' | 'skip_source' | 'surface_more' | 'draft_note' | 'open'
    expected_actions: list[str] = field(default_factory=list)


@dataclass
class ResponseClass(_UsageMixin):
    action: str = ""            # one of expected_actions, or 'unclear' / 'no'
    notes: str = ""


# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #

_OPENER_TOOL: dict[str, Any] = {
    "name": "submit_opener",
    "description": "Submit the call's opening script.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Spoken opener (~22 words). Greet, say how many items, "
                    "tease the top one in 6-8 words, then end with a "
                    "yes/no question like 'Ready to go through them?' "
                    "or 'Want the briefing?'."
                ),
            }
        },
        "required": ["script"],
    },
}

_BRIEFING_TOOL: dict[str, Any] = {
    "name": "submit_briefing",
    "description": "Submit a single-item spoken briefing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Spoken briefing for ONE item, ~30-40 words (≈15 "
                    "seconds). Lead with the concrete what; end with one "
                    "tight sentence of why it matters. No URLs, no "
                    "markdown. End with a natural pause — the followup "
                    "question is appended separately."
                ),
            }
        },
        "required": ["script"],
    },
}

_FOLLOWUP_TOOL: dict[str, Any] = {
    "name": "submit_followup",
    "description": "Submit one productivity-focused followup question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "One short spoken question, <=15 words, plain prose. "
                    "Examples: 'Add this concept to your study queue?' / "
                    "'Want fewer items from this source going forward?' / "
                    "'Should I draft a one-line note about this and email it?'"
                ),
            },
            "intent": {
                "type": "string",
                "enum": ["add_to_study", "skip_source", "surface_more", "draft_note", "open"],
                "description": "Which productivity action this question maps to.",
            },
            "expected_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The literal answer-classes you want to recognize from the "
                    "user. Always include 'yes', 'no', and any item-specific "
                    "option like 'more'. 2-4 items total."
                ),
            },
        },
        "required": ["script", "intent", "expected_actions"],
    },
}

_CLASSIFY_TOOL: dict[str, Any] = {
    "name": "classify_response",
    "description": "Map the user's spoken reply to one of the expected actions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Pick the closest match from expected_actions, or 'unclear' "
                    "if you cannot tell, or 'no' if the user clearly declined."
                ),
            },
            "notes": {
                "type": "string",
                "description": "<=20 words. Any qualitative reaction worth saving.",
            },
        },
        "required": ["action"],
    },
}


# --------------------------------------------------------------------------- #
# System prompts
# --------------------------------------------------------------------------- #

_OPENER_SYSTEM = """You write a SHORT spoken opener for an interactive phone briefing.
- Plain prose, ~22 words total. No markdown, no URLs.
- Greet briefly (no first names).
- State item count.
- Tease the top item in 6-8 words using a specific noun (a name, model, or concept).
- End with a yes/no question like "Ready to go through them?" or "Want the briefing?".
- The user will answer yes or no — do NOT offer 3-way choices.
Use the submit_opener tool."""

_BRIEFING_SYSTEM = """You write a 30-40 word spoken briefing for ONE item (~15 seconds).
- Plain prose. No URLs, no markdown, no SSML.
- First sentence: the concrete what. Use specific names/numbers/models.
- Second sentence: why it matters or what's new.
- Skip pleasantries — every word must earn its place.
- Do NOT ask a question — the followup is appended separately.
Use the submit_briefing tool."""

_FOLLOWUP_SYSTEM = """You write ONE productivity-focused followup question for the item just briefed.

Pick the intent that creates the MOST signal for future digests:
- 'add_to_study' for concepts/papers/tools the user might want to study later
- 'skip_source' if the item is from a source the user might want less of
- 'surface_more' if the user might want more like this
- 'draft_note' to offer a quick one-line takeaway emailed for later reference
- 'open' for an open-ended qualitative reaction (use sparingly; only when item is unusually striking)

The question must be <=15 words, spoken, no jargon. Always provide a small list of expected_actions (~2-4) including 'yes' and 'no' plus any natural variants.

Use the submit_followup tool."""

_CLASSIFY_SYSTEM = """Map a spoken phone reply to one of the expected actions.
- The transcript came from Twilio speech recognition. It may have small errors.
- Be generous about colloquial yes/no ("sure", "go ahead", "nah", "skip it").
- If the user said something off-topic but expressive (e.g. a reaction), set action='unclear' and capture the reaction in notes.
- If silence or "I don't know", action='unclear'.
Use the classify_response tool."""


# --------------------------------------------------------------------------- #
# Render functions
# --------------------------------------------------------------------------- #

def render_opener(
    client: anthropic.Anthropic,
    items: list[dict[str, Any]],
) -> OpenerResult:
    """``items`` is a list of dicts with keys: kind, title, source/channel, score (opt)."""
    lines = [f"Items in this call: {len(items)}"]
    for i, it in enumerate(items, 1):
        src = it.get("source") or it.get("channel") or "?"
        score = it.get("score")
        score_s = f" [score {score}]" if score is not None else ""
        lines.append(f"{i}. ({it.get('kind','?')}) {src}: {it.get('title','')[:120]}{score_s}")
    return _call_for_text(
        client, system=_OPENER_SYSTEM, user="\n".join(lines),
        tool=_OPENER_TOOL, max_tokens=MAX_OPENER_TOKENS, _result_cls=OpenerResult,
    )


def render_briefing(
    client: anthropic.Anthropic,
    item: dict[str, Any],
) -> BriefingResult:
    """``item`` keys depend on kind:
       video → title, channel, summary, takeaways (list), concepts (list of dicts)
       news  → title, source, summary
    """
    lines = [f"Item kind: {item.get('kind','?')}"]
    lines.append(f"Title: {item.get('title','')}")
    if item.get("source"):
        lines.append(f"Source: {item['source']}")
    if item.get("channel"):
        lines.append(f"Channel: {item['channel']}")
    if item.get("summary"):
        lines.append(f"Summary: {item['summary']}")
    if item.get("takeaways"):
        lines.append("Key takeaways:")
        for t in item["takeaways"][:4]:
            lines.append(f"- {t}")
    if item.get("concepts"):
        terms = ", ".join(c.get("term", "") for c in item["concepts"][:4] if c.get("term"))
        if terms:
            lines.append(f"New concepts: {terms}")
    return _call_for_text(
        client, system=_BRIEFING_SYSTEM, user="\n".join(lines),
        tool=_BRIEFING_TOOL, max_tokens=MAX_BRIEFING_TOKENS, _result_cls=BriefingResult,
    )


def render_followup(
    client: anthropic.Anthropic,
    item: dict[str, Any],
) -> FollowupResult:
    user = (
        f"Item kind: {item.get('kind','?')}\n"
        f"Title: {item.get('title','')}\n"
        f"Source/channel: {item.get('source') or item.get('channel') or '?'}\n"
        f"Has new concepts: {bool(item.get('concepts'))}"
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_FOLLOWUP_TOKENS,
            system=_FOLLOWUP_SYSTEM,
            tools=[_FOLLOWUP_TOOL],
            tool_choice={"type": "tool", "name": "submit_followup"},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        return FollowupResult(error=f"followup generation failed: {e}")

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_followup":
            inp = block.input or {}
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            actions = inp.get("expected_actions") or []
            if not isinstance(actions, list):
                actions = []
            return FollowupResult(
                script=str(inp.get("script") or "").strip(),
                intent=str(inp.get("intent") or "open"),
                expected_actions=[str(a) for a in actions if a],
                input_tokens=in_tok, output_tokens=out_tok,
                cost_usd=calc_cost(in_tok, out_tok),
            )
    return FollowupResult(error=f"no tool_use in response (stop_reason={resp.stop_reason})")


def classify_response(
    client: anthropic.Anthropic,
    transcript: str,
    expected_actions: list[str],
) -> ResponseClass:
    if not transcript.strip():
        return ResponseClass(action="unclear")
    user = (
        f"User said: \"{transcript.strip()}\"\n"
        f"Expected actions: {expected_actions}"
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_CLASSIFY_TOKENS,
            system=_CLASSIFY_SYSTEM,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_response"},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        return ResponseClass(error=f"classify failed: {e}")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_response":
            inp = block.input or {}
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            return ResponseClass(
                action=str(inp.get("action") or "unclear"),
                notes=str(inp.get("notes") or ""),
                input_tokens=in_tok, output_tokens=out_tok,
                cost_usd=calc_cost(in_tok, out_tok),
            )
    return ResponseClass(error=f"no tool_use in response (stop_reason={resp.stop_reason})")


# --------------------------------------------------------------------------- #
# Shared call helper for text-only tools
# --------------------------------------------------------------------------- #

def _call_for_text(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    tool: dict[str, Any],
    max_tokens: int,
    _result_cls,
):
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        return _result_cls(error=f"{tool['name']} failed: {e}")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
            inp = block.input or {}
            in_tok = resp.usage.input_tokens
            out_tok = resp.usage.output_tokens
            return _result_cls(
                script=str(inp.get("script") or "").strip(),
                input_tokens=in_tok, output_tokens=out_tok,
                cost_usd=calc_cost(in_tok, out_tok),
            )
    return _result_cls(error=f"no tool_use in response (stop_reason={resp.stop_reason})")
