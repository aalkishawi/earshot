# Earshot

Podcast & AI-news intelligence agent. Monitors a configurable list of YouTube
podcast channels, summarizes new episodes, extracts new concepts/tools/people
to study, scouts the web for AI news, and delivers everything by email.

Build status: **module 1 of 8** — skeleton + SQLite schema + config. The
pipeline itself (`earshot run`) is wired up over modules 2–7.

## Setup

Requires Python 3.11+. For the audio-ASR fallback path (module 3), also install
ffmpeg — on Windows the easiest path is `winget install Gyan.FFmpeg`. yt-dlp
uses ffmpeg to extract audio from the downloaded stream. If captions are
available for a video, ffmpeg is not invoked.

```powershell
# from the repo root
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .

# create the database
earshot init-db

# verify config + see what's wired up
earshot status
```

Copy `.env.example` to `.env` and fill in keys as you reach each module:

| Variable | Needed at | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | module 4 (analyzer) | https://console.anthropic.com |
| `YAHOO_EMAIL` | module 7 (email) | your Yahoo address |
| `YAHOO_APP_PASSWORD` | module 7 (email) | Yahoo Account Security → app passwords |
| `DIGEST_RECIPIENT` | module 7 (email) | where the digest is delivered |
| `GROQ_API_KEY` | module 3 (transcriber fallback) | https://console.groq.com |

## CLI

```
earshot status                       # config + db health
earshot init-db                      # create / re-apply schema (idempotent)
earshot channels                     # show configured channels + RSS URLs
earshot resolve-channel @somehandle  # look up a channel ID
earshot run --dry-run                # full pipeline (module 2+)
```

## Channels

Edit `channels.yaml`. Each channel:

```yaml
- handle: "@AIDailyBrief"
  channel_id: UCKelCK4ZaO6HeEI1KQjqzWA
  name: "The AI Daily Brief"
  priority: 2          # 1 = digest only, 2 = also fires instant alerts
  keywords: []         # title substrings that force high-priority for one video
```

Use `earshot resolve-channel @handle` to get the `channel_id` for a new entry.

## Roadmap

1. ✅ **Module 1** — package skeleton, SQLite schema, config, CLI
2. ✅ **Module 2** — RSS detector (idempotent new-video detection)
3. ✅ **Module 3** — transcriber (yt-dlp captions → Groq Whisper ASR fallback)
4. **Module 4** — analyzer (Claude: summary + concept extraction; glossary dedup)
5. **Module 5** — AI news scout
6. **Module 6** — digest builder + `study_queue.md`
7. **Module 7** — Yahoo SMTP notifier, scheduler entry point
8. **Module 8** — end-to-end acceptance test with real email delivery

Phone (Twilio TTS, then Retell) lands as a separate v1.5/v2 milestone — out
of v1 scope per `CLAUDE.md`.
