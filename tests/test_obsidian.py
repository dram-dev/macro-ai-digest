"""macro obsidian — confirms the lift onto digest_core render/paths/archive."""
from __future__ import annotations

import pytest

from digest import db, obsidian
from digest_core.obsidian.archive import INDEX_BEGIN
from digest_core.obsidian.paths import Paths as CorePaths


def test_paths_is_core_subclass_and_resolves(monkeypatch, tmp_path):
    assert issubclass(obsidian.Paths, CorePaths)
    vault = tmp_path / "vault"
    (vault / "80 Digest").mkdir(parents=True)
    monkeypatch.setattr(db.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(db.settings, "obsidian_digest_dir", "80 Digest")
    p = obsidian.Paths.resolve()
    assert isinstance(p, obsidian.Paths)        # subclass-safe for_vault
    assert p.daily_dir == vault / "80 Digest" / "Daily"


def test_paths_resolve_raises_when_vault_unset(monkeypatch):
    monkeypatch.setattr(db.settings, "obsidian_vault_path", "")
    with pytest.raises(RuntimeError, match="OBSIDIAN_VAULT_PATH is not set"):
        obsidian.Paths.resolve()


def test_wikilink_resolves_macro_label():
    assert obsidian._wikilink("fed_markets") == "[[Fed & Markets]]"


def test_chat_link_uses_macro_framing(make_item):
    # _chat_link reads a row; a dict is row-compatible for the keys it touches.
    row = {"id": 7, "title": "T", "url": "u", "source": "rss",
           "author": None, "published_at": None, "summary": None,
           "why_it_matters": None}
    link = obsidian._chat_link(row)
    assert link.startswith("[#7](https://claude.ai/new?q=")
    # macro framing is URL-encoded in the prompt
    assert "macro%2FAI%20digest" in link


def test_render_topic_archive_uses_core_index_and_markers(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="t1", title="Fed holds rates")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='fed_markets', triage_decision='keep',
               summary='The Fed held rates steady.', why_it_matters='Rate path.',
               confidence='high', triage_score=0.8 WHERE source_id='t1'"""
        )

    text, item_ids = obsidian.render_topic_archive("fed_markets")
    assert len(item_ids) == 1
    assert INDEX_BEGIN in text                       # core-built index block
    assert obsidian.ITEM_BEGIN.format(id=item_ids[0]) in text
    assert "https://claude.ai/new?q=" in text        # core chat link
    assert "Fed holds rates" in text
