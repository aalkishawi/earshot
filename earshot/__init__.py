"""Earshot — personal AI agent for podcast + AI-news intelligence."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("earshot")
except PackageNotFoundError:
    # Source checkout without pip install — fall back to a dev placeholder
    # rather than a stale hardcoded value.
    __version__ = "0.0.0-dev"
