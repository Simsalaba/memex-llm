from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Chars per token (conservative estimate — actual varies by language/content)
# Using 3.5 chars/token to be safe across English + code + other languages
_CHARS_PER_TOKEN = 3.5

# Reserve this fraction of num_ctx for prompt template + output tokens
# Prompt overhead is ~200 tokens, output is 2048 tokens max → reserve ~2300 tokens
# At 16384 ctx: 2300/16384 = 14% reserved → 86% usable
_CTX_USABLE_FRACTION = 0.85


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        env_path = os.getenv("LLM_WIKI_CONFIG")
        path = Path(env_path) if env_path else Path(__file__).parent.parent / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Allow env overrides for key settings
    if url := os.getenv("OLLAMA_BASE_URL"):
        cfg["ollama"]["base_url"] = url

    # Expand ~ in all path values
    for key, val in cfg.get("paths", {}).items():
        if val:
            cfg["paths"][key] = os.path.expanduser(str(val))

    # Auto-derive summary_context_chars and mapreduce_threshold from num_ctx
    # so they stay consistent if the user changes num_ctx
    num_ctx: int = cfg["ollama"]["num_ctx"]
    usable_chars = int(num_ctx * _CHARS_PER_TOKEN * _CTX_USABLE_FRACTION)

    cfg["ollama"]["summary_context_chars"] = usable_chars
    cfg["ollama"]["mapreduce_threshold"] = usable_chars

    return cfg


# Module-level singleton — loaded once on first import
_cfg: dict[str, Any] | None = None


def get() -> dict[str, Any]:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def vault_path() -> Path:
    return Path(get()["paths"]["vault"])


def checkpoint_path() -> Path:
    return Path(get()["paths"]["checkpoint"])


def graphify_graph_path() -> Path:
    return Path(get()["paths"]["graphify_graph"])
