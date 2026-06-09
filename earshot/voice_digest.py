"""Voice-tailored digest scripts.

The email digest is long and link-dense — that's good for reading and bad
for listening. This module asks Claude to rewrite a ``DigestPayload`` into a
~90-second spoken script with SSML pauses, no URLs, and conversational
phrasing. The output drops into ``<Say>`` inside TwiML.

Cost per call: ~$0.005 (typically <500 tokens in, <300 tokens out).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from earshot.analyzer import (
    CLAUDE_MODEL, MAX_OUTPUT_TOKENS, AnalyzerError, calc_cost,
)
from earshot.digest import DigestPayload


log = logging.getLogger("earshot.voice_digest")


VOICE_SCRIPT_TOOL: dict[str, Any] = {
    "name": "submit_voice_script",
    "description": "Submit the spoken-word digest script.",
    "input_schema": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Plain-text spoken script. May include SSML break tags "
                    "like <break time=\"500ms\"/> between sections. NO URLs, "
                    "no markdown, no XML other than break tags."
                ),
            }
        },
        "required": ["script"],
    },
}


SYSTEM_PROMPT = """You are turning a written digest into a 90-second SPOKEN briefing for the listener to hear over the phone.

CRITICAL RULES:
- Output must be a single plain-text script. Use the `submit_voice_script` tool.
- Length target: ~140 words (≈90 seconds at conversational pace). Hard cap: 180 words.
- NO URLs, no markdown, no formatting.
- Conversational tone. Imagine briefing a busy executive in the car.
- Mention specific names, models, concepts — those are the listenable signal. Skip generic phrasing.
- Use SSML break tags ONLY for pauses between sections: <break time="500ms"/> for short, <break time="1s"/> for major.
- Do NOT include any other XML tags or formatting.

STRUCTURE for a DAILY digest:
1. Opening: "Good [morning/afternoon]. Here's your Earshot briefing for [date]."
2. Episodes: one sentence per episode, channel + most interesting takeaway. Skip if no episodes.
3. News: 2-3 most important items. Mention source briefly.
4. Closing: "Full details in your email. Have a good [day/evening]."

STRUCTURE for an INSTANT alert:
1. "This is an Earshot alert."
2. The single item, briefly — what + why it matters.
3. "Details in your email."

If the digest contains nothing worth talking about, return a minimal script that says so politely."""


@dataclass
class VoiceScriptResult:
    script: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str | None = None


def render_voice_script(
    payload: DigestPayload,
    client: anthropic.Anthropic,
) -> VoiceScriptResult:
    user_message = _build_user_message(payload)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[VOICE_SCRIPT_TOOL],
            tool_choice={"type": "tool", "name": "submit_voice_script"},
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        return VoiceScriptResult(
            script="", input_tokens=0, output_tokens=0, cost_usd=0.0,
            error=f"voice script generation failed: {e}",
        )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_voice_script":
            script = str(block.input.get("script") or "").strip()
            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            cost = calc_cost(in_tok, out_tok)
            if not script:
                return VoiceScriptResult(
                    script="", input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
                    error="Claude returned an empty script",
                )
            return VoiceScriptResult(
                script=script, input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            )
    return VoiceScriptResult(
        script="", input_tokens=0, output_tokens=0, cost_usd=0.0,
        error=f"Claude did not call submit_voice_script (stop_reason={response.stop_reason})",
    )


def _build_user_message(payload: DigestPayload) -> str:
    lines: list[str] = []
    lines.append(f"Digest type: {payload.digest_type}")
    lines.append(f"Date: {payload.date_local}")
    lines.append(f"Subject: {payload.subject}")
    lines.append("")

    if payload.videos:
        lines.append(f"=== EPISODES ({len(payload.videos)}) ===")
        for v in payload.videos:
            prio = " [PRIORITY]" if v.is_priority else ""
            lines.append(f"\n## {v.channel_handle}: {v.title}{prio}")
            if v.summary:
                lines.append(f"Summary: {v.summary}")
            if v.key_takeaways:
                lines.append("Key takeaways:")
                for t in v.key_takeaways[:5]:
                    lines.append(f"- {t}")
            if v.new_concepts:
                concept_terms = ", ".join(c["term"] for c in v.new_concepts[:5])
                lines.append(f"New concepts: {concept_terms}")
        lines.append("")

    if payload.news_items:
        lines.append(f"=== NEWS ({len(payload.news_items)}) ===")
        for n in payload.news_items[:8]:
            lines.append(f"- [{n.importance_score}] {n.source}: {n.title}")
        lines.append("")

    return "\n".join(lines)
