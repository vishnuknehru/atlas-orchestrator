"""Suggested models per adapter, offered as dropdown choices in Settings.

Suggestions only: the registry still accepts any model id, and ATLAS never substitutes one silently.
"""

from __future__ import annotations

import json
from pathlib import Path

_STATIC_MODELS: dict[str, list[str]] = {
    "claude_cli": [
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ],
    "codex_cli": [],
    "manual_handoff": [
        "gemini-4-argon-high",
        "gemini-3.8-flash-high",
        "claude-opus-5-5-high",
        "claude-sonnet-5-5-high",
        "gpt-oss-120b-medium",
    ],
    "openai_compatible_http": [],
}


def _codex_models(home: Path) -> list[str]:
    """Model slugs the installed Codex CLI reports for the signed-in account (its own local cache)."""
    try:
        data = json.loads((home / ".codex" / "models_cache.json").read_text(encoding="utf-8"))
        slugs = [str(m["slug"]) for m in data.get("models", []) if m.get("slug")]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []
    return [s for s in slugs if s != "codex-auto-review"]


def suggested_models(adapter: str, home: Path | None = None) -> list[str]:
    models = list(_STATIC_MODELS.get(adapter, []))
    if adapter == "codex_cli":
        models = _codex_models(home or Path.home()) + models
    return list(dict.fromkeys(models))
