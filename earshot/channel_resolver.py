"""Resolve a YouTube @handle to its UC-prefixed channel ID.

Why we don't just grep for the first `channel/UC...` in the page HTML:
YouTube's @handle pages include references to *recommended/related* channels
that can appear in the markup before the page's own channel ID. The page's own
canonical channel ID is reliably found in the `og:url` meta tag.
"""
from __future__ import annotations

import re

import requests


_USER_AGENT = "Mozilla/5.0 (compatible; Earshot/0.1; +https://github.com/)"
_OG_URL_RE = re.compile(
    r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'
)
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')


class ChannelResolutionError(Exception):
    pass


def resolve_handle(handle: str, timeout: float = 10.0) -> tuple[str, str]:
    """Return (channel_id, channel_name) for a given @handle.

    Raises ChannelResolutionError on HTTP errors or if no channel ID can be
    extracted from the page.
    """
    handle = handle.lstrip("@")
    url = f"https://www.youtube.com/@{handle}"
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
    if resp.status_code != 200:
        raise ChannelResolutionError(
            f"HTTP {resp.status_code} fetching {url} — handle may not exist"
        )
    html = resp.text
    id_match = _OG_URL_RE.search(html)
    if not id_match:
        raise ChannelResolutionError(
            f"Could not extract channel ID from @{handle} page (og:url missing)"
        )
    title_match = _OG_TITLE_RE.search(html)
    name = title_match.group(1) if title_match else handle
    return id_match.group(1), name
