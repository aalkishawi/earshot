"""Tests for the news scout's RSS retry-with-backoff."""
from __future__ import annotations

from unittest.mock import MagicMock

from earshot import news_scout as scout_mod


# --- _is_transient_network_error -------------------------------------------- #

def test_winerror_10054_is_transient():
    assert scout_mod._is_transient_network_error(
        ConnectionResetError("[WinError 10054] connection forcibly closed")
    )


def test_timeout_is_transient():
    assert scout_mod._is_transient_network_error(TimeoutError("timed out"))


def test_5xx_in_message_is_transient():
    assert scout_mod._is_transient_network_error(Exception("HTTP 503 service unavailable"))


def test_xml_parse_error_is_not_transient():
    assert not scout_mod._is_transient_network_error(
        Exception("xml.sax.SAXParseException: mismatched tag")
    )


def test_none_is_not_transient():
    assert not scout_mod._is_transient_network_error(None)


# --- _feedparser_with_retry ------------------------------------------------- #

def _bozo_result(exc: Exception | None = None):
    """A feedparser-shaped object: bozo=1, no entries, optional exception."""
    r = MagicMock()
    r.bozo = 1
    r.entries = []
    r.bozo_exception = exc
    return r


def _ok_result():
    r = MagicMock()
    r.bozo = 0
    r.entries = [MagicMock(link="https://x/1", title="ok", get=lambda k, d=None: None)]
    r.bozo_exception = None
    return r


def test_retry_succeeds_on_second_attempt(monkeypatch):
    """Transient WinError 10054 on attempt 1 → success on attempt 2."""
    transient = ConnectionResetError("[WinError 10054]")
    calls = []

    def fake_parse(url, agent):
        calls.append(url)
        if len(calls) == 1:
            return _bozo_result(transient)
        return _ok_result()

    # Skip the actual sleep so the test runs fast.
    monkeypatch.setattr(scout_mod, "feedparser", MagicMock(parse=fake_parse))
    monkeypatch.setattr(scout_mod.time, "sleep", lambda s: None)

    parsed = scout_mod._feedparser_with_retry("https://x", max_attempts=3)
    assert len(calls) == 2
    assert parsed.entries  # success path


def test_retry_exhausts_attempts(monkeypatch):
    """All 3 attempts hit a transient error → returns last bozo result (no exception raised)."""
    transient = ConnectionResetError("[WinError 10054]")
    calls = []

    def fake_parse(url, agent):
        calls.append(url)
        return _bozo_result(transient)

    monkeypatch.setattr(scout_mod, "feedparser", MagicMock(parse=fake_parse))
    monkeypatch.setattr(scout_mod.time, "sleep", lambda s: None)

    parsed = scout_mod._feedparser_with_retry("https://x", max_attempts=3)
    assert len(calls) == 3
    assert parsed.bozo and not parsed.entries  # caller will raise/log


def test_non_transient_does_not_retry(monkeypatch):
    """XML parse error is not retryable — return after the first attempt."""
    permanent = Exception("xml.sax.SAXParseException: mismatched tag")
    calls = []

    def fake_parse(url, agent):
        calls.append(url)
        return _bozo_result(permanent)

    monkeypatch.setattr(scout_mod, "feedparser", MagicMock(parse=fake_parse))
    monkeypatch.setattr(scout_mod.time, "sleep", lambda s: None)

    parsed = scout_mod._feedparser_with_retry("https://x", max_attempts=3)
    assert len(calls) == 1  # no retries
    assert parsed.bozo


def test_success_does_not_retry(monkeypatch):
    """First-attempt success → only one call, even with retries available."""
    calls = []

    def fake_parse(url, agent):
        calls.append(url)
        return _ok_result()

    monkeypatch.setattr(scout_mod, "feedparser", MagicMock(parse=fake_parse))
    monkeypatch.setattr(scout_mod.time, "sleep", lambda s: None)

    parsed = scout_mod._feedparser_with_retry("https://x", max_attempts=5)
    assert len(calls) == 1
    assert parsed.entries
