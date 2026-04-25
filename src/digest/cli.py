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
    """Show item counts by source."""
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
