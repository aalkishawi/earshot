# Earshot

A personal AI agent that listens to the podcasts you don't have time for,
extracts what's worth studying, scouts AI news, and delivers a daily briefing
to your inbox — and, if you want it, your phone.

Earshot runs on your own machine, billed against your own API keys. No SaaS
sign-up. No data leaving your laptop except to the providers you configure
yourself (Anthropic, Yahoo, optionally Twilio and Groq).

## Trial users start here

### Step 1 — gather two credentials (5 min)

Before installing, grab these — `earshot init` will ask for them and won't
let you skip:

- **Anthropic API key** — sign up at [console.anthropic.com](https://console.anthropic.com/),
  fund $5 in credits (covers ~2 months at default volume; $5 is Anthropic's
  minimum, not Earshot's).
- **Yahoo email + app password** — log in at
  [login.yahoo.com](https://login.yahoo.com/) → Account Security → "Generate
  and manage app passwords". Use the generated 16-char string, **not your
  login password** (that won't work for SMTP).

Optional: a Twilio account with a phone number if you want voice calls.

### Step 2 — install and configure

**One-shot installer (recommended)** — creates a `.venv` in the current
directory so Earshot doesn't pollute your global Python:

```powershell
# Windows PowerShell
iwr -useb https://raw.githubusercontent.com/aalkishawi/earshot/main/install.ps1 | iex
```

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/aalkishawi/earshot/main/install.sh | bash
```

The installer creates `.venv`, installs Earshot v0.3.0 from GitHub, and
tells you the four commands to run next.

**Or install manually:**

```powershell
# Python 3.11+ required
pip install git+https://github.com/aalkishawi/earshot.git

# interactive setup — paste the credentials above when prompted
earshot init

# verify everything is green
earshot doctor

# register a daily 18:30 local-time run (no-op if you'd rather kick it off manually)
earshot schedule
```

### Step 3 — what to expect on the first run

The first time the pipeline runs (manually or scheduled), it **baselines**
existing entries from every channel and news source so it doesn't process
months of backlog all at once. **No digest comes out of the first run** —
that's normal. From the second run onward you'll get a digest for
genuinely new uploads + news.

If you want to validate the full pipeline immediately rather than wait,
pick one baselined video and force it through:

```powershell
earshot videos --state skipped -n 5    # pick a video_id
earshot transcribe --video-id <id>
earshot analyze --video-id <id>
earshot digest                          # builds + sends the digest email
```

### Upgrading

```powershell
pip install --upgrade --force-reinstall git+https://github.com/aalkishawi/earshot.git
earshot doctor                          # confirm new version is healthy
```

Your `.env`, `channels.yaml`, and SQLite database are preserved across
upgrades. The schema auto-migrates on first run.

### What it costs you

You bring your own API keys. The defaults are calibrated for cheapest viable:

| Service | What for | Realistic monthly cost |
|---|---|---|
| Anthropic (Claude Haiku 4.5) | Summarization, concept extraction, news scoring, voice script | ~$1–2 |
| Yahoo SMTP | Email digest delivery | free |
| Groq Whisper (optional) | Audio transcription fallback when a video has no captions | ~$0 (free tier covers most use) |
| Twilio (optional, v1.5) | Outbound voice call with a 90-second briefing | ~$1.60 (1 number + 1 call/day) |
| Twilio interactive (optional, v2) | Interactive conversation about high-priority alerts | ~$0.10–0.14 per call (avg 1–3/week) |

At typical volume (3–5 podcast channels, 20 daily AI news items), total
operational cost lands at **~$3/month** (or **~$4/month** with v2 enabled).

## What it does

1. **Detects new podcast uploads** by polling YouTube RSS feeds (no API quota).
2. **Transcribes** them — manual captions first, auto-captions next, Groq
   Whisper ASR only if neither exists.
3. **Summarizes + extracts concepts** via Claude Haiku 4.5. Maintains a
   persistent glossary so terms you've already encountered don't get
   re-alerted.
4. **Scouts AI news** from a curated RSS list (Anthropic, OpenAI, DeepMind,
   Hugging Face, Simon Willison, Latent Space, Import AI) plus Hacker News
   stories above a points threshold.
5. **Scores news for relevance** (1–5 importance) — landmark items fire
   instant alerts; everything else lands in the daily digest.
6. **Delivers** by email and (optionally) by outbound voice call. Two voice
   modes:
   - **v1.5 TTS readout** (~90 seconds): a Claude-rendered spoken digest, no
     interaction. Default for the daily noon-EST briefing.
   - **v2 interactive** (~2–3 minutes): a turn-by-turn conversation that
     briefs you on each item, asks one productivity-focused followup, and
     captures a free-text journal note at the end. Fires for instant alerts
     when configured.
7. **Logs everything**: token use, cost per run, sent alerts, processed
   videos, full conversation transcripts. Inspect with `earshot status`,
   `earshot videos`, `earshot news`.

## CLI reference

```
# first-time setup
earshot init                                 # interactive setup wizard
earshot init-db                              # apply schema only (idempotent)
earshot doctor                               # green/red status of every service

# pipeline (manual)
earshot detect [--channel @handle]           # poll RSS for new uploads
earshot transcribe [-n N] [--video-id ID]    # transcribe pending videos
earshot analyze   [-n N] [--video-id ID]     # Claude summary + concepts
earshot scout                                # AI news fetch + relevance scoring
earshot digest [--instant]                   # build + send daily / instant digest
earshot run                                  # all of the above in sequence

# voice replay
earshot call --video-id ID                   # v1.5 TTS call for one episode (no DB writes)
earshot call --news-id N                     # v1.5 TTS call for one news item (no DB writes)
earshot call --daily                         # v1.5 TTS call of today's digest (no DB writes)
earshot call --video-id ID --interactive     # v2 conversation (records to `interactions` table)
earshot call --news-id N --interactive       # same, for a news item

# v2 webhook server (only needed for interactive calls)
earshot webhook                              # run FastAPI server (foreground)

# inspection
earshot status                               # config + DB health
earshot channels                             # configured channels + RSS URLs
earshot videos [--state X] [--priority-only] # videos table
earshot news   [--min-score N] [--state X]   # news table
earshot show VIDEO_ID                        # pretty-print stored analysis
earshot study-queue                          # regenerate study_queue.md

# scheduling
earshot schedule [--at HH:MM]                # register Windows Task / cron
earshot schedule --remove                    # unregister

# credential tests
earshot test-email                           # one-line email to verify SMTP
earshot test-call                            # one-ring call to verify Twilio
```

## Configuration

After `earshot init`, your `.env` looks like:

```
# Required
ANTHROPIC_API_KEY=sk-ant-...
YAHOO_EMAIL=you@yahoo.com
YAHOO_APP_PASSWORD=xxxxxxxxxxxxxxxx
DIGEST_RECIPIENT=you@example.com

# Optional — voice channel (v1.5 TTS)
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...
TWILIO_TO_NUMBER=+1...
TWILIO_VOICE=Polly.Joanna

# Optional — interactive call (v2; requires the Twilio block above)
EARSHOT_WEBHOOK_URL=https://abc.ngrok-free.app   # public base URL Twilio calls back
EARSHOT_WEBHOOK_HOST=127.0.0.1
EARSHOT_WEBHOOK_PORT=8765
EARSHOT_WEBHOOK_MAX_CALL_SECONDS=300              # hard cap per call
EARSHOT_WEBHOOK_MAX_TURNS=12                      # secondary cap to bound LLM cost
EARSHOT_WEBHOOK_VALIDATE_SIGNATURE=1              # set 0 only for local dev

# Optional — ASR fallback
GROQ_API_KEY=...

# Tuning
DIGEST_TIMEZONE=America/New_York
DIGEST_HOUR=12
DIGEST_MIN_NEWS_SCORE=3       # 1-5; daily digest threshold
INSTANT_MIN_NEWS_SCORE=5      # 1-5; instant alert threshold
MAX_LLM_COST_PER_RUN_USD=2.00
```

Channels live in `channels.yaml`. Add one with the wizard, or:

```yaml
- handle: "@AIDailyBrief"
  channel_id: UCKelCK4ZaO6HeEI1KQjqzWA
  name: "The AI Daily Brief"
  priority: 2                  # 2 = also fires instant alerts on new episodes
  keywords: []                 # title substrings that force a single video to priority
```

`earshot resolve-channel @somehandle` looks up `channel_id` for any handle.

## v2 interactive call — setup

The interactive call is optional and disabled until you configure a webhook
URL. It needs three things running at once during a call: the webhook
server, an HTTPS tunnel to it (ngrok), and Twilio dialing your phone.

```powershell
# 1. install ngrok and authenticate (one-time)
winget install Ngrok.Ngrok
ngrok config add-authtoken <YOUR_TOKEN>     # get from dashboard.ngrok.com

# 2. terminal #1 — start the tunnel, copy the https URL it prints
ngrok http 8765

# 3. paste the URL into .env as EARSHOT_WEBHOOK_URL=https://...

# 4. terminal #2 — start the webhook server
earshot webhook

# 5. terminal #3 — place an interactive call
earshot call --video-id <ID> --interactive
```

When Twilio dials, **press any key on your phone during the trial-account
preamble** to release the agent. The conversation runs ~2–3 minutes:
opener → briefing of each item + one productivity question → free-text
journal capture at the end. The full transcript and captured answers are
stored in the `interactions` table.

Once configured, v2 **automatically fires for instant alerts** (priority
videos or news scoring above `INSTANT_MIN_NEWS_SCORE`). v1.5 TTS continues
to handle the routine daily noon-EST briefing.

> **Security note:** running the webhook through ngrok with
> `EARSHOT_WEBHOOK_VALIDATE_SIGNATURE=1` may 403 because ngrok's URL
> canonicalization can drift from Twilio's signature. Set `=0` only for
> local development on a temporary tunnel. Production deployments behind
> a stable hosted endpoint should keep it on.

## Notes for trial testers

- **Yahoo "Primary" tab**: the digest may land in your "All" inbox tab
  instead of "Primary" because of HTML + outbound links. Check both. After
  marking it Not Spam once, future digests usually land in Primary.
- **Twilio trial accounts** can only dial verified numbers. If
  `earshot test-call` fails with code 21219, verify the TO number at
  console.twilio.com → Phone Numbers → Verified Caller IDs. Trial accounts
  also play a "you have a Twilio trial account" preamble before every
  call; **press any digit on your phone keypad** to skip it and reach the
  agent. Upgrading the Twilio account (any paid credit) removes the
  preamble.
- **Cost cap**: `MAX_LLM_COST_PER_RUN_USD` (default $2) aborts a single run
  if Claude spending crosses it. Adjust upward if you're catching up on a
  large backlog.
- **Signature validation through ngrok** may 403 because ngrok's URL
  canonicalization can drift from Twilio's signature. The v2 setup section
  above covers when it's safe to disable.

## Architecture

```
[channels.yaml] [news_sources.yaml]
       │              │
       ▼              ▼
┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
│  Detector  │─▶│ Transcriber│─▶│  Analyzer  │─▶│  Digest    │
│ (RSS poll) │  │  (yt-dlp + │  │  (Claude   │  │  Builder   │
│            │  │  Groq ASR) │  │  Haiku)    │  │            │
└────────────┘  └────────────┘  └────────────┘  └─────┬──────┘
                                                      │
┌────────────┐         ┌──────────────────────┐       │
│  News      │────────▶│  SQLite              │◀──────┤
│  Scout     │         │  (state + history)   │       │
└────────────┘         └──────────────────────┘       │
                                                      ▼
                                            ┌─────────────────────────┐
                                            │  Notifiers              │
                                            │  email (Yahoo SMTP)     │
                                            │  voice TTS (v1.5)       │
                                            │  voice interactive (v2) │
                                            └─────────────────────────┘
```

## License

MIT. See [LICENSE](LICENSE).
