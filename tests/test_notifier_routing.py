"""Notifier routing tests — v1.5 vs v2 don't double-fire."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from earshot.digest import DigestPayload
from earshot.notifier.twilio_interactive import TwilioInteractiveNotifier
from earshot.notifier.twilio_voice import TwilioTtsNotifier


@pytest.fixture
def daily() -> DigestPayload:
    return DigestPayload(digest_type="daily", date_local="2026-06-11", subject="x")


@pytest.fixture
def instant_news() -> DigestPayload:
    return DigestPayload(digest_type="instant_news", date_local="2026-06-11", subject="x")


@pytest.fixture
def instant_video() -> DigestPayload:
    return DigestPayload(digest_type="instant_video", date_local="2026-06-11", subject="x")


# --- v1.5 TTS ---------------------------------------------------------------- #

def test_v15_defers_instant_when_v2_ready(instant_news):
    """When v2 is also configured, v1.5 must skip instant alerts to avoid double-dial."""
    tts = TwilioTtsNotifier(
        account_sid="x", auth_token="x", from_number="+1", to_number="+1",
        voice="Polly.Joanna", anthropic_client=MagicMock(),
        defer_instant_to_v2=True,
    )
    r = tts.send(instant_news)
    assert r.status == "skipped"
    assert "v2" in (r.error or "").lower()


def test_v15_handles_instant_when_v2_not_ready(instant_news, monkeypatch):
    """No v2 configured → v1.5 should attempt the instant alert (proves it doesn't skip blindly)."""
    tts = TwilioTtsNotifier(
        account_sid="x", auth_token="x", from_number="+1", to_number="+1",
        voice="Polly.Joanna", anthropic_client=MagicMock(),
        defer_instant_to_v2=False,
    )
    # The mock anthropic_client will fail render_voice_script with a missing
    # method — that's fine for this test, we just need to confirm the
    # routing didn't short-circuit to 'skipped'.
    r = tts.send(instant_news)
    assert r.status != "skipped"


# --- v2 interactive --------------------------------------------------------- #

def test_v2_skips_daily(daily):
    """v2 only ever handles instant_* payloads."""
    n = TwilioInteractiveNotifier(
        conn=MagicMock(), account_sid="x", auth_token="x",
        from_number="+1", to_number="+1", webhook_url="https://test.example",
    )
    r = n.send(daily)
    assert r.status == "skipped"


def test_v2_does_not_skip_instant_news(instant_news):
    """v2 must attempt instant_news (not skip it)."""
    # We're not actually placing a call — we just want to confirm v2's
    # skip guard doesn't fire. The notifier will fail at the empty refs
    # check (instant_news payload has no items).
    n = TwilioInteractiveNotifier(
        conn=MagicMock(), account_sid="x", auth_token="x",
        from_number="+1", to_number="+1", webhook_url="https://test.example",
    )
    r = n.send(instant_news)
    # Should fail with "no item refs" — proves it got past the skip guard.
    assert r.status == "failed"
    assert "no item refs" in (r.error or "")


def test_v2_does_not_skip_instant_video(instant_video):
    n = TwilioInteractiveNotifier(
        conn=MagicMock(), account_sid="x", auth_token="x",
        from_number="+1", to_number="+1", webhook_url="https://test.example",
    )
    r = n.send(instant_video)
    assert r.status == "failed"
    assert "no item refs" in (r.error or "")
