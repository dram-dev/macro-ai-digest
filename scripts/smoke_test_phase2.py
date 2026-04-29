"""Phase 2 smoke test — confirms Ollama + Claude Code CLI work for the pipeline.

Run after installing Ollama, pulling qwen2.5:14b, and installing Claude Code.

Usage:
    uv run python scripts/smoke_test_phase2.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from digest.config import settings  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"


def line(icon: str, label: str, detail: str = "") -> None:
    print(f"  {icon} {label:24} {detail}")


def test_ollama_running() -> bool:
    try:
        import requests

        r = requests.get(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name") for m in r.json().get("models", [])]
        if not models:
            line(FAIL, "ollama running", "0 models loaded — run `ollama pull qwen2.5:14b`")
            return False
        line(PASS, "ollama running", f"{len(models)} model(s): {', '.join(models[:3])}")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "ollama running", f"{type(exc).__name__}: {exc}")
        return False


def test_ollama_model_exists() -> bool:
    try:
        import requests

        r = requests.get(f"{settings.ollama_host.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name") for m in r.json().get("models", [])]
        target = settings.ollama_model
        if target in models:
            line(PASS, "qwen model present", target)
            return True
        # Tolerate "qwen2.5:14b" vs "qwen2.5:14b-instruct-q4_K_M" partial matches
        partials = [m for m in models if m.startswith(target.split(":")[0])]
        if partials:
            line(WARN, "qwen model present", f"exact tag '{target}' missing; found: {partials}")
            return False
        line(FAIL, "qwen model present", f"'{target}' not found")
        return False
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "qwen model present", f"{type(exc).__name__}: {exc}")
        return False


def test_qwen_responds() -> bool:
    try:
        import requests

        r = requests.post(
            f"{settings.ollama_host.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": "Respond with the single word OK and nothing else.",
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 10},
            },
            timeout=60,
        )
        r.raise_for_status()
        out = r.json().get("response", "").strip()
        if "OK" in out.upper():
            line(PASS, "qwen responds", f"reply: {out[:30]!r}")
            return True
        line(WARN, "qwen responds", f"reply didn't contain OK: {out[:50]!r}")
        return False
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "qwen responds", f"{type(exc).__name__}: {exc}")
        return False


def test_qwen_json_output() -> bool:
    """Validate Qwen produces parseable JSON in format=json mode (used by triage)."""
    try:
        import requests

        r = requests.post(
            f"{settings.ollama_host.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": (
                    'Respond with this exact JSON: {"decision":"keep","score":0.7,'
                    '"topic":"fed_markets","reason":"test"}'
                ),
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0, "num_predict": 100},
            },
            timeout=60,
        )
        r.raise_for_status()
        out = r.json().get("response", "")
        parsed = json.loads(out)
        if "decision" in parsed:
            line(PASS, "qwen JSON mode", f"parsed keys: {list(parsed.keys())[:4]}")
            return True
        line(WARN, "qwen JSON mode", f"parsed but missing 'decision' key: {parsed}")
        return False
    except json.JSONDecodeError as exc:
        line(FAIL, "qwen JSON mode", f"unparseable: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "qwen JSON mode", f"{type(exc).__name__}: {exc}")
        return False


def test_claude_cli_present() -> bool:
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if r.returncode == 0:
            line(PASS, "claude CLI present", r.stdout.strip()[:60])
            return True
        line(FAIL, "claude CLI present", f"exit {r.returncode}: {r.stderr[:100]}")
        return False
    except FileNotFoundError:
        line(FAIL, "claude CLI present", "not on PATH — install Claude Code")
        return False
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "claude CLI present", f"{type(exc).__name__}: {exc}")
        return False


def test_claude_cli_headless() -> bool:
    """The exact mode the summarizer uses. Catches auth / model / output-format issues."""
    try:
        r = subprocess.run(
            [
                "claude", "-p",
                "--model", settings.summarizer_model,
                "--output-format", "json",
            ],
            input="Respond with the single word OK and nothing else.",
            capture_output=True, text=True, timeout=60, check=False,
        )
        if r.returncode != 0:
            line(FAIL, "claude headless mode", f"exit {r.returncode}: {r.stderr[:200]}")
            return False
        try:
            envelope = json.loads(r.stdout)
            text = envelope.get("result") or envelope.get("response") or r.stdout
        except json.JSONDecodeError:
            text = r.stdout
        if "OK" in text.upper():
            line(PASS, "claude headless mode", f"model={settings.summarizer_model} reply ok")
            return True
        line(WARN, "claude headless mode", f"reply didn't contain OK: {text[:80]!r}")
        return False
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "claude headless mode", f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print("\n  Phase 2 smoke tests — triage & summarizer prerequisites\n")
    print(f"  OLLAMA_HOST       = {settings.ollama_host}")
    print(f"  OLLAMA_MODEL      = {settings.ollama_model}")
    print(f"  SUMMARIZER_BACKEND = {settings.summarizer_backend}")
    print(f"  SUMMARIZER_MODEL   = {settings.summarizer_model}\n")

    results = [
        test_ollama_running(),
        test_ollama_model_exists(),
        test_qwen_responds(),
        test_qwen_json_output(),
    ]
    if settings.summarizer_backend == "claude_cli_pro":
        results.append(test_claude_cli_present())
        results.append(test_claude_cli_headless())

    passed = sum(1 for v in results if v)
    total = len(results)
    print(f"\n  {passed}/{total} passed\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
