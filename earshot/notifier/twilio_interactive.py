"""Twilio outbound interactive-call notifier (v2).

Dials the user and points Twilio at the webhook's ``/twiml/start`` endpoint
so the conversation runs turn-by-turn. Unlike ``TwilioTtsNotifier`` (v1.5)
this notifier does NOT render any script up front — the agent renders each
turn just-in-time inside the webhook.

Flow:
  1. Build a list of ``ItemRef`` from a payload's videos + news items.
  2. Persist a new ``interactions`` row capturing those refs.
  3. ``calls.create`` with ``url=`` pointing at ``/twiml/start?interaction_id=N``.
  4. Attach the returned ``CallSid`` to the row (also reattached by
     /twiml/start if it races).

Cost shape (per call): ~$0.014/min Twilio + $0.018/Gather turn + ~$0.001
LLM/turn. A 3-minute, 5-turn call lands ~$0.10–$0.14.
"""
from __future__ import annotations

import logging
import sqlite3

from earshot.agent import ItemRef, attach_call_sid, create_interaction
from earshot.digest import DigestPayload
from earshot.notifier.base import AlertResult


log = logging.getLogger("earshot.notifier.twilio_interactive")


def _payload_to_refs(payload: DigestPayload) -> list[ItemRef]:
    refs: list[ItemRef] = []
    for v in payload.videos:
        refs.append(ItemRef(kind="video", ref_id=v.video_id))
    for n in payload.news_items:
        refs.append(ItemRef(kind="news", ref_id=str(n.id)))
    return refs


def _webhook_reachable(webhook_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """GET {webhook_url}/healthz with a short timeout.

    Returns (True, "") on a 200 response, (False, reason) otherwise.
    The webhook's /healthz endpoint is intentionally trivial — no auth,
    no Twilio signature, just ``{"status": "ok"}``.
    """
    try:
        import requests
        r = requests.get(webhook_url.rstrip("/") + "/healthz", timeout=timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    return True, ""


class TwilioInteractiveNotifier:
    """Notifier that initiates an interactive call. Use for instant alerts or on-demand."""

    channel = "voice_interactive"

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        webhook_url: str,
    ) -> None:
        self._conn = conn
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._to = to_number
        # Strip trailing slash for clean URL composition.
        self._webhook_url = webhook_url.rstrip("/")

    def send(self, payload: DigestPayload) -> AlertResult:
        # v2 only owns instant alerts. Daily digests stay on v1.5 TTS.
        if not payload.digest_type.startswith("instant_"):
            return AlertResult(
                status="skipped", channel=self.channel, recipient=self._to,
                error="v2 only handles instant_* payloads",
            )

        refs = _payload_to_refs(payload)
        if not refs:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error="no item refs in payload to discuss",
            )

        # Pre-flight: make sure the webhook is actually reachable before we
        # burn Twilio minutes on a call that will land on "application
        # error" the moment the user presses through the trial preamble.
        # Aborting here costs nothing; placing a doomed call costs ~$0.05.
        reachable, why = _webhook_reachable(self._webhook_url)
        if not reachable:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"webhook not reachable at {self._webhook_url}: {why}",
            )

        interaction_id = create_interaction(self._conn, refs)

        try:
            from twilio.rest import Client
            from twilio.base.exceptions import TwilioRestException
        except ImportError as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"twilio SDK not installed: {e}",
            )

        twiml_url = f"{self._webhook_url}/twiml/start?interaction_id={interaction_id}"
        status_url = f"{self._webhook_url}/twiml/status?interaction_id={interaction_id}"

        try:
            client = Client(self._account_sid, self._auth_token)
            call = client.calls.create(
                url=twiml_url, to=self._to, from_=self._from,
                method="POST",
                status_callback=status_url,
                status_callback_method="POST",
                status_callback_event=["completed", "failed", "no-answer", "busy"],
            )
        except TwilioRestException as e:
            hint = ""
            if getattr(e, "code", None) == 21219:
                hint = (
                    " — trial accounts require the TO number to be verified at "
                    "console.twilio.com > Phone Numbers > Verified Caller IDs."
                )
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"Twilio error {getattr(e, 'code', '?')}: "
                      f"{e.msg if hasattr(e, 'msg') else e}{hint}",
            )
        except Exception as e:
            return AlertResult(
                status="failed", channel=self.channel, recipient=self._to,
                error=f"call create failed: {e}",
            )

        # Attach the CallSid; /twiml/start would also do it but doing it
        # here lets us correlate even if the call never connects.
        if call.sid:
            try:
                attach_call_sid(self._conn, interaction_id, call.sid)
            except Exception as e:
                log.warning("attach_call_sid failed (continuing): %s", e)

        log.info("interactive call sid=%s status=%s interaction_id=%d to=%s",
                 call.sid, call.status, interaction_id, self._to)
        return AlertResult(
            status="sent", channel=self.channel, recipient=self._to,
        )
