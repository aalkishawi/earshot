"""Transcript acquisition for detected videos.

Order (cheapest first):
    1. yt-dlp -> manual captions (English variants)
    2. yt-dlp -> auto-generated captions
    3. yt-dlp -> download compressed audio -> Groq Whisper ASR

Captions are free; ASR costs ~$0.04 per audio hour on Groq's
``whisper-large-v3-turbo`` model. Captions are tried first by design.

Groq has a per-file upload limit (~25 MB). At 32 kbps mono mp3 that's
roughly 100 minutes of audio. Files larger than the limit are recorded as
``state='failed'`` with an error message — chunking is a future enhancement
if it actually comes up.
"""
from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from earshot.config import Config
from earshot.db import utcnow_iso


log = logging.getLogger("earshot.transcriber")

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB free-tier per-file limit
ENGLISH_LANGS = ["en", "en-US", "en-GB", "en-AU", "en-CA"]


class TranscriberError(Exception):
    pass


@dataclass
class TranscribeResult:
    video_id: str
    status: str            # 'transcribed' | 'failed' | 'skipped'
    source: str | None     # 'captions' | 'auto_captions' | 'asr'
    chars: int = 0
    transcript_path: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# VTT parser
# --------------------------------------------------------------------------- #

_VTT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def vtt_to_text(vtt: str) -> str:
    """Extract plain spoken text from a WebVTT file's contents.

    YouTube auto-captions emit per-word `<00:00:01.234>` timing tags and lots
    of duplicate "rolling" cues. We strip tags, skip cue headers, dedup
    consecutive identical lines.
    """
    lines: list[str] = []
    last = ""
    for raw_line in vtt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE"):
            continue
        if _VTT_TIMESTAMP_RE.match(line):
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        # cue identifier lines are short and have no spaces — skip them
        if line.isdigit():
            continue
        cleaned = _VTT_TAG_RE.sub("", line).strip()
        cleaned = html.unescape(cleaned)
        if not cleaned or cleaned == last:
            continue
        lines.append(cleaned)
        last = cleaned
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# yt-dlp wrappers
# --------------------------------------------------------------------------- #

def _ytdlp_logger() -> logging.Logger:
    # silence yt-dlp's noisy default output; surface errors at WARNING
    yl = logging.getLogger("yt_dlp")
    yl.setLevel(logging.WARNING)
    return yl


def fetch_captions(video_url: str, prefer_auto: bool = False) -> str | None:
    """Try to fetch captions for a video. Returns plain text or None.

    If ``prefer_auto`` is False (default), only manual captions are returned.
    If True, only auto-generated captions are returned. The two-pass design
    lets the caller prefer manual over auto cleanly.
    """
    from yt_dlp import YoutubeDL

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        opts = {
            "writesubtitles": not prefer_auto,
            "writeautomaticsub": prefer_auto,
            "subtitleslangs": ENGLISH_LANGS,
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": str(tmp / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "logger": _ytdlp_logger(),
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(video_url, download=True)
        except Exception as e:
            log.warning("yt-dlp caption fetch failed for %s: %s", video_url, e)
            return None

        # Look for the produced .vtt file in any of the requested languages.
        for lang in ENGLISH_LANGS:
            for candidate in tmp.glob(f"*.{lang}.vtt"):
                return vtt_to_text(candidate.read_text(encoding="utf-8", errors="replace"))
        # Some channels store generated subs under a different lang code variant.
        for candidate in tmp.glob("*.vtt"):
            return vtt_to_text(candidate.read_text(encoding="utf-8", errors="replace"))
    return None


def download_audio(video_url: str, target_dir: Path) -> Path:
    """Download a compressed mono audio file suitable for ASR.

    32 kbps mono mp3 keeps speech intelligible while shrinking 1 hr of audio
    to ~14 MB — well under Groq's 25 MB per-file limit.
    """
    from yt_dlp import YoutubeDL

    target_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(target_dir / "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "logger": _ytdlp_logger(),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "32",
            },
        ],
        "postprocessor_args": ["-ac", "1"],  # mono
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        video_id = info.get("id")
    if not video_id:
        raise TranscriberError(f"yt-dlp did not return a video id for {video_url}")
    mp3_path = target_dir / f"{video_id}.mp3"
    if not mp3_path.exists():
        raise TranscriberError(f"audio file not found at {mp3_path}")
    return mp3_path


# --------------------------------------------------------------------------- #
# Groq ASR
# --------------------------------------------------------------------------- #

def groq_transcribe(audio_path: Path, api_key: str, timeout: float = 600.0) -> str:
    """Send an audio file to Groq's Whisper endpoint, return plain text."""
    size = audio_path.stat().st_size
    if size > GROQ_MAX_FILE_BYTES:
        raise TranscriberError(
            f"audio file {size:,} bytes exceeds Groq's "
            f"{GROQ_MAX_FILE_BYTES:,}-byte per-file limit (chunking not yet implemented)"
        )
    with audio_path.open("rb") as f:
        files = {"file": (audio_path.name, f, "audio/mpeg")}
        data = {"model": GROQ_MODEL, "response_format": "json"}
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.post(
            GROQ_TRANSCRIPTION_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=timeout,
        )
    if resp.status_code != 200:
        raise TranscriberError(f"Groq HTTP {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    text = body.get("text", "")
    if not text:
        raise TranscriberError(f"Groq returned no text: {json.dumps(body)[:500]}")
    return text


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def transcribe_video(
    conn: sqlite3.Connection,
    video_row: sqlite3.Row,
    cfg: Config,
    dry_run: bool = False,
) -> TranscribeResult:
    """Try captions, then auto-captions, then ASR. Update DB on success/failure."""
    video_id = video_row["video_id"]
    url = video_row["url"]

    log.info("transcribing %s", video_id)

    text: str | None = None
    source: str | None = None
    error: str | None = None

    # 1. Manual captions
    try:
        text = fetch_captions(url, prefer_auto=False)
        if text:
            source = "captions"
    except Exception as e:
        log.warning("manual captions errored for %s: %s", video_id, e)

    # 2. Auto-captions
    if not text:
        try:
            text = fetch_captions(url, prefer_auto=True)
            if text:
                source = "auto_captions"
        except Exception as e:
            log.warning("auto captions errored for %s: %s", video_id, e)

    # 3. ASR fallback
    if not text:
        if not cfg.groq_api_key:
            error = "no captions available and GROQ_API_KEY is not set"
        else:
            audio_dir = cfg.data_dir / "audio_cache"
            audio_path: Path | None = None
            try:
                audio_path = download_audio(url, audio_dir)
                text = groq_transcribe(audio_path, cfg.groq_api_key)
                source = "asr"
            except Exception as e:
                error = f"asr failed: {e}"
            finally:
                if audio_path is not None:
                    try:
                        audio_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    if not text:
        if dry_run:
            return TranscribeResult(video_id=video_id, status="failed", source=None, error=error)
        conn.execute(
            "UPDATE videos SET state='failed', error=?, transcribed_at=? WHERE video_id=?",
            (error or "transcription failed", utcnow_iso(), video_id),
        )
        return TranscribeResult(video_id=video_id, status="failed", source=None, error=error)

    transcripts_dir = cfg.data_dir / "transcripts"
    transcript_path = transcripts_dir / f"{video_id}.txt"

    if dry_run:
        return TranscribeResult(
            video_id=video_id, status="transcribed", source=source,
            chars=len(text), transcript_path=str(transcript_path),
        )

    transcripts_dir.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(text, encoding="utf-8")

    conn.execute(
        """
        UPDATE videos
           SET state='transcribed',
               transcript_path=?,
               transcript_source=?,
               transcribed_at=?,
               error=NULL
         WHERE video_id=?
        """,
        (str(transcript_path), source, utcnow_iso(), video_id),
    )
    return TranscribeResult(
        video_id=video_id, status="transcribed", source=source,
        chars=len(text), transcript_path=str(transcript_path),
    )


def select_pending(
    conn: sqlite3.Connection,
    limit: int | None = None,
    video_id: str | None = None,
) -> list[sqlite3.Row]:
    """Return videos to transcribe. If video_id is given, return that one
    regardless of state (lets the user force a reprocess)."""
    if video_id:
        rows = conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchall()
        return list(rows)
    sql = "SELECT * FROM videos WHERE state = 'detected' ORDER BY detected_at ASC"
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql).fetchall())
