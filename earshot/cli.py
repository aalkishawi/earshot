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
@click.argument("video_id")
@click.pass_context
def show_cmd(ctx: click.Context, video_id: str) -> None:
    """Pretty-print the stored analysis for a video."""
    cfg: config_mod.Config = ctx.obj["config"]
    if not cfg.db_path.exists():
        click.echo(f"DB not found at {cfg.db_path}.", err=True)
        sys.exit(1)
    conn = db_mod.connect(cfg.db_path)
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
            click.echo(f"               -> {md_path}")
            if not dry_run:
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
        if not dry_run:
            digest_mod.mark_notified(conn, payload)

    # Always regenerate the study queue after a digest run.
    sq_path = config_mod.REPO_ROOT / "study_queue.md"
    n = study_queue_mod.write_file(conn, sq_path)
    click.echo(f"  study_queue.md updated ({n} terms) -> {sq_path}")
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
    """Run the full pipeline. (Not yet implemented — module 7+.)"""
    dry_run: bool = ctx.obj["dry_run"]
    click.echo(f"`earshot run` is not implemented yet (dry_run={dry_run}).")
    click.echo("Module 7 will add email delivery. Available now:")
    click.echo("  earshot init-db")
    click.echo("  earshot status")
    click.echo("  earshot channels")
    click.echo("  earshot resolve-channel @somehandle")
    click.echo("  earshot detect [--dry-run] [--channel @handle]")
    click.echo("  earshot videos [--state X] [-n N] [--priority-only]")
    click.echo("  earshot transcribe [-n N] [--video-id ID]")
    click.echo("  earshot analyze [-n N] [--video-id ID] [--dry-run]")
    click.echo("  earshot show VIDEO_ID")
    click.echo("  earshot scout [--skip-scoring]")
    click.echo("  earshot news [--min-score N] [--state X] [-n N]")
    click.echo("  earshot digest [--instant] [--dry-run]")
    click.echo("  earshot study-queue")


if __name__ == "__main__":
    main()
