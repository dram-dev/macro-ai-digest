"""Run-quality gate on macro's `digest pipeline`.

Required stages (ingest → triage → summarize → publish) must make the run exit
non-zero on failure so launchd/cron can't mistake a broken run for a good one;
the 3c–3k enrichment passes stay best-effort. The pipeline lazy-imports nine
enrichment modules (some pull MLX/torch), so they're faked via sys.modules to
keep this test light.
"""
from __future__ import annotations

import sys
import types

import pytest
from click.testing import CliRunner

from digest import cli
from digest_core import runlock


def _fake_module(name: str, **funcs) -> types.ModuleType:
    m = types.ModuleType(name)
    for key, fn in funcs.items():
        setattr(m, key, fn)
    return m


def _boom(msg: str):
    def _fn(*args, **kwargs):
        raise RuntimeError(msg)
    return _fn


@pytest.fixture
def stub_pipeline(monkeypatch, tmp_path):
    """Patch every stage to a benign success; tests override one at a time."""
    # Private lock file: a real digest run holding /tmp's lock must not stall tests.
    monkeypatch.setenv("PIPELINE_LOCK_PATH", str(tmp_path / "pipeline.lock"))
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(cli.db, "triage_lookback_hours", lambda: 24)
    monkeypatch.setattr(cli, "run_ingest", lambda *a, **k: (0, 0))

    fakes = {
        "digest.triage": {"run_triage": lambda *a, **k: {"kept": 1, "dropped": 0, "errors": 0}},
        "digest.summarize": {"run_summarize": lambda *a, **k: {
            "succeeded": 1, "failed": 0, "ready": 1}},
        "digest.obsidian": {"publish": lambda *a, **k: {
            "daily_items": 1, "topic_archives": 1, "daily_path": "x"}},
        "digest.connections": {"run_connections": lambda *a, **k: []},
        "digest.storylines": {"run_storylines": lambda *a, **k: {
            "moved": 0, "new": 0, "resolved": 0, "dormant": 0}},
        "digest.ensemble": {"run_ensemble": lambda *a, **k: {"succeeded": 0, "failed": 0}},
        "digest.sentiment": {"run_sentiment": lambda *a, **k: {
            "processed": 0, "succeeded": 0, "failed": 0}},
        "digest.entities": {"run_entities": lambda *a, **k: {"processed": 0, "with_entities": 0}},
        "digest.cluster": {"run_clustering": lambda *a, **k: {"items": 0, "clusters": 0}},
        "digest.stock_tracker": {"run_stock_tracker": lambda *a, **k: {
            "path": "", "tickers": 0, "events": 0}},
        "digest.outcomes": {"run_outcomes": lambda *a, **k: {
            "confirmed": 0, "contradicted": 0, "pending": 0}},
        "digest.predictions": {"resolve_due_predictions": lambda *a, **k: {
            "due": 0, "correct": 0, "incorrect": 0, "unclear": 0, "deferred": 0}},
    }
    for name, funcs in fakes.items():
        monkeypatch.setitem(sys.modules, name, _fake_module(name, **funcs))
    return monkeypatch


def test_pipeline_all_ok_exits_zero(stub_pipeline):
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "manual"])
    assert res.exit_code == 0, res.output
    assert "run quality" in res.output
    assert "all stages ok" in res.output


def test_pipeline_publish_failure_exits_nonzero(stub_pipeline):
    stub_pipeline.setitem(sys.modules, "digest.obsidian",
                          _fake_module("digest.obsidian", publish=_boom("vault locked")))
    res = CliRunner().invoke(cli.main, ["pipeline"])
    assert res.exit_code == 1, res.output
    assert "publish failed" in res.output
    assert "publish (required): vault locked" in res.output


def test_pipeline_required_upstream_failure_skips_publish(stub_pipeline):
    stub_pipeline.setitem(sys.modules, "digest.summarize",
                          _fake_module("digest.summarize", run_summarize=_boom("mlx down")))
    res = CliRunner().invoke(cli.main, ["pipeline"])
    assert res.exit_code == 1, res.output
    assert "required stage failed: mlx down" in res.output
    assert "upstream failure" in res.output           # publish skipped, not attempted


def test_pipeline_optional_enrichment_failure_still_exits_zero(stub_pipeline):
    stub_pipeline.setitem(sys.modules, "digest.connections",
                          _fake_module("digest.connections", run_connections=_boom("no items")))
    res = CliRunner().invoke(cli.main, ["pipeline"])
    assert res.exit_code == 0, res.output
    assert "connections (optional): no items" in res.output
    assert "stage 4: publish" in res.output           # enrichment miss didn't block publish


def test_pipeline_skip_publish_exits_zero(stub_pipeline):
    res = CliRunner().invoke(cli.main, ["pipeline", "--skip-publish"])
    assert res.exit_code == 0, res.output
    assert "all stages ok" in res.output


def test_optional_enrichment_failure_with_markup_stays_non_fatal(stub_pipeline):
    # An exception message carrying Rich-markup tokens must not be re-parsed as
    # markup: "[/]" would raise MarkupError (crashing a best-effort stage) and
    # "[active]" would be silently swallowed. Escaping keeps the enrichment miss
    # non-fatal and preserves the message verbatim.
    stub_pipeline.setitem(sys.modules, "digest.connections",
                          _fake_module("digest.connections",
                                       run_connections=_boom("broke [/] on [active]")))
    res = CliRunner().invoke(cli.main, ["pipeline"])
    assert res.exit_code == 0, res.output            # enrichment miss stays non-fatal
    assert "connections (optional)" in res.output
    assert "[active]" in res.output                  # bracket content preserved, not eaten


def test_triage_window_is_resolved_before_ingest_logs_this_run(stub_pipeline):
    # Resolved after ingest, the window would anchor on THIS run's own run_log
    # rows and shrink to the floor — stranding the previous run's leftovers.
    calls: list = []
    stub_pipeline.setattr(cli.db, "triage_lookback_hours", lambda: calls.append("window") or 30)
    stub_pipeline.setattr(cli, "run_ingest", lambda *a, **k: calls.append("ingest") or (0, 0))
    stub_pipeline.setitem(sys.modules, "digest.triage", _fake_module(
        "digest.triage",
        run_triage=lambda *a, **k: calls.append(k) or {"kept": 1, "dropped": 0, "errors": 0}))
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "daily"])
    assert res.exit_code == 0, res.output
    assert calls == ["window", "ingest", {"lookback_hours": 30}]


def test_pipeline_waits_its_turn_and_fails_if_the_other_run_is_wedged(stub_pipeline):
    stub_pipeline.setenv("PIPELINE_LOCK_TIMEOUT_SEC", "0.1")
    stub_pipeline.setattr(runlock, "_POLL_SEC", 0.02)
    with runlock.pipeline_serialize("pc-insurance-digest"):
        res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "daily"])
    assert res.exit_code == 1, res.output
    assert "waiting for the other digest run" in res.output
    assert "pc-insurance-digest" in res.output
    assert "stage 1: ingest" not in res.output  # never ran alongside it
