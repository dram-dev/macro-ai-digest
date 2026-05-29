"""macro summarize — confirms it rides the shared core backends."""
from __future__ import annotations

from digest import summarize
from digest_core.summarize.backends import BACKENDS


def test_backends_come_from_core():
    assert summarize.BACKENDS is BACKENDS
    assert set(summarize.BACKENDS) == {
        "claude_cli_pro", "haiku_api", "gemini_flash_free", "local_qwen", "mlx_local",
    }


def test_backend_config_uses_macro_max_tokens():
    cfg = summarize._backend_config()
    assert cfg.max_tokens == 600          # macro's historical output cap


def test_extract_json_is_shared_core():
    from digest_core.summarize.runner import extract_json
    assert summarize.extract_json is extract_json
    assert summarize.extract_json('prose {"a": {"b": 1}} tail') == {"a": {"b": 1}}
