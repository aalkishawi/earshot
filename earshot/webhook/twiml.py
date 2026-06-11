"""TwiML response builders.

Every spoken line is wrapped in ``<Say>``; user speech is captured by
``<Gather input="speech">``. Briefing + followup go in the SAME ``<Say>``
inside ONE ``<Gather>`` so we pay Twilio's per-use speech-recognition fee
once per item instead of twice.

Scripts here are read aloud by Polly. We never trust the raw script string
into XML — only ``<break time="..."/>`` tags pass through; everything else
is escaped.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode


_ALLOWED_TAG_RE = re.compile(r'<break\s+time="\d+(ms|s)"\s*/>', re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_script(script: str) -> str:
    """Drop every XML tag except ``<break time="..."/>``; escape the rest."""
    out: list[str] = []
    pos = 0
    for m in _ANY_TAG_RE.finditer(script or ""):
        out.append(_escape(script[pos:m.start()]))
        tag = m.group(0)
        if _ALLOWED_TAG_RE.fullmatch(tag):
            out.append(tag)
        pos = m.end()
    out.append(_escape((script or "")[pos:]))
    return "".join(out).strip()


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _safe_voice(voice: str) -> str:
    return voice if re.fullmatch(r"[A-Za-z0-9_.\-]+", voice or "") else "Polly.Joanna"


def gather_response(
    script: str,
    voice: str,
    action_url: str,
    speech_timeout: str = "auto",
    timeout_seconds: int = 5,
    speech_model: str = "phone_call",
) -> str:
    """``<Say>script</Say><Gather ...>`` with a fall-through ``<Redirect>``.

    The Redirect handles the silence case: Twilio falls through to it if
    the user never spoke. ``action_url`` should already include query
    params for routing (interaction_id, etc.).
    """
    safe = sanitize_script(script)
    voice_safe = _safe_voice(voice)
    timeout_url = _with_query(action_url, {"timeout": "true"})
    return (
        "<Response>"
        f'<Gather input="speech" timeout="{timeout_seconds}" '
        f'speechTimeout="{_escape(speech_timeout)}" '
        f'speechModel="{_escape(speech_model)}" '
        f'action="{_escape(action_url)}" method="POST">'
        f'<Say voice="{voice_safe}">{safe}</Say>'
        "</Gather>"
        f'<Redirect method="POST">{_escape(timeout_url)}</Redirect>'
        "</Response>"
    )


def say_and_hangup(script: str, voice: str) -> str:
    """Final TwiML — say a line, then hang up. Used at end of call or on error."""
    safe = sanitize_script(script)
    voice_safe = _safe_voice(voice)
    return f'<Response><Say voice="{voice_safe}">{safe}</Say><Hangup/></Response>'


def _with_query(url: str, extra: dict[str, str]) -> str:
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode(extra)
