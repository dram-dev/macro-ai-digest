"""CLI entry point — `digest ingest <source>`, `digest stats`, `digest recent`."""
from __future__ import annotations

import logging
import sys

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from digest import db
from digest.config import settings

console = Console()


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


INGESTORS = {
    "gmail": "digest.ingest.gmail:GmailIngestor",
    "reddit": "digest.ingest.reddit:RedditIngestor",
    "rss": "digest.ingest.rss:RSSIngestor",
    "substack": "digest.ingest.substack:SubstackIngestor",
    "arxiv": "digest.ingest.arxiv:ArXivIngestor",
    "edgar": "digest.ingest.edgar:EdgarIngestor",
    "insider": "digest.ingest.insider:InsiderIngestor",
    "ftd": "digest.ingest.ftd:FTDIngestor",
    "fred": "digest.ingest.fred:FREDIngestor",
    "cboe": "digest.ingest.cboe:CBOEIngestor",
    "cftc": "digest.ingest.cftc:CFTCIngestor",
    "yahoo": "digest.ingest.yahoo:YahooIngestor",
    "hn": "digest.ingest.hackernews:HNIngestor",
    "clipped": "digest.ingest.clipped:ClippedIngestor",
    "huggingface": "digest.ingest.huggingface:HFIngestor",
}


def _load(dotted: str):
    module_path, class_name = dotted.split(":")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


@click.group()
def main() -> None:
    """Macro + AI Digest CLI."""
    _setup_logging()


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS.keys()) + ["all"]))
@click.option("--run-type", default="manual", help="Tag for run_log (am/pm/manual)")
def ingest(source: str, run_type: str) -> None:
    """Ingest from one source or all."""
    db.init_db()
    targets = list(INGESTORS.keys()) if source == "all" else [source]

    total_fetched = 0
    total_new = 0
    for name in targets:
        console.rule(f"[bold cyan]{name}")
        try:
            cls = _load(INGESTORS[name])
            inst = cls()
            fetched, new = inst.run(run_type=run_type)
            total_fetched += fetched
            total_new += new
            console.print(f"[green]✓[/green] {name}: fetched={fetched} new={new}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗[/red] {name}: {exc}")

    if source == "all":
        console.rule("[bold]summary")
        console.print(f"total fetched={total_fetched} new={total_new}")


@main.command()
def stats() -> None:
    """Show item counts by source, plus triage and summarizer status."""
    db.init_db()
    counts = db.item_stats()
    table = Table(title="Items by source")
    table.add_column("Source")
    table.add_column("Count", justify="right")
    for src, n in counts.items():
        table.add_row(src, str(n))
    if not counts:
        console.print("[yellow]No items yet. Try:[/yellow] digest ingest all")
        return
    console.print(table)

    # Phase 2 status
    triage = db.triage_stats()
    if triage:
        t2 = Table(title="Triage status")
        t2.add_column("Decision")
        t2.add_column("Count", justify="right")
        for k, v in triage.items():
            t2.add_row(k, str(v))
        console.print(t2)

    sum_stats = db.summarizer_stats(days=7)
    if sum_stats:
        t3 = Table(title="Summarizer activity (7d)")
        t3.add_column("Backend")
        t3.add_column("Items", justify="right")
        t3.add_column("In chars", justify="right")
        t3.add_column("Out chars", justify="right")
        for backend, info in sum_stats.items():
            t3.add_row(
                backend,
                str(info.get("n", 0)),
                str(info.get("in_chars") or 0),
                str(info.get("out_chars") or 0),
            )
        console.print(t3)


@main.command()
@click.option("--limit", default=200, help="Max items to triage in this run")
def triage(limit: int) -> None:
    """Run local Qwen triage over pending items."""
    from digest.triage import run_triage

    db.init_db()
    console.rule("[bold cyan]triage")
    counts = run_triage(limit=limit)
    console.print(
        f"[green]✓[/green] triage: pending={counts['pending']} "
        f"kept={counts['kept']} dropped={counts['dropped']} errors={counts['errors']}"
    )


@main.command()
@click.option(
    "--limit",
    default=None,
    type=int,
    help=f"Max items to summarize (overrides SUMMARIZER_MAX_PER_RUN)",
)
def summarize(limit: int | None) -> None:
    """Summarize the top-scored items that passed triage."""
    from digest.config import settings as _settings
    from digest.summarize import run_summarize

    db.init_db()
    console.rule(f"[bold cyan]summarize ({_settings.summarizer_backend})")
    counts = run_summarize(limit=limit)
    console.print(
        f"[green]✓[/green] summarize: ready={counts['ready']} "
        f"succeeded={counts['succeeded']} failed={counts['failed']}"
    )


@main.command()
@click.option("--run-type", default="manual", help="Tag for run_log (am/pm/manual)")
@click.option("--skip-publish", is_flag=True, help="Don't write to Obsidian (debug)")
def pipeline(run_type: str, skip_publish: bool) -> None:
    """Full pipeline: ingest → triage → summarize → publish to Obsidian."""
    from digest.triage import run_triage
    from digest.summarize import run_summarize
    from digest.obsidian import publish as obs_publish

    db.init_db()

    # Stage 1 — ingest all
    console.rule("[bold cyan]stage 1: ingest")
    for name in INGESTORS:
        try:
            cls = _load(INGESTORS[name])
            fetched, new = cls().run(run_type=run_type)
            console.print(f"  [green]✓[/green] {name}: {fetched}/{new}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗[/red] {name}: {exc}")

    # Stage 2 — triage everything new
    console.rule("[bold cyan]stage 2: triage")
    t = run_triage()
    console.print(
        f"  [green]✓[/green] kept={t['kept']} dropped={t['dropped']} errors={t['errors']}"
    )

    # Stage 3a — summarize clipped items first, with NO cap. The user curated
    # these by hand; they shouldn't lose to RSS noise in the cap fight.
    console.rule("[bold cyan]stage 3a: summarize (clipped, uncapped)")
    sc = run_summarize(source="clipped", uncapped=True)
    console.print(
        f"  [green]✓[/green] clipped: succeeded={sc['succeeded']} "
        f"failed={sc['failed']} ready={sc['ready']}"
    )

    # Stage 3b — summarize the rest, capped per SUMMARIZER_MAX_PER_RUN.
    console.rule("[bold cyan]stage 3b: summarize (rest, capped)")
    s = run_summarize()
    console.print(
        f"  [green]✓[/green] succeeded={s['succeeded']} failed={s['failed']} ready={s['ready']}"
    )

    # Stage 3c — cross-item connection detection (best-effort, non-blocking)
    console.rule("[bold cyan]stage 3c: connections")
    try:
        from digest.connections import run_connections
        threads = run_connections()
        console.print(f"  [green]✓[/green] {len(threads)} connection threads found")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] connections skipped: {exc}")

    # Stage 3d — multi-persona ensemble scoring (best-effort, non-blocking)
    console.rule("[bold cyan]stage 3d: ensemble scoring")
    try:
        from digest.ensemble import run_ensemble
        ec = run_ensemble()
        console.print(
            f"  [green]✓[/green] ensemble: succeeded={ec['succeeded']} failed={ec['failed']}"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] ensemble skipped: {exc}")

    # Stage 3e — financial sentiment classification (best-effort, non-blocking)
    console.rule("[bold cyan]stage 3e: sentiment")
    try:
        from digest.sentiment import run_sentiment
        sc = run_sentiment()
        console.print(
            f"  [green]✓[/green] sentiment: processed={sc['processed']} "
            f"succeeded={sc['succeeded']} failed={sc['failed']}"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] sentiment skipped: {exc}")

    # Stage 3f — entity extraction + ticker linkage (best-effort, non-blocking)
    console.rule("[bold cyan]stage 3f: entities")
    try:
        from digest.entities import run_entities
        enc = run_entities()
        console.print(
            f"  [green]✓[/green] entities: processed={enc['processed']} "
            f"with_entities={enc['with_entities']}"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] entities skipped: {exc}")

    # Stage 3h — stock price tracker with digest signal overlays (best-effort)
    console.rule("[bold cyan]stage 3h: stock tracker")
    try:
        from digest.stock_tracker import run_stock_tracker
        stk = run_stock_tracker()
        console.print(
            f"  [green]✓[/green] stocks: tickers={stk['tickers']} events={stk['events']}"
        )
        if stk["path"]:
            console.print(f"  [dim]→ {stk['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] stock tracker skipped: {exc}")

    # Stage 3i — quant signal outcome tracking (best-effort, non-blocking)
    console.rule("[bold cyan]stage 3i: outcomes")
    try:
        from digest.outcomes import run_outcomes
        oc = run_outcomes()
        console.print(
            f"  [green]✓[/green] outcomes: confirmed={oc['confirmed']} "
            f"contradicted={oc['contradicted']} pending={oc['pending']}"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]⚠[/yellow] outcomes skipped: {exc}")

    # Stage 4 — write to Obsidian
    if skip_publish:
        console.rule("[bold yellow]stage 4: publish (skipped)")
        return
    console.rule("[bold cyan]stage 4: publish")
    try:
        result = obs_publish()
        console.print(
            f"  [green]✓[/green] daily={result['daily_items']} items, "
            f"topic_archives={result['topic_archives']}"
        )
        console.print(f"  [dim]→ {result['daily_path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] publish failed: {exc}")


@main.command()
@click.option(
    "--date",
    "date_iso",
    default=None,
    help="Date to publish in YYYY-MM-DD (default: today UTC)",
)
@click.option(
    "--topics-only",
    is_flag=True,
    help="Refresh topic archives only, skip daily note",
)
def publish(date_iso: str | None, topics_only: bool) -> None:
    """Write daily note + topic archives to the Obsidian vault."""
    from digest.obsidian import (
        Paths, publish as obs_publish, write_topic_archive,
    )

    db.init_db()
    if topics_only:
        paths = Paths.resolve()
        paths.ensure()
        console.rule("[bold cyan]publish: topics only")
        for slug in db.topics_with_summaries():
            path, n = write_topic_archive(slug, paths)
            console.print(f"  [green]✓[/green] {path.name}: {n} items")
        return

    console.rule("[bold cyan]publish")
    try:
        result = obs_publish(date_iso=date_iso)
        console.print(
            f"  [green]✓[/green] {result['date']}: "
            f"daily={result['daily_items']} items, "
            f"topic_archives={result['topic_archives']}"
        )
        console.print(f"  [dim]→ {result['daily_path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option(
    "--date",
    "date_iso",
    default=None,
    help="Any date in the target week YYYY-MM-DD (default: today UTC)",
)
def weekly(date_iso: str | None) -> None:
    """Generate weekly synthesis note in Obsidian."""
    from digest.obsidian import publish_weekly

    db.init_db()
    console.rule("[bold cyan]weekly digest")
    try:
        result = publish_weekly(date_iso=date_iso)
        console.print(
            f"  [green]✓[/green] week={result['week']} "
            f"items={result['item_count']} themes={result['theme_count']}"
        )
        console.print(f"  [dim]→ {result['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option("--source", default=None, help="Filter by source")
@click.option("--limit", default=20, help="Max rows")
def recent(source: str | None, limit: int) -> None:
    """Show most recently ingested items."""
    db.init_db()
    rows = db.recent_items(source=source, limit=limit)
    if not rows:
        console.print("[yellow]No items.[/yellow]")
        return
    table = Table(title=f"Recent items" + (f" — {source}" if source else ""))
    table.add_column("Source")
    table.add_column("Published", style="dim")
    table.add_column("Title")
    for row in rows:
        table.add_row(
            row["source"],
            (row["published_at"] or "")[:10],
            (row["title"] or "")[:80],
        )
    console.print(table)


@main.command()
def regime() -> None:
    """Classify current macro regime from FRED signals and show result."""
    from digest.macro_regime import REGIME_LABELS, compute_regime

    db.init_db()
    console.rule("[bold cyan]macro regime")
    try:
        result = compute_regime()
        console.print(f"  [bold green]{result.label}[/bold green]  ({result.regime})")
        if result.dimensions:
            t = Table(title="Dimension scores")
            t.add_column("Dimension")
            t.add_column("Score", justify="right")
            for dim, score in sorted(result.dimensions.items()):
                bar = "█" * min(int(abs(score) * 5), 10)
                sign = "+" if score >= 0 else ""
                t.add_row(dim, f"{sign}{score:.2f}  {bar}")
            console.print(t)
        if result.top_signals:
            t2 = Table(title="Top FRED signals")
            t2.add_column("Series")
            t2.add_column("z-score", justify="right")
            for label, z in result.top_signals:
                t2.add_row(label, f"{z:+.2f}")
            console.print(t2)
        console.print(f"\n[dim]{result.narrative}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option("--limit", default=200, show_default=True, help="Max items to score per run")
def ensemble(limit: int) -> None:
    """Run multi-persona Ollama ensemble scoring on summarized items."""
    from digest.ensemble import run_ensemble

    db.init_db()
    console.rule("[bold cyan]ensemble")
    counts = run_ensemble(limit=limit)
    console.print(
        f"  [green]✓[/green] ensemble: processed={counts['processed']} "
        f"succeeded={counts['succeeded']} failed={counts['failed']}"
    )


@main.command()
@click.option("--horizon", default=7, show_default=True, help="Days after ingestion to check")
@click.option("--limit", default=500, show_default=True, help="Max items to check")
def outcomes(horizon: int, limit: int) -> None:
    """Check FRED/CBOE/CFTC z-score signal outcomes (DB-internal, no API calls)."""
    from digest.outcomes import run_outcomes

    db.init_db()
    console.rule("[bold cyan]outcomes")
    counts = run_outcomes(horizon_days=horizon, limit=limit)
    console.print(
        f"  [green]✓[/green] outcomes: checked={counts['checked']} "
        f"confirmed={counts['confirmed']} contradicted={counts['contradicted']} "
        f"neutral={counts['neutral']} pending={counts['pending']}"
    )


@main.command()
def cluster() -> None:
    """Cluster all summarized items into narrative threads (TF-IDF + KMeans)."""
    from digest.cluster import run_clustering

    db.init_db()
    console.rule("[bold cyan]cluster")
    counts = run_clustering()
    console.print(
        f"  [green]✓[/green] cluster: items={counts['items']} clusters={counts['clusters']}"
    )


@main.command()
@click.option("--top-n", default=100, show_default=True, help="Max items per tier")
def signals(top_n: int) -> None:
    """Write High / Medium / Low signal leaderboards to Obsidian."""
    from digest.signals import write_signal_files

    db.init_db()
    console.rule("[bold cyan]signals")
    counts = write_signal_files(top_n=top_n)
    console.print(
        f"  [green]✓[/green] high={counts['high']} medium={counts['medium']} low={counts['low']}"
    )
    console.print(f"  [dim]→ {counts['high'] + counts['medium'] + counts['low']} total items across 3 files[/dim]")


@main.command()
@click.option(
    "--date",
    "date_iso",
    default=None,
    help="Any YYYY-MM-DD in the target week (default: today UTC)",
)
def essay(date_iso: str | None) -> None:
    """Generate a weekly opinionated essay from this week's raw digest signals."""
    from digest.essay import generate_essay
    from datetime import date as _date

    db.init_db()
    console.rule("[bold cyan]essay")
    try:
        ref = _date.fromisoformat(date_iso) if date_iso else None
        result = generate_essay(ref_date=ref)
        console.print(
            f"  [green]✓[/green] week={result['week']} "
            f"words={result['word_count']} sources={result['source_items']}"
        )
        console.print(f"  [dim]→ {result['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
def health() -> None:
    """Check status of all app subsystems (DB, Ollama, MLX, vault, launchd, env)."""
    from digest.health import run_health, overall_status

    db.init_db()
    console.rule("[bold cyan]app health")
    report = run_health()
    overall = overall_status(report)
    _STATUS_COLOR = {"ok": "green", "warn": "yellow", "fail": "red"}
    _STATUS_ICON  = {"ok": "✓", "warn": "⚠", "fail": "✗"}

    for component, result in report.items():
        s     = result["status"]
        color = _STATUS_COLOR[s]
        icon  = _STATUS_ICON[s]
        details = result.get("details", {})
        detail_str = "  ".join(f"{k}={v}" for k, v in details.items() if k != "jobs")
        console.print(f"  [{color}]{icon}[/{color}] [bold]{component}[/bold]  [dim]{detail_str[:100]}[/dim]")
        # Launchd jobs get their own sub-lines
        if "jobs" in details:
            for label, jinfo in details["jobs"].items():
                short = label.replace("com.dr.", "")
                if jinfo.get("loaded"):
                    exit_c = jinfo.get("last_exit", "?")
                    jcolor = "green" if exit_c in ("0", "-") else "red"
                    console.print(f"       [{jcolor}]{short}[/{jcolor}]  pid={jinfo['pid']}  exit={exit_c}")
                else:
                    console.print(f"       [dim]{short}  not loaded[/dim]")

    overall_color = _STATUS_COLOR[overall]
    console.rule(f"[{overall_color}]overall: {overall}[/{overall_color}]")


@main.command()
def security() -> None:
    """Run security audit: file permissions, credential scan, subprocess safety, network."""
    from digest.security import run_security, overall_status

    console.rule("[bold cyan]security audit")
    report = run_security()
    overall = overall_status(report)
    _STATUS_COLOR = {"ok": "green", "warn": "yellow", "fail": "red"}
    _STATUS_ICON  = {"ok": "✓", "warn": "⚠", "fail": "✗"}

    for check, result in report.items():
        s      = result["status"]
        color  = _STATUS_COLOR[s]
        icon   = _STATUS_ICON[s]
        issues = result.get("issues", [])
        details = result.get("details", {})

        # Build a concise summary line
        if check == "file_permissions":
            summary = "  ".join(f"{k}: {v}" for k, v in details.items() if isinstance(v, dict))[:80]
        elif check == "hardcoded_secrets":
            summary = f"{details.get('count', 0)} findings"
        elif check == "subprocess_safety":
            summary = f"{details.get('shell_true_count', 0)} shell=True usages  {details.get('note','')}"
        elif check == "sql_safety":
            summary = details.get("note", "")[:80]
        elif check == "network_exposure":
            listening = details.get("listening", [])
            summary = "  ".join(f"{s['service']}:{s['port']} ({s['address']})" for s in listening) or "none listening"
        elif check == "gitignore":
            missing = details.get("missing", [])
            summary = f"missing: {missing}" if missing else "all patterns covered"
        else:
            summary = ""

        console.print(f"  [{color}]{icon}[/{color}] [bold]{check}[/bold]  [dim]{summary[:100]}[/dim]")
        for issue in issues:
            console.print(f"       [yellow]→[/yellow] {issue}")
        if check == "network_exposure":
            for svc in details.get("exposed_to_lan", []):
                console.print(f"       [yellow]→[/yellow] {svc['service']} exposed on LAN ({svc['address']}) — consider binding to 127.0.0.1")
        if check == "hardcoded_secrets" and details.get("findings"):
            for f in details["findings"][:3]:
                console.print(f"       [red]→[/red] {f['file']}: {f['pattern']}: {f['snippet'][:60]}")

    overall_color = _STATUS_COLOR[overall]
    console.rule(f"[{overall_color}]overall: {overall}[/{overall_color}]")


@main.command()
def dashboard() -> None:
    """Generate interactive HTML signal dashboard (Plotly.js, self-contained)."""
    from digest.dashboard import generate_dashboard

    db.init_db()
    console.rule("[bold cyan]dashboard")
    try:
        result = generate_dashboard()
        console.print(
            f"  [green]✓[/green] events={result['events']} "
            f"fred_series={result['fred_series']} yahoo={result['yahoo_series']}"
        )
        console.print(f"  [dim]→ {result['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option("--limit", default=200, show_default=True, help="Max items to classify")
def sentiment(limit: int) -> None:
    """Classify financial sentiment (bullish/bearish/neutral) on kept items via MLX."""
    from digest.sentiment import run_sentiment

    db.init_db()
    console.rule("[bold cyan]sentiment")
    counts = run_sentiment(limit=limit)
    console.print(
        f"  [green]✓[/green] processed={counts['processed']} "
        f"succeeded={counts['succeeded']} failed={counts['failed']}"
    )


@main.command()
@click.option("--limit", default=500, show_default=True, help="Max items to process")
def entities(limit: int) -> None:
    """Extract financial entities and ticker linkages from kept items."""
    from digest.entities import run_entities

    db.init_db()
    console.rule("[bold cyan]entities")
    counts = run_entities(limit=limit)
    console.print(
        f"  [green]✓[/green] processed={counts['processed']} "
        f"with_entities={counts['with_entities']}"
    )


@main.command()
@click.option("--limit", default=50, show_default=True, help="Max tickers to track")
def stocks(limit: int) -> None:
    """Track top digest-mentioned stocks: price chart + signal overlays → Investments folder."""
    from digest.stock_tracker import run_stock_tracker

    db.init_db()
    console.rule("[bold cyan]stock tracker")
    try:
        result = run_stock_tracker(ticker_limit=limit)
        console.print(
            f"  [green]✓[/green] tickers={result['tickers']} events={result['events']}"
        )
        if result["path"]:
            console.print(f"  [dim]→ {result['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite DB and schema."""
    db.init_db()
    console.print(f"[green]✓[/green] DB initialized at {settings.db_path}")


if __name__ == "__main__":
    sys.exit(main())
