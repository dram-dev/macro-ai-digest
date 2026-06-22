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
def stub_pipeline(monkeypatch):
    """Patch every stage to a benign success; tests override one at a time."""
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)
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
