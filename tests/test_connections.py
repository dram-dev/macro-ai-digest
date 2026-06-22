"""Validation of LLM-proposed connection threads: schema + referential grounding.

`_validate_threads` must drop anything the model returns that isn't a well-formed,
grounded thread — without raising (connections are best-effort).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from digest.connections import ConnectionThread, _validate_threads

VALID_IDS = {1, 2, 3, 4, 5}
GOOD_INSIGHT = "These two items reinforce the same Fed-policy thread for the reader."


def _thread(**over):
    base = {"theme": "Fed policy tightening", "item_ids": [1, 2], "insight": GOOD_INSIGHT}
    base.update(over)
    return base


def test_valid_thread_passes_and_is_normalized():
    out = _validate_threads([_thread(theme="  Fed policy  ", item_ids=[1, 2, 2, 3])], VALID_IDS)
    assert len(out) == 1
    assert out[0]["theme"] == "Fed policy"          # stripped
    assert out[0]["item_ids"] == [1, 2, 3]          # deduped, order preserved
    assert out[0]["insight"] == GOOD_INSIGHT


def test_fewer_than_two_ids_dropped():
    assert _validate_threads([_thread(item_ids=[1])], VALID_IDS) == []


def test_duplicate_ids_collapsing_below_two_dropped():
    assert _validate_threads([_thread(item_ids=[2, 2, 2])], VALID_IDS) == []


def test_ungrounded_ids_dropped_when_too_few_remain():
    # only id 1 is real → < 2 grounded ids → dropped
    assert _validate_threads([_thread(item_ids=[1, 99, 100])], VALID_IDS) == []


def test_partly_grounded_thread_keeps_only_grounded_ids():
    out = _validate_threads([_thread(item_ids=[1, 2, 99])], VALID_IDS)
    assert len(out) == 1
    assert out[0]["item_ids"] == [1, 2]             # 99 filtered out


def test_missing_or_empty_theme_dropped():
    assert _validate_threads([_thread(theme="   ")], VALID_IDS) == []
    no_theme = {"item_ids": [1, 2], "insight": GOOD_INSIGHT}
    assert _validate_threads([no_theme], VALID_IDS) == []


def test_short_insight_dropped():
    assert _validate_threads([_thread(insight="too short")], VALID_IDS) == []


def test_non_dict_entries_dropped():
    assert _validate_threads(["nope", 42, None, [1, 2]], VALID_IDS) == []


def test_numeric_string_ids_are_coerced():
    out = _validate_threads([_thread(item_ids=["1", "2"])], VALID_IDS)
    assert len(out) == 1
    assert out[0]["item_ids"] == [1, 2]


def test_mixed_batch_returns_only_valid_threads_in_order():
    batch = [
        _thread(theme="keep A", item_ids=[1, 2]),
        _thread(item_ids=[1]),                       # too few ids
        _thread(theme="keep B", item_ids=[3, 4, 5]),
        "garbage",
    ]
    out = _validate_threads(batch, VALID_IDS)
    assert [t["theme"] for t in out] == ["keep A", "keep B"]


def test_model_rejects_short_insight_directly():
    with pytest.raises(ValidationError):
        ConnectionThread(theme="x", item_ids=[1, 2], insight="short")
