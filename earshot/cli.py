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

from earshot import __version__, config as config_mod, db as db_mod
from earshot.channel_resolver import ChannelResolutionError, resolve_handle
from earshot.detector import detect_all
from earshot.transcriber import select_pending, transcribe_video


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
    pending = select_pending(conn, limit=limit, video_id=video_id)
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


@main.command("run")
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the full pipeline. (Not yet implemented — module 4+.)"""
    dry_run: bool = ctx.obj["dry_run"]
    click.echo(f"`earshot run` is not implemented yet (dry_run={dry_run}).")
    click.echo("Modules 4+ will add summarize → notify. Available now:")
    click.echo("  earshot init-db")
    click.echo("  earshot status")
    click.echo("  earshot channels")
    click.echo("  earshot resolve-channel @somehandle")
    click.echo("  earshot detect [--dry-run] [--channel @handle]")
    click.echo("  earshot videos [--state X] [-n N] [--priority-only]")
    click.echo("  earshot transcribe [-n N] [--video-id ID]")


if __name__ == "__main__":
    main()
