"""Earshot CLI.

Subcommands grow with each module:
  init-db          : create the SQLite file and apply schema
  status           : show DB + config health
  channels         : list configured channels and their RSS feeds
  resolve-channel  : look up a @handle's channel ID (for adding new channels)
  detect           : poll RSS feeds and record new videos (module 2)
  videos           : list rows from the videos table (module 2)
  run              : (placeholder) full pipeline — implemented in later modules
"""
from __future__ import annotations

import sys

import click

import json
import re

# Windows console defaults to cp1252 which can't encode characters like
# `→`, `—`, `…` that appear in help text and echoed output. Force UTF-8
# at the wrapper layer; downstream code can use unicode freely.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from earshot import __version__, config as config_mod, db as db_mod
from earshot.channel_resolver import ChannelResolutionError, resolve_handle
from earshot.detector import detect_all
from earshot.transcriber import select_pending as select_pending_transcribe, transcribe_video
from earshot.analyzer import (
    CostCapReached, analyze_video, finish_run, make_client,
    select_pending as select_pending_analyze, start_run,
)
from earshot.news_scout import scout as run_scout
from earshot import digest as digest_mod
from earshot import study_queue as study_queue_mod
from earshot.notifier import make_notifiers
from earshot.notifier.email_yahoo import EmailYahooNotifier
from earshot.notifier.twilio_voice import TwilioTtsNotifier
from earshot.notifier.twilio_interactive import TwilioInteractiveNotifier
from earshot import doctor as doctor_mod
from earshot import scheduler as scheduler_mod
from earshot import wizard as wizard_mod


_XML_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_string_list(items: list) -> list[str]:
    """Make `show` defensive against shape variations in Claude's output.

    Three pathologies handled:
      - List of dicts (legacy schema) → pull the first non-empty string value.
      - List of single characters (the model leaked tool-call XML and JSON
        emitted it char-by-char) → join, strip XML tags, return as one item.
      - List of strings → pass through, dropping empties.
    """
    if not items:
        return []
    # Pathology: char-level fragmentation. Heuristic: most elements are <=2
    # chars AND the total list is long.
    str_items = [x for x in items if isinstance(x, str)]
    if len(str_items) == len(items) and len(items) > 20:
        tiny = sum(1 for s in str_items if len(s) <= 2)
        if tiny / len(items) > 0.5:
            joined = "".join(str_items)
            cleaned = _XML_TAG_RE.sub("", joined).strip()
            return [cleaned] if cleaned else []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
            if s:
                out.append(s)
        elif isinstance(it, dict):
            for key in ("quote", "text", "takeaway", "content"):
                v = it.get(key)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
    return out


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="earshot")
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Process and print, but do not send email or place calls.",
)
@click.pass_context
def main(ctx: click.Context, dry_run: bool) -> None:
    """Earshot — podcast & AI-news intelligence agent."""
    ctx.ensure_object(dict)
    ctx.obj["dry_run"] = dry_run
    ctx.obj["config"] = config_mod.load()


@main.command("init")
def init_cmd() -> None:
    """Interactive setup wizard — first-time configuration for a new install."""
    wizard_mod.run_wizard()


@main.command("doctor")
@click.pass_context
def doctor_cmd(ctx: click.Context) -> None:
    """Diagnose configuration. Reports green/red status for every service."""
    cfg: config_mod.Config = ctx.obj["config"]
    results = doctor_mod.run_all(cfg)
    click.echo("")
    n_fail = n_warn = n_ok = n_skip = 0
    for r in results:
        if r.status == "ok":
            mark = click.style("[OK]  ", fg="green")
            n_ok += 1
        elif r.status == "skip":
            mark = click.style("[--]  ", fg="white")
            n_skip += 1
        elif r.status == "warn":
            mark = click.style("[WARN]", fg="yellow")
            n_warn += 1
        else:
            mark = click.style("[FAIL]", fg="red")
            n_fail += 1
        click.echo(f"  {mark}  {r.name:14s}  {r.message}")
        if r.hint and r.status in ("warn", "fail"):
            click.echo(f"          hint: {r.hint}")
    click.echo("")
    click.echo(
        f"  Summary: {n_ok} ok, {n_warn} warn, {n_fail} fail, {n_skip} skipped"
    )
    if n_fail:
        sys.exit(1)


@main.command("webhook")
@click.option("--host", default=None, help="Bind host (default from EARSHOT_WEBHOOK_HOST).")
@click.option("--port", default=None, type=int, help="Bind port (default from EARSHOT_WEBHOOK_PORT).")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev only).")
@click.pass_context
def webhook_cmd(ctx: click.Context, host: str | None, port: int | None, reload: bool) -> None:
    """Run the interactive-call webhook server (v2).

    Expose this via ``ngrok http <port>`` during development and set the
    resulting URL as ``EARSHOT_WEBHOOK_URL`` in .env. Twilio hits this server
    to drive the conversation turn-by-turn.
    """
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.webhook_url:
        click.echo(
            "EARSHOT_WEBHOOK_URL is not set. Start ngrok (`ngrok http "
            f"{cfg.webhook_port}`), then set the https URL in .env.",
            err=True,
        )
        sys.exit(1)
    try:
        import uvicorn
    except ImportError:
        click.echo("uvicorn is not installed. Reinstall earshot: `pip install -e .`", err=True)
        sys.exit(1)
    host = host or cfg.webhook_host
    port = port or cfg.webhook_port

    # Sweep orphaned 'started' interactions from prior crashed runs so the
    # audit log doesn't drift. Cheap one-shot at boot.
    if cfg.db_path.exists():
        from earshot import agent as agent_mod
        conn = db_mod.connect(cfg.db_path)
        n_swept = agent_mod.sweep_orphaned_interactions(conn)
        conn.close()
        if n_swept:
            click.echo(f"  swept {n_swept} orphaned interaction row(s) → status='abandoned'")

    click.echo(f"Earshot webhook on http://{host}:{port}  (public: {cfg.webhook_url})")
    click.echo(f"  signature validation: {'ON' if cfg.webhook_validate_signature else 'OFF'}")
    click.echo(f"  caps: {cfg.webhook_max_turns} turns / {cfg.webhook_max_call_seconds}s per call")
    if reload:
        uvicorn.run("earshot.webhook.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        from earshot.webhook.app import create_app
        uvicorn.run(create_app(cfg), host=host, port=port, log_level=cfg.log_level.lower())


@main.command("schedule")
@click.option("--at", "at_time", default="18:30", help="Local time HH:MM (default 18:30 = 11:30 EDT from AST).")
@click.option("--remove", is_flag=True, help="Unregister the scheduled task.")
@click.pass_context
def schedule_cmd(ctx: click.Context, at_time: str, remove: bool) -> None:
    """Register a daily Windows Task Scheduler entry that runs `earshot run`.

    On non-Windows, prints a cron line you can paste into `crontab -e`.
    """
    cfg: config_mod.Config = ctx.obj["config"]

    if remove:
        if not scheduler_mod.is_windows():
            click.echo("Nothing to do on non-Windows — remove the cron line yourself.")
            return
        ok, msg = scheduler_mod.unregister_windows()
        click.echo(msg)
        if not ok:
            sys.exit(1)
        return

    if not scheduler_mod.is_windows():
        click.echo("Detected non-Windows. Paste this into `crontab -e`:")
        click.echo("")
        try:
            hh, mm = at_time.split(":")
            click.echo(scheduler_mod.cron_line_for_unix(cfg, int(hh), int(mm)))
        except ValueError:
            click.echo("Invalid --at format. Use HH:MM (24h).", err=True)
            sys.exit(1)
        return

    try:
        hh, mm = at_time.split(":")
        hour, minute = int(hh), int(mm)
    except ValueError:
        click.echo("Invalid --at format. Use HH:MM (24h).", err=True)
        sys.exit(1)

    ok, msg = scheduler_mod.register_windows(cfg, hour, minute)
    click.echo(msg)
    if not ok:
        sys.exit(1)


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Create the SQLite database file and apply the schema."""
    cfg: config_mod.Config = ctx.obj["config"]
    click.echo(f"Initializing database at {cfg.db_path}")
    conn = db_mod.connect(cfg.db_path)
    db_mod.init_schema(conn)
    version = db_mod.get_schema_version(conn)
    click.echo(f"Schema version: {version}")
    conn.close()
    click.echo("OK")


@main.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show DB + config health summary."""
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]

    click.echo(f"earshot v{__version__}  (dry_run={dry_run})")
    click.echo("")
    click.echo("Paths:")
    click.echo(f"  db_path           = {cfg.db_path} {'(exists)' if cfg.db_path.exists() else '(not created — run `earshot init-db`)'}")
    click.echo(f"  data_dir          = {cfg.data_dir}")
    click.echo(f"  channels.yaml     = {cfg.channels_path} {'(exists)' if cfg.channels_path.exists() else '(MISSING)'}")
    click.echo(f"  news_sources.yaml = {cfg.news_sources_path} {'(exists)' if cfg.news_sources_path.exists() else '(MISSING)'}")
    click.echo("")
    click.echo("Channels configured:")
    if cfg.channels:
        for c in cfg.channels:
            kw = f"  keywords={c.keywords}" if c.keywords else ""
            click.echo(f"  - {c.handle:25s} prio={c.priority}  id={c.channel_id}  ({c.name}){kw}")
    else:
        click.echo("  (none)")
    click.echo("")
    click.echo("News sources configured:")
    if cfg.news_sources:
        for s in cfg.news_sources:
            click.echo(f"  - {s.get('name','?')} [{s.get('type','?')}]")
    else:
        click.echo("  (none)")
    click.echo("")
    click.echo("Secrets / env:")
    missing = cfg.missing_keys_for_email()
    click.echo(f"  ANTHROPIC_API_KEY  : {'set' if cfg.anthropic_api_key else 'MISSING'}")
    click.echo(f"  GROQ_API_KEY       : {'set' if cfg.groq_api_key else 'unset (needed module 3+)'}")
    click.echo(f"  YAHOO_EMAIL        : {'set' if cfg.yahoo_email else 'MISSING'}")
    click.echo(f"  YAHOO_APP_PASSWORD : {'set' if cfg.yahoo_app_password else 'MISSING'}")
    click.echo(f"  DIGEST_RECIPIENT   : {cfg.digest_recipient or 'MISSING'}")
    click.echo("")
    if missing:
        click.echo(f"NOT YET READY for live email. Missing: {', '.join(missing)}")
    else:
        click.echo("Ready for live email delivery.")

    if cfg.db_path.exists():
        conn = db_mod.connect(cfg.db_path)
        version = db_mod.get_schema_version(conn)
        ch_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        v_count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        n_count = conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
        g_count = conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        r_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        click.echo("")
        click.echo(f"Database (schema v{version}):")
        click.echo(f"  channels   : {ch_count}")
        click.echo(f"  videos     : {v_count}")
        click.echo(f"  news_items : {n_count}")
        click.echo(f"  glossary   : {g_count}")
        click.echo(f"  runs       : {r_count}")


@main.command("channels")
@click.pass_context
def channels(ctx: click.Context) -> None:
    """List configured channels with their RSS URLs."""
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.channels:
        click.echo("No channels configured. Edit channels.yaml.")
        return
    for c in cfg.channels:
        click.echo(f"{c.handle}  ({c.name})")
        click.echo(f"  channel_id : {c.channel_id}")
        click.echo(f"  priority   : {c.priority}")
        click.echo(f"  keywords   : {c.keywords}")
        click.echo(f"  rss        : {c.rss_url}")
        click.echo("")


@main.command("resolve-channel")
@click.argument("handle")
def resolve_channel_cmd(handle: str) -> None:
    """Resolve a YouTube @handle to its channel ID. Prints a channels.yaml block."""
    try:
        channel_id, name = resolve_handle(handle)
    except ChannelResolutionError as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)
    handle_clean = handle if handle.startswith("@") else f"@{handle}"
    click.echo(f"Resolved {handle_clean} -> {channel_id}  ({name})")
    click.echo("")
    click.echo("Add this block to channels.yaml:")
    click.echo("")
    click.echo(f'  - handle: "{handle_clean}"')
    click.echo(f"    channel_id: {channel_id}")
    click.echo(f'    name: "{name}"')
    click.echo(f"    priority: 1")
    click.echo(f"    keywords: []")


@main.command("detect")
@click.option("--channel", "channel_filter", default=None, help="Only poll one channel (e.g. @AIDailyBrief).")
@click.pass_context
def detect_cmd(ctx: click.Context, channel_filter: str | None) -> None:
    """Poll RSS feeds for new videos. Idempotent.

    On a channel's first sync, all current feed entries are baselined to
    'skipped' state so we don't process the backlog. Subsequent runs catch
    genuinely new uploads.
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.channels:
        click.echo("No channels configured. Edit channels.yaml.", err=True)
        sys.exit(1)
    if not cfg.db_path.exists() and not dry_run:
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)

    conn = db_mod.connect(cfg.db_path) if cfg.db_path.exists() else db_mod.connect(cfg.db_path)
    if not dry_run:
        db_mod.init_schema(conn)

    click.echo(f"Polling {len(cfg.channels)} channel(s){' (dry-run)' if dry_run else ''}...")
    results = detect_all(conn, cfg.channels, dry_run=dry_run, handle_filter=channel_filter)

    total_new = total_baseline = total_prio = total_seen = 0
    for r in results:
        if r.error:
            click.echo(f"  {r.channel_handle:25s} ERROR: {r.error}")
            continue
        click.echo(
            f"  {r.channel_handle:25s} fetched={r.fetched:2d}  "
            f"new={r.inserted_new:2d}  priority={r.priority_flagged:2d}  "
            f"baselined={r.baselined:2d}  seen={r.already_seen:2d}"
        )
        total_new += r.inserted_new
        total_baseline += r.baselined
        total_prio += r.priority_flagged
        total_seen += r.already_seen

    click.echo("")
    click.echo(
        f"Total: new={total_new}  priority={total_prio}  "
        f"baselined={total_baseline}  already_seen={total_seen}"
    )
    if dry_run:
        click.echo("(dry-run: nothing written to the database)")
    conn.close()


@main.command("videos")
@click.option("--state", "state_filter", default=None, help="Filter by state: detected | skipped | transcribed | summarized | notified | failed")
@click.option("-n", "limit", default=20, type=int, help="Max rows to show.")
@click.option("--priority-only", is_flag=True, help="Only show is_priority=1 rows.")
@click.pass_context
def videos_cmd(ctx: click.Context, state_filter: str | None, limit: int, priority_only: bool) -> None:
    """List rows from the videos table, most-recently-detected first."""
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
    sql = """
        SELECT v.video_id, v.title, v.state, v.is_priority, v.published_at,
               c.handle AS channel_handle
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE 1=1
    """
    params: list = []
    if state_filter:
        sql += " AND v.state = ?"
        params.append(state_filter)
    if priority_only:
        sql += " AND v.is_priority = 1"
    sql += " ORDER BY v.detected_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        click.echo("No matching videos.")
        return
    for r in rows:
        prio_marker = "!" if r["is_priority"] else " "
        title = r["title"] or ""
        if len(title) > 70:
            title = title[:67] + "..."
        click.echo(
            f"  [{r['state']:10s}] {prio_marker} {r['channel_handle']:20s} "
            f"{r['video_id']}  {title}"
        )
    conn.close()


@main.command("transcribe")
@click.option("-n", "limit", default=None, type=int, help="Max videos to process this run.")
@click.option("--video-id", default=None, help="Force-transcribe a specific video (any state).")
@click.pass_context
def transcribe_cmd(ctx: click.Context, limit: int | None, video_id: str | None) -> None:
    """Transcribe videos in 'detected' state. Captions first, ASR as fallback.

    With --video-id, processes the named video regardless of current state
    (useful to manually pull a baselined episode through the pipeline).
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)

    conn = db_mod.connect(cfg.db_path)
    pending = select_pending_transcribe(conn, limit=limit, video_id=video_id)
    if not pending:
        click.echo("No videos to transcribe. (Run `earshot detect` first, or pass --video-id.)")
        return

    click.echo(
        f"Transcribing {len(pending)} video(s){' (dry-run)' if dry_run else ''}..."
    )
    n_ok = n_fail = 0
    total_chars = 0
    for row in pending:
        result = transcribe_video(conn, row, cfg, dry_run=dry_run)
        if result.status == "transcribed":
            n_ok += 1
            total_chars += result.chars
            click.echo(
                f"  OK   {result.video_id}  source={result.source:14s}  "
                f"chars={result.chars:>7,}  -> {result.transcript_path}"
            )
        else:
            n_fail += 1
            click.echo(f"  FAIL {result.video_id}  {result.error}")

    click.echo("")
    click.echo(
        f"Summary: transcribed={n_ok} failed={n_fail} total_chars={total_chars:,}"
        + ("  (dry-run: no files or DB writes)" if dry_run else "")
    )
    conn.close()


@main.command("analyze")
@click.option("-n", "limit", default=None, type=int, help="Max videos to process this run.")
@click.option("--video-id", default=None, help="Analyze a specific video regardless of state.")
@click.pass_context
def analyze_cmd(ctx: click.Context, limit: int | None, video_id: str | None) -> None:
    """Summarize transcribed videos and extract concepts via Claude.

    Cost-capped via MAX_LLM_COST_PER_RUN_USD (default $2). Token usage and
    cost are persisted in the `runs` table.
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)

    conn = db_mod.connect(cfg.db_path)
    pending = select_pending_analyze(conn, limit=limit, video_id=video_id)
    if not pending:
        click.echo("No videos to analyze. (Run `earshot transcribe` first, or pass --video-id.)")
        return

    client = make_client(cfg)
    if client is None and not dry_run:
        click.echo("ANTHROPIC_API_KEY not set. Set it in .env or use --dry-run.", err=True)
        sys.exit(1)

    run_id = None if dry_run else start_run(conn)
    click.echo(
        f"Analyzing {len(pending)} video(s){' (dry-run)' if dry_run else ''}  "
        f"(model={'(dry-run)' if dry_run else 'haiku-4.5'}, run_id={run_id})"
    )

    n_ok = n_fail = 0
    total_new = total_dup = 0
    total_in = total_out = 0
    total_cost = 0.0
    capped = False

    for row in pending:
        try:
            result = analyze_video(conn, row, cfg, client, run_id=run_id, dry_run=dry_run)
        except CostCapReached as e:
            click.echo(f"  STOP: {e}")
            capped = True
            break
        if result.status == "summarized":
            n_ok += 1
            total_new += result.new_concepts
            total_dup += result.dup_concepts
            total_in += result.input_tokens
            total_out += result.output_tokens
            total_cost += result.cost_usd
            click.echo(
                f"  OK   {result.video_id}  "
                f"concepts: +{result.new_concepts} new / {result.dup_concepts} known  "
                f"tokens: {result.input_tokens:>6,} in / {result.output_tokens:>5,} out  "
                f"cost: ${result.cost_usd:.4f}"
            )
        elif result.status == "skipped":
            click.echo(f"  SKIP {result.video_id}  {result.error}")
        else:
            n_fail += 1
            click.echo(f"  FAIL {result.video_id}  {result.error}")

    if run_id is not None:
        finish_run(conn, run_id, status="ok" if not capped else "capped")

    click.echo("")
    click.echo(
        f"Summary: ok={n_ok} failed={n_fail}  "
        f"concepts: +{total_new} new / {total_dup} known  "
        f"tokens: {total_in:,} in / {total_out:,} out  "
        f"cost: ${total_cost:.4f}"
        + ("  (dry-run)" if dry_run else "")
    )
    conn.close()


@main.command("show")
@click.argument("item_id")
@click.pass_context
def show_cmd(ctx: click.Context, item_id: str) -> None:
    """Pretty-print the stored analysis for a video OR news item.

    Numeric ITEM_ID is treated as a news_items.id; anything else is treated
    as a YouTube video_id (e.g. ``4sN_zR8Vf94``).
    """
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)

    # Numeric → news item
    if item_id.isdigit():
        _show_news(conn, int(item_id))
        conn.close()
        return

    # Otherwise → video
    video_id = item_id
    row = conn.execute(
        """
        SELECT v.video_id, v.title, v.url, v.state, v.is_priority, v.summary_json,
               v.transcript_source, c.handle, c.name AS channel_name
          FROM videos v
          JOIN channels c ON c.channel_id = v.channel_id
         WHERE v.video_id = ?
        """,
        (video_id,),
    ).fetchone()
    if not row:
        click.echo(f"No video with id {video_id}", err=True)
        sys.exit(1)

    prio = " [PRIORITY]" if row["is_priority"] else ""
    click.echo(f"{row['handle']} / {row['video_id']}{prio}")
    click.echo(f"{row['title']}")
    click.echo(f"{row['url']}")
    click.echo(f"state={row['state']}  transcript_source={row['transcript_source']}")
    click.echo("")

    if not row["summary_json"]:
        click.echo("No analysis on file. Run `earshot analyze --video-id "
                   f"{video_id}` to generate one.")
        return

    a = json.loads(row["summary_json"])
    click.echo("Summary:")
    click.echo(f"  {a.get('summary','(none)')}")
    click.echo("")

    # Defensive rendering: Claude sometimes returns string arrays as a list of
    # single characters or wraps content in legacy XML tool-call syntax. Normalize.
    takeaways = _normalize_string_list(a.get("key_takeaways") or [])
    if takeaways:
        click.echo("Key takeaways:")
        for t in takeaways:
            click.echo(f"  - {t}")
        click.echo("")
    quotes = _normalize_string_list(a.get("notable_quotes") or [])
    if quotes:
        click.echo("Notable quotes:")
        for q in quotes:
            click.echo(f'  "{q}"')
        click.echo("")

    concepts = a.get("concepts") or []
    # Mark which were new (first_seen_video_id = this video) vs already-known.
    if concepts:
        click.echo(f"Concepts ({len(concepts)}):")
        for c in concepts:
            term = c.get("term", "")
            cat = c.get("category", "concept")
            gloss = conn.execute(
                "SELECT first_seen_video_id FROM glossary WHERE normalized_term = ?",
                (term.strip().lower(),),
            ).fetchone()
            tag = "[NEW]  " if (gloss and gloss["first_seen_video_id"] == video_id) else "[known]"
            click.echo(f"  {tag} {term} ({cat})")
            if c.get("definition"):
                click.echo(f"          {c['definition']}")
            if c.get("why_it_matters"):
                click.echo(f"          why: {c['why_it_matters']}")
    conn.close()


def _show_news(conn, news_id: int) -> None:
    """Pretty-print one news item. Helper for `show` when the arg is numeric."""
    row = conn.execute(
        """SELECT id, source, title, url, summary, importance_score, state,
                  published_at, seen_at, notified_at
             FROM news_items WHERE id = ?""",
        (news_id,),
    ).fetchone()
    if not row:
        click.echo(f"No news item with id {news_id}", err=True)
        sys.exit(1)

    score = row["importance_score"]
    score_str = f"[{score}]" if score is not None else "[—]"
    click.echo(f"{score_str} {row['source']} / news #{row['id']}")
    click.echo(f"{row['title']}")
    click.echo(f"{row['url']}")
    click.echo(f"state={row['state']}  published={row['published_at'] or '—'}  seen={row['seen_at']}")
    if row["notified_at"]:
        click.echo(f"notified_at={row['notified_at']}")
    click.echo("")
    if row["summary"]:
        click.echo("Summary:")
        click.echo(f"  {row['summary']}")


@main.command("scout")
@click.option("--skip-scoring", is_flag=True, help="Fetch only — do not call Claude.")
@click.pass_context
def scout_cmd(ctx: click.Context, skip_scoring: bool) -> None:
    """Fetch AI news from configured sources, dedupe, score via Claude.

    With --dry-run: fetches but does not write to DB or call Claude.
    With --skip-scoring: writes to DB but does not call Claude (lets you
    inspect the raw items before paying for scoring).
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)
    if not cfg.news_sources:
        click.echo("No news sources configured. Edit news_sources.yaml.", err=True)
        sys.exit(1)

    conn = db_mod.connect(cfg.db_path)
    client = make_client(cfg)
    if client is None and not dry_run and not skip_scoring:
        click.echo("ANTHROPIC_API_KEY not set. Use --skip-scoring or --dry-run, or set the key.", err=True)
        sys.exit(1)

    run_id = None if dry_run else start_run(conn)
    click.echo(
        f"Scouting {len(cfg.news_sources)} source(s)"
        f"{' (dry-run)' if dry_run else ''}"
        f"{' (skip-scoring)' if skip_scoring else ''}"
        f"  run_id={run_id}"
    )

    fetch_results, summary = run_scout(
        conn, cfg, client, dry_run=dry_run,
        skip_scoring=skip_scoring, run_id=run_id,
    )

    for r in fetch_results:
        if r.error:
            click.echo(f"  {r.source_name:25s} ERROR: {r.error}")
        else:
            click.echo(
                f"  {r.source_name:25s} fetched={r.fetched:3d}  "
                f"new={r.inserted:3d}  baselined={r.baselined:4d}  seen={r.already_seen:3d}"
            )

    if run_id is not None:
        finish_run(conn, run_id, status="ok")

    click.echo("")
    click.echo(
        f"Summary: inserted={summary.total_inserted}  "
        f"baselined={summary.total_baselined}  "
        f"already_seen={summary.total_already_seen}  "
        f"scored={summary.total_scored}  "
        f"scoring_cost=${summary.scoring_cost_usd:.4f}"
        + ("  (dry-run)" if dry_run else "")
    )
    conn.close()


@main.command("news")
@click.option("--min-score", default=None, type=int, help="Filter to items with importance_score >= N.")
@click.option("--state", "state_filter", default=None, help="seen | scored | notified | skipped")
@click.option("-n", "limit", default=20, type=int)
@click.pass_context
def news_cmd(ctx: click.Context, min_score: int | None, state_filter: str | None, limit: int) -> None:
    """List news items, most-recently-seen first."""
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
    sql = "SELECT id, source, title, url, state, importance_score, published_at FROM news_items WHERE 1=1"
    params: list = []
    if min_score is not None:
        sql += " AND importance_score >= ?"
        params.append(min_score)
    if state_filter:
        sql += " AND state = ?"
        params.append(state_filter)
    sql += " ORDER BY seen_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        click.echo("No matching news items.")
        return
    for r in rows:
        score = r["importance_score"]
        score_str = f"[{score}]" if score is not None else "[-]"
        title = (r["title"] or "")[:80]
        click.echo(
            f"  {score_str} {r['state']:8s} {r['source']:25s} {title}"
        )
        click.echo(f"           {r['url']}")
    conn.close()


@main.command("digest")
@click.option("--instant", is_flag=True, help="Build instant alerts instead of the daily digest.")
@click.pass_context
def digest_cmd(ctx: click.Context, instant: bool) -> None:
    """Build the digest payload(s). Archives markdown + html under data/digests/.

    Without --dry-run, items are marked notified (state='notified') so they
    don't appear in subsequent digests. With --dry-run, payloads are still
    written to disk but the DB is not modified.

    Email delivery happens in module 7 (Yahoo SMTP notifier).
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)

    anthropic_client = make_client(cfg)
    notifiers = make_notifiers(cfg, conn=conn, anthropic_client=anthropic_client, dry_run=dry_run)
    click.echo(f"Notifiers: {', '.join(n.channel for n in notifiers)}")

    def _fanout(p: digest_mod.DigestPayload, md_path) -> bool:
        """Send to every notifier, log alerts, return True if any succeeded."""
        any_sent = False
        for n in notifiers:
            result = n.send(p)
            # 'skipped' = notifier opted out for this payload (e.g. v1.5 voice
            # skipping an instant alert when v2 is configured). Stay quiet — no
            # log line, no alert row.
            if result.status == "skipped":
                continue
            click.echo(
                f"               send:    {result.status} via {result.channel}"
                + (f" -> {result.recipient}" if result.recipient and result.recipient != 'stdout' else "")
                + (f"  ERROR: {result.error}" if result.error else "")
            )
            if not dry_run:
                digest_mod.record_alert(
                    conn, p, channel=result.channel, recipient=result.recipient,
                    status=result.status, payload_path=md_path, error=result.error,
                )
            if result.status == "sent":
                any_sent = True
        return any_sent

    if instant:
        payloads = digest_mod.build_instant_alerts(conn, cfg)
        if not payloads:
            click.echo("No instant alerts to send.")
            conn.close()
            return
        click.echo(f"Built {len(payloads)} instant alert(s){' (dry-run)' if dry_run else ''}:")
        for p in payloads:
            md_path = digest_mod.archive_payload(p, cfg.data_dir)
            click.echo(f"  {p.digest_type:14s} {p.subject}")
            click.echo(f"               archive: {md_path}")
            any_sent = _fanout(p, md_path)
            if not dry_run and any_sent:
                digest_mod.mark_notified(conn, p)
    else:
        payload = digest_mod.build_daily(conn, cfg)
        if payload is None:
            click.echo("Nothing to digest (no unnotified summarized videos or scored news above threshold).")
            conn.close()
            return
        md_path = digest_mod.archive_payload(payload, cfg.data_dir)
        click.echo(f"Built daily digest{' (dry-run)' if dry_run else ''}:")
        click.echo(f"  subject: {payload.subject}")
        click.echo(f"  videos:  {len(payload.videos)}")
        click.echo(f"  news:    {len(payload.news_items)}")
        click.echo(f"  archive: {md_path}")
        any_sent = _fanout(payload, md_path)
        if not dry_run and any_sent:
            digest_mod.mark_notified(conn, payload)

    # Always regenerate the study queue after a digest run.
    sq_path = config_mod.REPO_ROOT / "study_queue.md"
    n = study_queue_mod.write_file(conn, sq_path)
    click.echo(f"  study_queue.md updated ({n} terms) -> {sq_path}")
    conn.close()


@main.command("test-email")
@click.pass_context
def test_email_cmd(ctx: click.Context) -> None:
    """Send a one-line test email to DIGEST_RECIPIENT. Verifies SMTP creds work."""
    cfg: config_mod.Config = ctx.obj["config"]
    missing = cfg.missing_keys_for_email()
    if missing:
        click.echo(f"Email not configured. Missing: {', '.join(missing)}", err=True)
        sys.exit(1)
    notifier = EmailYahooNotifier(
        sender_email=cfg.yahoo_email or "",
        app_password=cfg.yahoo_app_password or "",
        recipient=cfg.digest_recipient or "",
    )
    click.echo(f"Sending test email from {cfg.yahoo_email} to {cfg.digest_recipient}...")
    result = notifier.send_test()
    if result.status == "sent":
        click.echo("OK — check your inbox.")
    else:
        click.echo(f"FAILED: {result.error}", err=True)
        sys.exit(2)


@main.command("call")
@click.option("--video-id", default=None, help="Voice-call about a specific video (any state).")
@click.option("--news-id", default=None, type=int, help="Voice-call about a specific news item.")
@click.option("--daily", is_flag=True, help="Voice-call the daily digest content (regenerated, not marked notified).")
@click.option(
    "--interactive", is_flag=True,
    help="Place an interactive (v2) call instead of a TTS readout. Requires the webhook server to be running.",
)
@click.option(
    "--yes", "skip_confirm", is_flag=True,
    help="Skip the interactive-call cost confirmation prompt.",
)
@click.pass_context
def call_cmd(
    ctx: click.Context, video_id: str | None, news_id: int | None,
    daily: bool, interactive: bool, skip_confirm: bool,
) -> None:
    """Voice-only call for a specific item or the daily digest.

    Does NOT mark items notified and does NOT send email — it just dials the
    phone. Useful for replaying a missed call without disturbing the
    notification state machine.
    """
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    if cfg.missing_keys_for_voice():
        click.echo(f"Voice not configured. Missing: {', '.join(cfg.missing_keys_for_voice())}", err=True)
        sys.exit(1)

    n_flags = sum([bool(video_id), bool(news_id), daily])
    if n_flags != 1:
        click.echo("Specify exactly one of --video-id, --news-id, or --daily.", err=True)
        sys.exit(2)

    conn = db_mod.connect(cfg.db_path)
    payload: digest_mod.DigestPayload | None = None

    if video_id:
        row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        if not row:
            click.echo(f"No video with id {video_id}", err=True)
            sys.exit(1)
        if not row["summary_json"]:
            click.echo(f"Video {video_id} has no summary yet. Run `earshot analyze --video-id {video_id}` first.", err=True)
            sys.exit(1)
        v = digest_mod._video_to_digest(conn, row)
        payload = digest_mod.DigestPayload(
            digest_type="instant_video",
            date_local=digest_mod.today_in_tz(cfg.digest_timezone),
            subject=f"Earshot replay — {v.channel_handle}: {v.title}",
            videos=[v],
        )
    elif news_id:
        row = conn.execute("SELECT * FROM news_items WHERE id = ?", (news_id,)).fetchone()
        if not row:
            click.echo(f"No news item with id {news_id}", err=True)
            sys.exit(1)
        n = digest_mod._news_to_digest(row)
        payload = digest_mod.DigestPayload(
            digest_type="instant_news",
            date_local=digest_mod.today_in_tz(cfg.digest_timezone),
            subject=f"Earshot replay — {n.source}: {n.title[:80]}",
            news_items=[n],
        )
    elif daily:
        # Rebuild the daily payload from anything currently scored, ignoring
        # notified_at filter so we always get something. (Replays from
        # archived runs aren't supported yet — this regenerates the script.)
        rows = list(conn.execute(
            """SELECT * FROM news_items
                WHERE state IN ('scored','notified')
                  AND importance_score >= ?
                ORDER BY importance_score DESC, seen_at DESC LIMIT 15""",
            (cfg.digest_min_news_score,),
        ).fetchall())
        if not rows:
            click.echo("No scored news above threshold to call about.", err=True)
            sys.exit(1)
        news_items = [digest_mod._news_to_digest(r) for r in rows]
        payload = digest_mod.DigestPayload(
            digest_type="daily",
            date_local=digest_mod.today_in_tz(cfg.digest_timezone),
            subject=f"Earshot replay — daily digest ({len(news_items)} items)",
            news_items=news_items,
        )

    client = make_client(cfg)
    if client is None:
        click.echo("ANTHROPIC_API_KEY required to render the voice script.", err=True)
        sys.exit(1)

    if interactive:
        if not cfg.webhook_url:
            click.echo(
                "EARSHOT_WEBHOOK_URL is not set. Start the webhook server "
                "(`earshot webhook`) behind ngrok and set the URL in .env.",
                err=True,
            )
            sys.exit(1)
        n_items = len(payload.videos) + len(payload.news_items)
        est_low = 0.10
        est_high = 0.14
        click.echo(
            f"This will place an INTERACTIVE call covering {n_items} item(s). "
            f"Estimated cost: ${est_low:.2f}–${est_high:.2f}."
        )
        if not skip_confirm and not click.confirm("Proceed?", default=False):
            click.echo("Aborted.")
            return
        notifier = TwilioInteractiveNotifier(
            conn=conn,
            account_sid=cfg.twilio_account_sid or "",
            auth_token=cfg.twilio_auth_token or "",
            from_number=cfg.twilio_from_number or "",
            to_number=cfg.twilio_to_number or "",
            webhook_url=cfg.webhook_url,
        )
        click.echo(f"Placing interactive call to {cfg.twilio_to_number}...")
        result = notifier.send(payload)
        if result.status == "sent":
            click.echo("OK — answer your phone. The conversation drives via the webhook.")
        else:
            click.echo(f"FAILED: {result.error}", err=True)
            sys.exit(2)
        conn.close()
        return

    notifier = TwilioTtsNotifier(
        account_sid=cfg.twilio_account_sid or "",
        auth_token=cfg.twilio_auth_token or "",
        from_number=cfg.twilio_from_number or "",
        to_number=cfg.twilio_to_number or "",
        voice=cfg.twilio_voice,
        anthropic_client=client,
    )
    click.echo(f"Placing call to {cfg.twilio_to_number}...")
    result = notifier.send(payload)
    if result.status == "sent":
        click.echo("OK — answer your phone.")
    else:
        click.echo(f"FAILED: {result.error}", err=True)
        sys.exit(2)
    conn.close()


@main.command("test-call")
@click.pass_context
def test_call_cmd(ctx: click.Context) -> None:
    """Place a 1-line test call to TWILIO_TO_NUMBER. Verifies Twilio creds."""
    cfg: config_mod.Config = ctx.obj["config"]
    missing = cfg.missing_keys_for_voice()
    if missing:
        click.echo(f"Voice channel not configured. Missing: {', '.join(missing)}", err=True)
        sys.exit(1)
    notifier = TwilioTtsNotifier(
        account_sid=cfg.twilio_account_sid or "",
        auth_token=cfg.twilio_auth_token or "",
        from_number=cfg.twilio_from_number or "",
        to_number=cfg.twilio_to_number or "",
        voice=cfg.twilio_voice,
        anthropic_client=None,  # type: ignore[arg-type]  # not used by send_test
    )
    click.echo(f"Placing test call from {cfg.twilio_from_number} to {cfg.twilio_to_number}...")
    result = notifier.send_test()
    if result.status == "sent":
        click.echo("OK — answer your phone.")
    else:
        click.echo(f"FAILED: {result.error}", err=True)
        sys.exit(2)


@main.command("interactions")
@click.option("-n", "limit", default=10, type=int, help="Max rows to show.")
@click.option("--status", "status_filter", default=None,
              help="Filter by status: started | completed | failed | abandoned.")
@click.pass_context
def interactions_cmd(ctx: click.Context, limit: int, status_filter: str | None) -> None:
    """List recent v2 interactive calls — duration, cost, journal-note presence."""
    from earshot import agent as agent_mod

    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
    rows = agent_mod.list_recent_interactions(conn, limit=limit, status_filter=status_filter)
    if not rows:
        click.echo("No interactions yet." if not status_filter else
                   f"No interactions with status={status_filter}.")
        return
    click.echo(f"  {'id':>4}  {'started':19s}  {'status':10s}  {'dur':>5s}  "
               f"{'turns':>5s}  {'cost':>7s}  journal?")
    for ix in rows:
        dur = f"{ix.duration_seconds}s" if ix.duration_seconds is not None else "—"
        journal = "yes" if ix.journal_note else "—"
        click.echo(
            f"  {ix.id:>4d}  {ix.started_at[:19]:19s}  {ix.status:10s}  "
            f"{dur:>5s}  {len(ix.turns):>5d}  ${ix.cost_usd:>6.4f}  {journal}"
        )
    conn.close()


@main.command("replay")
@click.argument("interaction_id", type=int)
@click.pass_context
def replay_cmd(ctx: click.Context, interaction_id: int) -> None:
    """Pretty-print the full transcript of a v2 interactive call."""
    from earshot import agent as agent_mod

    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
    ix = agent_mod.get_interaction_by_id(conn, interaction_id)
    if ix is None:
        click.echo(f"No interaction with id {interaction_id}.", err=True)
        sys.exit(1)

    click.echo(f"Interaction #{ix.id}  call_sid={ix.call_sid or '—'}")
    click.echo(f"  status={ix.status}  started={ix.started_at}  ended={ix.ended_at or '—'}"
               f"  duration={ix.duration_seconds}s" if ix.duration_seconds else
               f"  status={ix.status}  started={ix.started_at}  ended={ix.ended_at or '—'}")
    click.echo(f"  tokens: {ix.tokens_in:,} in / {ix.tokens_out:,} out   cost: ${ix.cost_usd:.4f}")
    if ix.error:
        click.echo(f"  error: {ix.error}")
    click.echo(f"  items: {len(ix.item_refs)}")
    for r in ix.item_refs:
        click.echo(f"    - {r.kind}/{r.ref_id}")
    click.echo("")

    if not ix.turns:
        click.echo("  (no turns recorded — call ended before reaching the webhook)")
    else:
        click.echo("Turns:")
        for t in ix.turns:
            item_label = ""
            if t.item_ref:
                item_label = f"  [{t.item_ref.kind}/{t.item_ref.ref_id}]"
            click.echo(f"  #{t.turn} {t.phase}{item_label}")
            if t.question:
                click.echo(f"     agent : {t.question}")
            if t.answer_text is not None:
                click.echo(f"     user  : {t.answer_text or '(silence)'}")
            if t.action:
                click.echo(f"     action: {t.action}")

    if ix.journal_note:
        click.echo("")
        click.echo(f"Journal: {ix.journal_note}")
    conn.close()


@main.command("study-queue")
@click.pass_context
def study_queue_cmd(ctx: click.Context) -> None:
    """Re-export the glossary as study_queue.md in the repo root."""
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
    sq_path = config_mod.REPO_ROOT / "study_queue.md"
    n = study_queue_mod.write_file(conn, sq_path)
    click.echo(f"Wrote {n} term(s) to {sq_path}")
    conn.close()


@main.command("run")
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the full pipeline: detect → transcribe → analyze → scout → digest.

    Voice (Twilio) and email (Yahoo SMTP) notifications fire as part of the
    digest step if their respective creds are configured.
    """
    cfg: config_mod.Config = ctx.obj["config"]
    dry_run: bool = ctx.obj["dry_run"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}. Run `earshot init-db` first.", err=True)
        sys.exit(1)

    click.echo(f"=== earshot run {'(dry-run)' if dry_run else ''} ===")
    click.echo("")
    ctx.invoke(detect_cmd, channel_filter=None)
    click.echo("")
    ctx.invoke(transcribe_cmd, limit=None, video_id=None)
    click.echo("")
    ctx.invoke(analyze_cmd, limit=None, video_id=None)
    click.echo("")
    ctx.invoke(scout_cmd, skip_scoring=False)
    click.echo("")
    ctx.invoke(digest_cmd, instant=True)
    click.echo("")
    ctx.invoke(digest_cmd, instant=False)


if __name__ == "__main__":
    main()
