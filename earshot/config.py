"""Configuration loading for Earshot.

Sources, in order:
  1. .env (via python-dotenv) — secrets and runtime knobs
  2. channels.yaml — channel watch list with per-channel priority/keywords
  3. news_sources.yaml — RSS sources for the AI news scout (module 5)

Missing files do not raise here; callers check `Config.is_complete()` before
running the live pipeline. This lets module-1 smoke tests work on a fresh repo
with no .env yet.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "earshot.db"
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_CHANNELS_PATH = REPO_ROOT / "channels.yaml"
DEFAULT_NEWS_SOURCES_PATH = REPO_ROOT / "news_sources.yaml"


@dataclass
class ChannelConfig:
    handle: str
    channel_id: str
    name: str
    priority: int = 1
    keywords: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def rss_url(self) -> str:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={self.channel_id}"


@dataclass
class Config:
    # Paths
    db_path: Path
    data_dir: Path
    channels_path: Path
    news_sources_path: Path

    # Secrets / API keys
    anthropic_api_key: str | None
    groq_api_key: str | None
    yahoo_email: str | None
    yahoo_app_password: str | None
    digest_recipient: str | None

    # Runtime knobs
    digest_timezone: str
    digest_hour: int
    log_level: str
    max_llm_cost_per_run_usd: float

    # Loaded data
    channels: list[ChannelConfig]
    news_sources: list[dict[str, Any]]

    def is_complete_for_email(self) -> bool:
        return all([
            self.anthropic_api_key,
            self.yahoo_email,
            self.yahoo_app_password,
            self.digest_recipient,
        ])

    def missing_keys_for_email(self) -> list[str]:
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.yahoo_email:
            missing.append("YAHOO_EMAIL")
        if not self.yahoo_app_password:
            missing.append("YAHOO_APP_PASSWORD")
        if not self.digest_recipient:
            missing.append("DIGEST_RECIPIENT")
        return missing


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_channels(path: Path) -> list[ChannelConfig]:
    data = _load_yaml(path)
    raw_channels = data.get("channels", [])
    out: list[ChannelConfig] = []
    for c in raw_channels:
        out.append(ChannelConfig(
            handle=c["handle"],
            channel_id=c["channel_id"],
            name=c.get("name", c["handle"]),
            priority=int(c.get("priority", 1)),
            keywords=list(c.get("keywords", [])),
            active=bool(c.get("active", True)),
        ))
    return out


def _load_news_sources(path: Path) -> list[dict[str, Any]]:
    data = _load_yaml(path)
    return list(data.get("sources", []))


def load(dotenv_path: Path | None = None) -> Config:
    if dotenv_path is None:
        dotenv_path = REPO_ROOT / ".env"
    load_dotenv(dotenv_path, override=False)

    return Config(
        db_path=Path(os.environ.get("EARSHOT_DB_PATH", str(DEFAULT_DB_PATH))),
        data_dir=Path(os.environ.get("EARSHOT_DATA_DIR", str(DEFAULT_DATA_DIR))),
        channels_path=DEFAULT_CHANNELS_PATH,
        news_sources_path=DEFAULT_NEWS_SOURCES_PATH,

        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        groq_api_key=os.environ.get("GROQ_API_KEY") or None,
        yahoo_email=os.environ.get("YAHOO_EMAIL") or None,
        yahoo_app_password=os.environ.get("YAHOO_APP_PASSWORD") or None,
        digest_recipient=os.environ.get("DIGEST_RECIPIENT") or None,

        digest_timezone=os.environ.get("DIGEST_TIMEZONE", "America/New_York"),
        digest_hour=int(os.environ.get("DIGEST_HOUR", "12")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        max_llm_cost_per_run_usd=float(os.environ.get("MAX_LLM_COST_PER_RUN_USD", "2.00")),

        channels=_load_channels(DEFAULT_CHANNELS_PATH),
        news_sources=_load_news_sources(DEFAULT_NEWS_SOURCES_PATH),
    )


def json_dumps(obj: Any) -> str:
    """Stable JSON for storing structured data in SQLite TEXT columns."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
