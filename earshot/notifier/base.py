"""Notifier interface + shared result type."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from earshot.digest import DigestPayload


@dataclass
class AlertResult:
    status: str           # 'sent' | 'failed' | 'dry_run' | 'skipped'
    channel: str          # 'email' | 'stdout' | 'twilio' | ...
    recipient: str = ""
    error: str | None = None


class Notifier(Protocol):
    """Anything that takes a DigestPayload and tries to deliver it."""

    channel: str

    def send(self, payload: DigestPayload) -> AlertResult: ...
