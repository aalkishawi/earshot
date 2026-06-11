"""Tests for the wizard's error classification helpers.

These probe the message-massaging logic without touching real APIs.
"""
from __future__ import annotations

from earshot.wizard import _classify_anthropic_error, _classify_yahoo_error


# --- Anthropic ------------------------------------------------------------- #

def test_anthropic_credit_balance_low():
    exc = Exception("Your credit balance is too low to access the Anthropic API.")
    msg = _classify_anthropic_error(exc)
    assert "credit" in msg.lower()
    assert "console.anthropic.com" in msg


def test_anthropic_bad_key_401():
    exc = Exception("Error code: 401 - invalid API key")
    msg = _classify_anthropic_error(exc)
    assert "rejected" in msg.lower() or "api key" in msg.lower()
    assert "sk-ant-" in msg


def test_anthropic_authentication_error():
    exc = Exception("AuthenticationError: invalid x-api-key")
    msg = _classify_anthropic_error(exc)
    assert "rejected" in msg.lower() or "authentication" in msg.lower()


def test_anthropic_rate_limit():
    exc = Exception("rate limit exceeded")
    msg = _classify_anthropic_error(exc)
    assert "rate" in msg.lower()


def test_anthropic_network_failure():
    exc = ConnectionError("getaddrinfo failed")
    msg = _classify_anthropic_error(exc)
    assert "network" in msg.lower() or "internet" in msg.lower()


def test_anthropic_unknown_error_falls_through():
    exc = ValueError("something weird")
    msg = _classify_anthropic_error(exc)
    assert "something weird" in msg


# --- Yahoo ----------------------------------------------------------------- #

def test_yahoo_short_password_diagnoses_login_password():
    """A 12-char password is almost certainly a login password, not the 16-char app one."""

    class SMTPAuthenticationError(Exception):
        pass

    exc = SMTPAuthenticationError("(535, b'5.7.0 AUTHENTICATE failed')")
    msg = _classify_yahoo_error(exc, password="MyLoginPa$$")
    assert "16 char" in msg.lower() or "app password" in msg.lower()


def test_yahoo_correct_length_password_just_wrong():
    """A 16-char password but still rejected — points to wrong or stale password."""

    class SMTPAuthenticationError(Exception):
        pass

    exc = SMTPAuthenticationError("(535, b'5.7.0 AUTHENTICATE failed')")
    msg = _classify_yahoo_error(exc, password="abcdefghijklmnop")
    # Should NOT suggest the user is using their login password
    assert "login password" not in msg.lower()
    assert "extra characters" in msg.lower() or "latest one" in msg.lower()


def test_yahoo_network_failure():
    exc = ConnectionError("timed out connecting to smtp.mail.yahoo.com")
    msg = _classify_yahoo_error(exc, password="abcdefghijklmnop")
    assert "internet" in msg.lower() or "couldn't reach" in msg.lower()
