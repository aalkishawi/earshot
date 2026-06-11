"""Notifier package.

Pluggable delivery layer. The active notifier set is chosen by
``make_notifiers``:
  * dry_run=True → ``[StdoutNotifier]``
  * email creds present → adds ``EmailYahooNotifier``
  * Twilio + Anthropic creds present → adds ``TwilioTtsNotifier`` (v1.5)
  * Twilio + Anthropic + webhook URL present → adds
    ``TwilioInteractiveNotifier`` (v2), and v1.5 starts skipping instant_*
    payloads so they don't double-dial
  * if neither configured → falls back to ``[StdoutNotifier]``

A single ``digest`` run iterates every notifier in the list; items are
marked notified as long as *at least one* channel succeeded. A voice-only
failure with email success still flips the row to ``notified`` — the user
got the email, we don't want to redeliver.

``status='skipped'`` is treated as a no-op (not success, not failure) —
the fan-out doesn't record an alert row or count it toward delivery.
"""
from __future__ import annotations

import sqlite3

import anthropic

from earshot.config import Config
from earshot.notifier.base import AlertResult, Notifier
from earshot.notifier.email_yahoo import EmailYahooNotifier
from earshot.notifier.stdout import StdoutNotifier
from earshot.notifier.twilio_interactive import TwilioInteractiveNotifier
from earshot.notifier.twilio_voice import TwilioTtsNotifier


def make_notifiers(
    cfg: Config,
    *,
    conn: sqlite3.Connection | None = None,
    anthropic_client: anthropic.Anthropic | None = None,
    dry_run: bool = False,
) -> list[Notifier]:
    if dry_run:
        return [StdoutNotifier(reason="dry-run")]

    notifiers: list[Notifier] = []
    if cfg.is_complete_for_email():
        notifiers.append(EmailYahooNotifier(
            sender_email=cfg.yahoo_email or "",
            app_password=cfg.yahoo_app_password or "",
            recipient=cfg.digest_recipient or "",
        ))

    v2_ready = (
        cfg.is_complete_for_voice() and bool(cfg.webhook_url) and conn is not None
    )

    if cfg.is_complete_for_voice() and anthropic_client is not None:
        notifiers.append(TwilioTtsNotifier(
            account_sid=cfg.twilio_account_sid or "",
            auth_token=cfg.twilio_auth_token or "",
            from_number=cfg.twilio_from_number or "",
            to_number=cfg.twilio_to_number or "",
            voice=cfg.twilio_voice,
            anthropic_client=anthropic_client,
            defer_instant_to_v2=v2_ready,
        ))

    if v2_ready:
        notifiers.append(TwilioInteractiveNotifier(
            conn=conn,
            account_sid=cfg.twilio_account_sid or "",
            auth_token=cfg.twilio_auth_token or "",
            from_number=cfg.twilio_from_number or "",
            to_number=cfg.twilio_to_number or "",
            webhook_url=cfg.webhook_url or "",
        ))

    if not notifiers:
        reasons: list[str] = []
        if not cfg.is_complete_for_email():
            reasons.append(f"email missing: {', '.join(cfg.missing_keys_for_email())}")
        if not cfg.is_complete_for_voice():
            reasons.append(f"voice missing: {', '.join(cfg.missing_keys_for_voice())}")
        return [StdoutNotifier(reason="no channels configured (" + "; ".join(reasons) + ")")]
    return notifiers


__all__ = [
    "AlertResult", "Notifier", "EmailYahooNotifier",
    "StdoutNotifier", "TwilioTtsNotifier", "TwilioInteractiveNotifier",
    "make_notifiers",
]
