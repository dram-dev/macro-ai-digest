"""Shared `claude -p` transport for the synthesis passes.

Connections, weekly synthesis, and storylines all make one headless Claude
CLI call per run and parse a JSON object out of the reply. The transport and
the tolerant JSON extraction live here so the prompts stay domain-side.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from digest.config import settings


def call_claude(system_prompt: str, prompt: str, timeout: int = 120) -> str:
    """Run one headless `claude -p` call; returns the raw result text."""
    full = f"{system_prompt}\n\n{prompt}"
    cmd = ["claude", "-p", "--model", settings.summarizer_model, "--output-format", "json"]
    result = subprocess.run(
        cmd,
        input=full,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exit {result.returncode}: {result.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(result.stdout)
        return envelope.get("result") or envelope.get("response") or result.stdout
    except json.JSONDecodeError:
        return result.stdout


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON object from model output; {} if none can be extracted."""
    raw = (raw or "").strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}
