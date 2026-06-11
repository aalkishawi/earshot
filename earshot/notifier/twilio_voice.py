"""Twilio TTS outbound-call notifier (v1.5).

Places an outbound call via the Twilio REST API with **inline TwiML** — no
webhook server required. The TwiML is built locally from a Claude-generated
voice script and passed as the ``twiml`` argument to ``calls.create``.

Cost shape: ~$0.014 per minute US outbound, plus ~$1/month for the
Twilio-issued FROM number. Polly voice is free with voice calls.

On trial Twilio accounts, the TO number must be in the **Verified Caller
IDs** list at console.twilio.com. We surface auth errors clearly so the
user knows what to fix.
"""
from __future__ import annotations

import logging
import re

import anthropic

from earshot.digest import DigestPayload
from earshot.notifier.base import AlertResult
from earshot.voice_digest import render_voice_script


log = logging.getLogger("earshot.notifier.twilio_voice")


# Strip anything that isn't a <break ... /> tag or plain text. We trust the
# voice-script generator but defense in depth — never let arbitrary XML out
# to TwiML.
_ALLOWED_TAG_RE = re.compile(r'<break\s+time="\d+(ms|s)"\s*/>', re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _sanitize_script(script: str) -> str:
    """Allow only <break time="..."/> tags; XML-escape everything else."""
    out_parts: list[str] = []
    pos = 0
    for m in _ANY_TAG_RE.finditer(script):
        # plain text before the tag
        out_parts.append(_escape(script[pos:m.start()]))
        tag = m.group(0)
        if _ALLOWED_TAG_RE.fullmatch(tag):
            out_parts.append(tag)
        # disallowed tags are dropped silently
        pos = m.end()
    out_parts.append(_escape(script[pos:]))
    return "".join(out_parts).strip()


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_twiml(script: str, voice: str) -> str:
    safe = _sanitize_script(script)
    # Voice attribute must also be sanitized — we accept only the Polly.X
    # form and fall back to Polly.Joanna if it looks malformed.
    voice_safe = voice if re.fullmatch(r"[A-Za-z0-9_.\-]+", voice or "") else "Polly.Joanna"
    return f'<Response><Say voice="{voice_safe}">{safe}</Say></Response>'


class TwilioTtsNotifier:
    channel = "voice"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        voice: str,
        anthropic_client: anthropic.Anthropic | None,
        defer_instant_to_v2: bool = False,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._to = to_number
        self._voice = voice
        self._anthropic = anthropic_client
        # When v2 (interactive) is also configured, v2 owns instant alerts and
        # v1.5 (this notifier) sticks to daily digests. Avoids double-dialing.
        self._defer_instant_to_v2 = defer_instant_to_v2

    def send(self, payload: DigestPayload) -> AlertResult:
        if self._defer_instant_to_v2 and payload.digest_type.startswith("instant_"):
            return AlertResult(
                status="skipped", channel=self.channel, recipient=self._to,
                error="instant alert handled by voice_interactive (v2)",
            )
        if self._anthropic is None:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error="voice notifier needs ANTHROPIC_API_KEY to render a voice script",
            )
        script_result = render_voice_script(payload, self._anthropic)
        if script_result.error or not script_result.script:
            err = script_result.error or "empty script"
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"voice script: {err}",
            )

        twiml = build_twiml(script_result.script, self._voice)

        try:
            from twilio.rest import Client
            from twilio.base.exceptions import TwilioRestException
        except ImportError as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"twilio SDK not installed: {e}",
            )

        try:
            client = Client(self._account_sid, self._auth_token)
            call = client.calls.create(
                twiml=twiml, to=self._to, from_=self._from,
            )
        except TwilioRestException as e:
            hint = ""
            # Twilio code 21219 = "To phone number not verified" on trial accts.
            if getattr(e, "code", None) == 21219:
                hint = (
                    " — trial accounts require the TO number to be verified at "
                    "console.twilio.com > Phone Numbers > Verified Caller IDs."
                )
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"Twilio error {getattr(e, 'code', '?')}: {e.msg if hasattr(e, 'msg') else e}{hint}",
            )
        except Exception as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"call create failed: {e}",
            )

        log.info("placed call sid=%s status=%s to=%s", call.sid, call.status, self._to)
        return AlertResult(
            status="sent", channel=self.channel, recipient=self._to,
        )

    def send_test(self, body: str = "This is your Earshot test call. If you hear this, voice delivery is working.") -> AlertResult:
        """Cheap credential check that doesn't need a DigestPayload or Claude."""
        twiml = build_twiml(body, self._voice)
        try:
            from twilio.rest import Client
            from twilio.base.exceptions import TwilioRestException
        except ImportError as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"twilio SDK not installed: {e}",
            )
        try:
            client = Client(self._account_sid, self._auth_token)
            call = client.calls.create(twiml=twiml, to=self._to, from_=self._from)
        except TwilioRestException as e:
            hint = ""
            if getattr(e, "code", None) == 21219:
                hint = (
                    " — trial accounts require the TO number to be verified at "
                    "console.twilio.com > Phone Numbers > Verified Caller IDs."
                )
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"Twilio error {getattr(e, 'code', '?')}: {e.msg if hasattr(e, 'msg') else e}{hint}",
            )
        except Exception as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"call create failed: {e}",
            )
        log.info("test call sid=%s status=%s to=%s", call.sid, call.status, self._to)
        return AlertResult(status="sent", channel=self.channel, recipient=self._to)
