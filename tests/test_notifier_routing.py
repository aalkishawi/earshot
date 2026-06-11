"""Notifier routing tests — v1.5 vs v2 don't double-fire."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from earshot.digest import DigestNewsItem, DigestPayload
from earshot.notifier import twilio_interactive as v2_mod
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


# --- pre-flight healthz check ----------------------------------------------- #

def _instant_news_with_item() -> DigestPayload:
    """Payload with one real item — needed so we reach the pre-flight check."""
    return DigestPayload(
        digest_type="instant_news", date_local="2026-06-11", subject="x",
        news_items=[DigestNewsItem(
            id=1, source="test", title="t", url="https://x", importance_score=5, summary="",
        )],
    )


def test_v2_aborts_when_webhook_unreachable(monkeypatch):
    """If /healthz doesn't respond, the notifier must abort BEFORE dialing
    Twilio. Otherwise we burn minutes on a call that lands on 'application
    error' the instant the user presses through the trial preamble."""

    def fake_unreachable(url, timeout=3.0):
        return False, "ConnectionError: no route"

    monkeypatch.setattr(v2_mod, "_webhook_reachable", fake_unreachable)

    # If Twilio's calls.create were called, this notifier would also need
    # account_sid/auth_token to look real. We give it junk on purpose; the
    # test should never reach the Twilio code path.
    n = TwilioInteractiveNotifier(
        conn=MagicMock(), account_sid="x", auth_token="x",
        from_number="+1", to_number="+13106867861",
        webhook_url="https://test.example",
    )
    r = n.send(_instant_news_with_item())
    assert r.status == "failed"
    assert "webhook not reachable" in (r.error or "")
    # create_interaction also must NOT have been called — no DB write for
    # a call we know is going to fail.
    n._conn.execute.assert_not_called()


def test_v2_proceeds_when_webhook_healthy(monkeypatch):
    """A 200 OK from /healthz lets the call proceed (and Twilio errors are
    surfaced as 'failed' with the Twilio-specific message, not the
    pre-flight one)."""

    def fake_reachable(url, timeout=3.0):
        return True, ""

    monkeypatch.setattr(v2_mod, "_webhook_reachable", fake_reachable)

    # Mock the Twilio Client so we don't actually hit their API.
    fake_client_cls = MagicMock()
    fake_call = MagicMock()
    fake_call.sid = "CAtest"
    fake_call.status = "queued"
    fake_client_cls.return_value.calls.create.return_value = fake_call

    # Patch the local import inside .send. We mock the whole `twilio.rest`
    # module so the inline import inside send() picks up the fake.
    import sys
    import types
    fake_rest = types.ModuleType("twilio.rest")
    fake_rest.Client = fake_client_cls
    fake_exceptions = types.ModuleType("twilio.base.exceptions")
    class FakeRestException(Exception):
        code = 0
    fake_exceptions.TwilioRestException = FakeRestException
    monkeypatch.setitem(sys.modules, "twilio.rest", fake_rest)
    monkeypatch.setitem(sys.modules, "twilio.base.exceptions", fake_exceptions)

    # Need a real DB to call create_interaction. Use an in-memory sqlite.
    import sqlite3
    from earshot import db as db_mod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_mod.init_schema(conn)

    n = TwilioInteractiveNotifier(
        conn=conn, account_sid="AC123", auth_token="x",
        from_number="+1", to_number="+13106867861",
        webhook_url="https://test.example",
    )
    r = n.send(_instant_news_with_item())
    assert r.status == "sent", r.error
    # Twilio.calls.create was called once with the webhook URL
    fake_client_cls.return_value.calls.create.assert_called_once()
    call_kwargs = fake_client_cls.return_value.calls.create.call_args.kwargs
    assert "test.example/twiml/start?interaction_id=" in call_kwargs["url"]
