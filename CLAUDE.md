# Earshot — Claude Instructions

## How to use this file
This is the project spec. Read it in full before responding to any request in
this repo. The original prompt from the user (Ashraf) is reproduced verbatim
below — treat it as the source of truth for scope, milestones, and tech choices.

## Working agreement
- **Plan first.** Before writing code, propose a plan + architecture and ask
  clarifying questions. Do not start implementing until the user approves.
- **Confirm tech choices are still current.** Especially YouTube transcript
  access (libraries break often). Verify before committing to a library.
- **Build incrementally** and let the user approve each module.
- **EMAIL-FIRST.** Get the full pipeline working end-to-end with email
  delivery before touching phone calls. Phone is explicitly out of v1 scope.
- **Never hardcode secrets** — env vars only, with a `.env.example` listing
  every required key.
- **Idempotency matters.** A crash mid-run must not double-send emails or
  double-process videos. Persist seen video IDs.
- **Cost controls.** Cap LLM/API calls, batch where possible, log token usage.

---

## Original project prompt (verbatim)

# Project: Earshot — Podcast & AI-News Intelligence Agent

## Goal
Build "Earshot", an agent that monitors a configurable list of YouTube podcast
channels, summarizes new episodes, extracts new concepts/technologies for me to
study, scouts the web for AI news, and reaches me via email (and, later, phone).

Before writing code: propose a plan and architecture, ask me any clarifying
questions, and confirm the tech choices below are still the best current options
(especially YouTube transcript access, which changes frequently). Build
incrementally and let me approve each module. Build EMAIL-FIRST — get the full
pipeline working end-to-end with email delivery before touching phone calls.
Never hardcode secrets — use env vars and provide a .env.example.

## Core Features

1. **New-episode detection**
   - Watch a configurable list of YouTube channels.
   - Use each channel's RSS feed (https://www.youtube.com/feeds/videos.xml?channel_id=ID)
     for new-upload detection — no API quota needed.
   - Persist seen video IDs so nothing is processed or alerted twice.

2. **Transcript acquisition**
   - Pull transcripts from YouTube captions where available.
   - Fall back to downloading audio (yt-dlp) + Whisper transcription when no
     captions exist.
   - Verify the current best library/approach before committing — this area breaks often.

3. **Summarization**
   - Per episode: concise summary + key takeaways + notable quotes/timestamps.

4. **Concept & technology extraction**
   - Extract new concepts, technologies, acronyms, tools, papers, and people
     mentioned (e.g. UBI, UBH, RAG, a new model name).
   - For each: term, a one-line definition, why it matters, and a timestamp/context.
   - Deduplicate against a stored glossary so I'm not re-alerted on terms I've
     already seen. Maintain a persistent "study queue" of new items.

5. **AI news scouting**
   - Periodically scout the web for the latest AI news (search + RSS from major
     AI sources).
   - Filter for relevance, dedupe against previously-sent items, rank by importance.

6. **Email delivery (PRIMARY channel — build this first)**
   - Send a digest email containing: new episode summaries, new concepts to study,
     and curated AI news.
   - Support both instant alerts (high-priority) and a scheduled daily digest.

7. **Phone calls (OPTIONAL / STAGED — do not build until email works)**
   - Mode A — Updates readout (v1.5, simple): outbound call that reads a short
     spoken summary via TTS. Twilio outbound + TTS is enough here.
   - Mode B — Interactive (v2, stretch): outbound conversational call that can ask
     me questions and capture my answers. This is a separate, heavier build
     requiring a conversational agent (e.g. Retell), webhooks, turn-taking, and
     answer capture. Treat as a distinct milestone, not part of v1.
   - Architect the alerting layer so a phone channel can be plugged in later
     without rewrites, but ship v1 with email only.

## Suggested additions (implement if reasonable)
- Priority/urgency routing: email for routine; reserve any future call for
  genuinely high-priority items or when an answer is needed.
- Per-channel config (name, channel_id, priority, keywords to watch for).
- Cost controls: cap LLM/API calls, batch where possible, log token usage.
- Idempotency: a crash mid-run must not double-send emails or double-call.
- Observability: structured logging + a simple run history.
- A reviewable study queue export (markdown or a table) of all extracted concepts.

## Tech stack (defaults — confirm or swap)
- Language: Python.
- New-video detection: YouTube RSS feeds. YouTube Data API only if richer metadata is needed.
- Transcripts: captions + yt-dlp/Whisper fallback (verify current best).
- LLM: Claude (Anthropic API) for summarization + extraction with structured output.
- Email: [Resend / SendGrid / SMTP — pick one].
- Voice (only when you reach the phone milestone): Twilio for outbound dialing;
  Retell for the conversational agent.
- Storage: SQLite for local, or Postgres if hosted (tables: channels, videos,
  concepts/glossary, news_items, runs/alerts).
- Scheduling: a scheduler (APScheduler/cron) running the poll → process → notify loop.
- Hosting: runnable locally first; deployable to Railway.

## Config
- channels.yaml/json: list of channels with priority + watch-keywords.
- Global config: digest schedule, poll interval, my email + (optional) phone number,
  priority thresholds.
- .env for all keys (Anthropic, YouTube, email; Twilio/Retell only at phone milestone).

## Build order / milestones
1. v1: RSS detection → transcript → summary → concept extraction → AI news →
   email digest, with dry-run/test mode. Ship this fully working first.
2. v1.5: optional Twilio TTS "read me the digest" outbound call.
3. v2: optional Retell interactive call that asks me questions.

## Deliverables
- Working repo named "earshot" with README and setup steps.
- .env.example listing every required key.
- A dry-run / test mode that processes one video and prints output without
  sending email or placing calls.
- Acceptance (v1): detect a new video → summarize → extract concepts → assemble
  digest → (test mode) print it → send email. Phone is explicitly out of v1 scope.
