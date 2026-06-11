"""FastAPI app driving the v2 interactive call.

Three POST endpoints Twilio hits over the public webhook URL:

  * ``/twiml/start?interaction_id=N`` — first hit when the call answers.
    Renders the opener and returns a ``<Gather>`` pointing back at us.
  * ``/twiml/turn?interaction_id=N&phase=P&exp=a,b,c`` — every subsequent
    Gather result. ``phase`` says what the user just answered (opener |
    item | closer); ``exp`` is the comma-separated list of expected
    actions for classification.
  * ``/twiml/status?interaction_id=N`` — Twilio call-status callback.
    Finalizes the interaction row when the call ends.

Cost caps live here, not in the prompts: each turn checks call duration +
turn count and short-circuits to a graceful hang-up if either is exceeded.

Stateless between requests — interaction state is persisted in SQLite and
re-read every hit.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from earshot import agent as agent_mod
from earshot import db as db_mod
from earshot.analyzer import make_client
from earshot.config import Config
from earshot.webhook.twiml import gather_response, say_and_hangup


log = logging.getLogger("earshot.webhook")

XML_CT = "application/xml"

_GOODBYE = "That's it for today. Take care."
_GOODBYE_EARLY = "We've hit your time limit. I'll save what we have. Take care."
_GOODBYE_ERROR = "I'm having trouble. Saving what we have. Goodbye."
_CLOSER_QUESTION = (
    "One last thing. Anything from today you want to remember? "
    "Speak it now and I'll save it as a journal note. Or stay silent to skip."
)


def create_app(cfg: Config) -> FastAPI:
    if not cfg.webhook_url:
        raise RuntimeError(
            "EARSHOT_WEBHOOK_URL is required (the public base URL Twilio hits, "
            "e.g. https://abc.ngrok-free.app)"
        )
    client = make_client(cfg)
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required for the webhook service")

    # Local import to avoid hard dep at module import time.
    validator = None
    if cfg.webhook_validate_signature:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(cfg.twilio_auth_token or "")

    app = FastAPI(title="Earshot Webhook", version="2.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/twiml/start")
    async def start(request: Request, interaction_id: int) -> PlainTextResponse:
        body = await _form(request)
        _verify_signature(request, body, cfg, validator)

        conn = db_mod.connect(cfg.db_path)
        ix = agent_mod.get_interaction_by_id(conn, interaction_id)
        if ix is None:
            raise HTTPException(404, f"interaction {interaction_id} not found")

        # Attach CallSid (Twilio only provides it once the call is up).
        call_sid = body.get("CallSid")
        if call_sid and ix.call_sid is None:
            agent_mod.attach_call_sid(conn, interaction_id, call_sid)

        # Render opener.
        items_meta = [_item_meta(conn, ref) for ref in ix.item_refs]
        opener = agent_mod.render_opener(client, items_meta)
        if opener.error:
            log.warning("opener render failed: %s", opener.error)
            return _xml(say_and_hangup(_GOODBYE_ERROR, cfg.twilio_voice))

        # Record the OPENER turn with the question; answer/action fill in next hit.
        turn = agent_mod.Turn(
            turn=1, phase=agent_mod.Phase.OPENER.value, item_ref=None,
            question=opener.script,
        )
        agent_mod.append_turn(
            conn, interaction_id, turn,
            add_tokens_in=opener.input_tokens,
            add_tokens_out=opener.output_tokens,
            add_cost_usd=opener.cost_usd,
        )

        action_url = _action_url(cfg.webhook_url, interaction_id, "opener", ["yes", "no"])
        return _xml(gather_response(opener.script, cfg.twilio_voice, action_url))

    @app.post("/twiml/turn")
    async def turn(
        request: Request,
        interaction_id: int,
        phase: str,
        exp: str = "",
    ) -> PlainTextResponse:
        body = await _form(request)
        _verify_signature(request, body, cfg, validator)

        conn = db_mod.connect(cfg.db_path)
        ix = agent_mod.get_interaction_by_id(conn, interaction_id)
        if ix is None:
            raise HTTPException(404, f"interaction {interaction_id} not found")

        # Cost / duration caps.
        if _over_limits(ix, cfg):
            agent_mod.finalize_interaction(
                conn, interaction_id, status="completed",
                duration_seconds=_elapsed_seconds(ix.started_at),
            )
            return _xml(say_and_hangup(_GOODBYE_EARLY, cfg.twilio_voice))

        speech = (body.get("SpeechResult") or "").strip()
        timeout_flag = (body.get("timeout") == "true") or (not speech)

        # 1. Fill in the previous turn with the user's response.
        expected = [a for a in exp.split(",") if a]
        if phase == agent_mod.Phase.CLOSER.value:
            # Free-text journal capture — no classification, save verbatim.
            agent_mod.set_journal_note(conn, interaction_id, speech)
            agent_mod.update_last_turn_answer(
                conn, interaction_id,
                answer_text=speech, action="captured" if speech else "skipped",
            )
            agent_mod.finalize_interaction(
                conn, interaction_id, status="completed",
                duration_seconds=_elapsed_seconds(ix.started_at),
            )
            return _xml(say_and_hangup(_GOODBYE, cfg.twilio_voice))

        if timeout_flag:
            # Silence — treat as 'no' to make forward progress.
            agent_mod.update_last_turn_answer(
                conn, interaction_id, answer_text="", action="no",
            )
        else:
            cls = agent_mod.classify_response(client, speech, expected)
            agent_mod.update_last_turn_answer(
                conn, interaction_id, answer_text=speech, action=cls.action or "unclear",
                add_tokens_in=cls.input_tokens, add_tokens_out=cls.output_tokens,
                add_cost_usd=cls.cost_usd,
            )

        # Reload to reflect the just-written turn.
        ix = agent_mod.get_interaction_by_id(conn, interaction_id)
        last_action = ix.turns[-1].action if ix.turns else "no"

        # 2. Decide next prompt.
        if phase == agent_mod.Phase.OPENER.value:
            if last_action == "no" or not ix.item_refs:
                return _render_closer(cfg, interaction_id)
            return _render_item(client, conn, cfg, ix, interaction_id)

        if phase == agent_mod.Phase.ITEM.value:
            next_phase = ix.current_phase()
            if next_phase == agent_mod.Phase.CLOSER:
                return _render_closer(cfg, interaction_id)
            if next_phase == agent_mod.Phase.ITEM:
                return _render_item(client, conn, cfg, ix, interaction_id)
            return _xml(say_and_hangup(_GOODBYE, cfg.twilio_voice))

        # Unknown phase — defensive.
        return _xml(say_and_hangup(_GOODBYE, cfg.twilio_voice))

    @app.post("/twiml/status")
    async def status(request: Request, interaction_id: int) -> Response:
        body = await _form(request)
        _verify_signature(request, body, cfg, validator)

        call_status = body.get("CallStatus", "unknown")
        duration = int(body.get("CallDuration", "0") or "0")
        conn = db_mod.connect(cfg.db_path)
        ix = agent_mod.get_interaction_by_id(conn, interaction_id)
        if ix is None or ix.ended_at:
            return Response(status_code=204)
        if call_status == "completed":
            agent_mod.finalize_interaction(
                conn, interaction_id, status="completed",
                duration_seconds=duration,
            )
        else:
            agent_mod.finalize_interaction(
                conn, interaction_id, status="failed",
                duration_seconds=duration,
                error=f"twilio call_status={call_status}",
            )
        return Response(status_code=204)

    return app


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def _render_item(
    client: anthropic.Anthropic,
    conn: sqlite3.Connection,
    cfg: Config,
    ix: agent_mod.Interaction,
    interaction_id: int,
) -> PlainTextResponse:
    """Render briefing + followup as ONE Say in ONE Gather (cost optimization)."""
    item_ref = ix.current_item()
    if item_ref is None:
        return _render_closer(cfg, interaction_id)

    content = _load_item_content(conn, item_ref)
    if content is None:
        # Missing item — record a placeholder turn and advance.
        agent_mod.append_turn(
            conn, interaction_id,
            agent_mod.Turn(
                turn=len(ix.turns) + 1, phase=agent_mod.Phase.ITEM.value,
                item_ref=item_ref, question="(item content missing)",
                answer_text="", action="missing",
            ),
        )
        ix = agent_mod.get_interaction_by_id(conn, interaction_id)
        if ix.current_phase() == agent_mod.Phase.CLOSER:
            return _render_closer(cfg, interaction_id)
        return _render_item(client, conn, cfg, ix, interaction_id)

    briefing = agent_mod.render_briefing(client, content)
    followup = agent_mod.render_followup(client, content)
    if briefing.error or followup.error:
        log.warning("item render failed: briefing=%s followup=%s",
                    briefing.error, followup.error)
        return _xml(say_and_hangup(_GOODBYE_ERROR, cfg.twilio_voice))

    combined = briefing.script + ' <break time="400ms"/> ' + followup.script
    expected = followup.expected_actions or ["yes", "no"]

    agent_mod.append_turn(
        conn, interaction_id,
        agent_mod.Turn(
            turn=len(ix.turns) + 1, phase=agent_mod.Phase.ITEM.value,
            item_ref=item_ref, question=combined,
        ),
        add_tokens_in=briefing.input_tokens + followup.input_tokens,
        add_tokens_out=briefing.output_tokens + followup.output_tokens,
        add_cost_usd=briefing.cost_usd + followup.cost_usd,
    )

    action_url = _action_url(cfg.webhook_url, interaction_id, "item", expected)
    return _xml(gather_response(combined, cfg.twilio_voice, action_url))


def _render_closer(cfg: Config, interaction_id: int) -> PlainTextResponse:
    """Static closer (no LLM cost). Single Gather captures the journal note."""
    # Inject the OPENER->CLOSER (skip path) doesn't write a CLOSER turn here;
    # the actual recording happens at the /twiml/turn fill-in step. But we
    # still need a question row to update. Append one now.
    conn = db_mod.connect(cfg.db_path)
    ix = agent_mod.get_interaction_by_id(conn, interaction_id)
    if ix and (not ix.turns or ix.turns[-1].phase != agent_mod.Phase.CLOSER.value
               or ix.turns[-1].answer_text is not None):
        agent_mod.append_turn(
            conn, interaction_id,
            agent_mod.Turn(
                turn=len(ix.turns) + 1, phase=agent_mod.Phase.CLOSER.value,
                item_ref=None, question=_CLOSER_QUESTION,
            ),
        )

    action_url = _action_url(cfg.webhook_url, interaction_id, "closer", [])
    return _xml(gather_response(
        _CLOSER_QUESTION, cfg.twilio_voice, action_url,
        speech_timeout="3",  # let the user pause to think
        timeout_seconds=8,
    ))


# --------------------------------------------------------------------------- #
# Item loading — produces the dict shape prompts.py expects
# --------------------------------------------------------------------------- #

def _item_meta(conn: sqlite3.Connection, ref: agent_mod.ItemRef) -> dict[str, Any]:
    """Lightweight metadata for the opener (no body text)."""
    if ref.kind == "video":
        row = conn.execute(
            """SELECT v.title, c.handle, c.name FROM videos v
                 JOIN channels c ON c.channel_id = v.channel_id
                WHERE v.video_id = ?""",
            (ref.ref_id,),
        ).fetchone()
        if not row:
            return {"kind": "video", "title": "(missing)", "channel": "?"}
        return {"kind": "video", "title": row["title"] or "", "channel": row["handle"] or row["name"] or "?"}
    if ref.kind == "news":
        row = conn.execute(
            "SELECT title, source, importance_score FROM news_items WHERE id = ?",
            (int(ref.ref_id),),
        ).fetchone()
        if not row:
            return {"kind": "news", "title": "(missing)", "source": "?"}
        return {
            "kind": "news", "title": row["title"] or "",
            "source": row["source"] or "?",
            "score": row["importance_score"],
        }
    return {"kind": ref.kind, "title": "(unknown)"}


def _load_item_content(conn: sqlite3.Connection, ref: agent_mod.ItemRef) -> dict[str, Any] | None:
    """Full content for briefing/followup prompts."""
    if ref.kind == "video":
        row = conn.execute(
            """SELECT v.video_id, v.title, v.summary_json, c.handle, c.name
                 FROM videos v JOIN channels c ON c.channel_id = v.channel_id
                WHERE v.video_id = ?""",
            (ref.ref_id,),
        ).fetchone()
        if not row:
            return None
        a = json.loads(row["summary_json"]) if row["summary_json"] else {}
        concepts = [
            dict(r) for r in conn.execute(
                """SELECT term, category, definition, why_it_matters
                     FROM glossary WHERE first_seen_video_id = ?
                    ORDER BY category, term""",
                (ref.ref_id,),
            ).fetchall()
        ]
        return {
            "kind": "video",
            "title": row["title"] or "",
            "channel": row["handle"] or row["name"] or "?",
            "summary": str(a.get("summary") or ""),
            "takeaways": list(a.get("key_takeaways") or []),
            "concepts": concepts,
        }
    if ref.kind == "news":
        row = conn.execute(
            "SELECT title, source, summary, importance_score FROM news_items WHERE id = ?",
            (int(ref.ref_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "kind": "news",
            "title": row["title"] or "",
            "source": row["source"] or "?",
            "summary": row["summary"] or "",
            "score": row["importance_score"],
        }
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _xml(body: str) -> PlainTextResponse:
    return PlainTextResponse(content=body, media_type=XML_CT)


def _action_url(base: str, interaction_id: int, phase: str, expected: list[str]) -> str:
    exp = ",".join(expected) if expected else ""
    url = f"{base}/twiml/turn?interaction_id={interaction_id}&phase={phase}"
    if exp:
        url += f"&exp={exp}"
    return url


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _verify_signature(
    request: Request, body: dict[str, str], cfg: Config, validator,
) -> None:
    if validator is None:
        return
    signature = request.headers.get("X-Twilio-Signature", "")
    # Twilio signs the exact URL it called — reconstruct from cfg.webhook_url
    # + path + query so signing matches even when ngrok terminates TLS.
    url = (cfg.webhook_url or "").rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not validator.validate(url, body, signature):
        raise HTTPException(status_code=403, detail="bad twilio signature")


def _elapsed_seconds(started_at_iso: str) -> int:
    try:
        started = datetime.strptime(started_at_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - started).total_seconds())


def _over_limits(ix: agent_mod.Interaction, cfg: Config) -> bool:
    if len(ix.turns) >= cfg.webhook_max_turns:
        return True
    if _elapsed_seconds(ix.started_at) >= cfg.webhook_max_call_seconds:
        return True
    return False
