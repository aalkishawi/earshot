"""Shared fixtures for Earshot tests.

All fixtures are hermetic — no network, no real API keys, no real DB
outside the tmp_path. Anthropic + Twilio clients are mocked where they
appear.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from earshot import db as db_mod
from earshot.config import Config


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh schema-initialized SQLite connection. Closes at teardown."""
    db_path = tmp_path / "test.db"
    conn = db_mod.connect(db_path)
    db_mod.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Minimal Config with only the values tests typically need."""
    return Config(
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        channels_path=tmp_path / "channels.yaml",
        news_sources_path=tmp_path / "news_sources.yaml",
        anthropic_api_key="sk-test",
        groq_api_key=None,
        yahoo_email=None,
        yahoo_app_password=None,
        digest_recipient=None,
        twilio_account_sid=None,
        twilio_auth_token="test-twilio-token",
        twilio_from_number=None,
        twilio_to_number=None,
        twilio_voice="Polly.Joanna",
        webhook_url="https://test.example",
        webhook_host="127.0.0.1",
        webhook_port=8765,
        webhook_max_call_seconds=300,
        webhook_max_turns=12,
        webhook_validate_signature=True,
        digest_timezone="America/New_York",
        digest_hour=12,
        digest_min_news_score=3,
        instant_min_news_score=5,
        log_level="INFO",
        max_llm_cost_per_run_usd=2.0,
        channels=[],
        news_sources=[],
    )


@pytest.fixture
def mock_anthropic_client() -> MagicMock:
    """Mock that returns a tool-use response Claude would produce.

    Tests override the .input mapping to whatever tool result they want.
    """
    client = MagicMock()
    # Default: a tool_use block with empty input and 50/20 token usage.
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_opener"
    block.input = {"script": "Hi. One item. Ready?"}
    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = 50
    response.usage.output_tokens = 20
    response.stop_reason = "tool_use"
    client.messages.create.return_value = response
    return client
