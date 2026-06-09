"""Stdout notifier — prints subject + first lines, no I/O.

Active in dry-run mode and when email creds are not configured. Lets the
pipeline run end-to-end during development without spamming a real inbox.
"""
from __future__ import annotations

from earshot.digest import DigestPayload, render_markdown
from earshot.notifier.base import AlertResult


class StdoutNotifier:
    channel = "stdout"

    def __init__(self, reason: str = "") -> None:
        self._reason = reason

    def send(self, payload: DigestPayload) -> AlertResult:
        banner = f"[stdout-notifier {self._reason}]" if self._reason else "[stdout-notifier]"
        print(f"\n{banner} would send:")
        print(f"  Subject: {payload.subject}")
        # First 8 lines of the body, for sanity.
        for line in render_markdown(payload).splitlines()[:8]:
            print(f"  | {line}")
        print("  ...")
        return AlertResult(status="dry_run", channel=self.channel, recipient="stdout")
