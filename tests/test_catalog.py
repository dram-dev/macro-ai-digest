"""macro source catalog — the `digest sources` feature works in this domain too."""
from __future__ import annotations

from digest import db
from digest_core import catalog


def test_collect_enriches_macro_sources(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="r1")])
    db.log_run(
        run_type="manual", source="rss", items_fetched=1, items_new=1,
        duration_ms=5, status="ok",
    )
    summaries, failures = catalog.collect(db.get_conn, "digest.ingest")
    by_name = {s.name: s for s in summaries}

    assert by_name["rss"].total_items == 1
    assert by_name["rss"].status == "active"
    assert by_name["yahoo"].status == "never-run"
    assert isinstance(failures, dict)


def test_print_sources_smoke(fresh_db, capsys):
    catalog.print_sources(db.get_conn, "digest.ingest")
    out = capsys.readouterr().out
    assert "Source catalog" in out
    assert "rss" in out
