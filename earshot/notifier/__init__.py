"""Notifier package.

Pluggable delivery layer. The active notifier is chosen by ``make_notifier``:
  * dry_run=True or no email creds  → ``StdoutNotifier``
  * email creds present              → ``EmailYahooNotifier``

Future channels (Twilio TTS in v1.5, Retell in v2) slot in by adding a new
``Notifier``-conforming class and updating the factory.
"""
from __future__ import annotations

from earshot.config import Config
from earshot.notifier.base import AlertResult, Notifier
from earshot.notifier.email_yahoo import EmailYahooNotifier
from earshot.notifier.stdout import StdoutNotifier


def make_notifier(cfg: Config, dry_run: bool = False) -> Notifier:
    if dry_run:
        return StdoutNotifier(reason="dry-run")
    if cfg.is_complete_for_email():
        return EmailYahooNotifier(
            sender_email=cfg.yahoo_email or "",
            app_password=cfg.yahoo_app_password or "",
            recipient=cfg.digest_recipient or "",
        )
    missing = cfg.missing_keys_for_email()
    return StdoutNotifier(reason=f"email not configured (missing: {', '.join(missing)})")


__all__ = ["AlertResult", "Notifier", "EmailYahooNotifier", "StdoutNotifier", "make_notifier"]
