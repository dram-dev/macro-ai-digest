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
    "edgar": "digest.ingest.edgar:EdgarIngestor",
    "fred": "digest.ingest.fred:FREDIngestor",
    "hn": "digest.ingest.hackernews:HNIngestor",
    "clipped": "digest.ingest.clipped:ClippedIngestor",
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


@main.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite DB and schema."""
    db.init_db()
    console.print(f"[green]✓[/green] DB initialized at {settings.db_path}")


if __name__ == "__main__":
    sys.exit(main())
