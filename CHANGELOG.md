# Changelog

All notable changes are tracked here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/) (currently in the 0.x
phase, so minor bumps may introduce breaking changes when needed).

## [Unreleased]

### Added
- `earshot show NEWS_ID` — inspect a single news item by id, mirroring
  the existing video flow.
- `earshot runs [-n N] [--status X]` — pipeline run history with Claude
  spend totals.
- GitHub Actions test workflow on push / PR across Python 3.11 / 3.12 /
  3.13, with a build badge in the README.

### Changed
- Wizard surfaces actionable error messages for Anthropic (credit
  balance, bad key) and Yahoo SMTP (16-char app password heuristic)
  failures instead of dumping raw SDK exceptions.
- `earshot init` ends with the same green/red probes as `earshot
  doctor` and refuses to claim "Setup complete" when anything's red.
- README's signature-validation security note rewritten now that the
  URL-reconstruction hardening + tests cover ngrok drift.

## [0.3.0] — 2026-06-11

### Added
- **v2 interactive call channel.** Outbound conversational call via
  Twilio `<Gather>` + Claude Haiku. ~$0.10–0.14 per call (3-5x cheaper
  than Retell / Vapi / ElevenLabs ConvAI).
  - Five-turn conversation: opener → per-item briefing + one
    productivity-focused followup → free-text journal capture at close.
  - Briefing + followup share a single `<Gather>` per item to halve
    speech-recognition charges.
  - Auto-fires for instant alerts; v1.5 TTS retains the routine daily
    briefing. No double-dial — each notifier checks payload type.
- **Webhook service** (`earshot webhook`) — FastAPI app exposing
  `/twiml/start`, `/twiml/turn`, `/twiml/status`. Hardened Twilio
  signature validation against ngrok URL drift (raw ASGI query string,
  multiple fallback URL forms, debug logging on rejection).
- **Replay tooling** — `earshot interactions` lists recent calls;
  `earshot replay <id>` prints the full turn-by-turn transcript.
  Orphaned `started` rows are swept to `abandoned` on webhook startup.
- **One-shot installers** — `install.ps1` and `install.sh` create a
  fresh venv and install via `pip install git+...` in one command.
- **pytest suite** (48 tests, ~5s) covering state machine, TwiML
  safety, notifier routing, schema migration, signature validation,
  store helpers, and wizard error classification.

### Changed
- Schema bumped v1 → v2 with the `interactions` table (auto-migrates).
- README "Trial users start here" front-loads required credentials and
  sets honest first-run expectations (the first run baselines and
  produces no digest — by design).
- `pyproject.toml` now lists `fastapi`, `uvicorn[standard]`, and
  `python-multipart` as runtime deps; adds `[project.optional-dependencies.dev]`
  for pytest + httpx.

## [0.2.0] — 2026-06-10

### Added
- v1.5 voice channel — outbound Twilio TTS call reading a ~90-second
  voice-tailored digest.
- Trial-distribution polish: `earshot init` wizard, `earshot doctor`
  diagnostic, `earshot schedule` Windows Task / cron helper, MIT
  license file.

## [0.1.0] — 2026-06-10

### Added
- Initial release. Full v1 pipeline: RSS detector → captions /
  Whisper transcriber → Claude summary + concept extraction → AI news
  scout with relevance scoring → digest builder → Yahoo SMTP email
  delivery → study queue export. Daily scheduling via `earshot run`.
