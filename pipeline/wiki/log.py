"""Append-only log.md maintenance."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path


def append_log(vault_path: Path, message: str) -> None:
    log_file = vault_path / "log.md"
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    entry = f"{ts} | {message}\n"

    if not log_file.exists():
        log_file.write_text("# Wiki Log\n\nAppend-only record of ingest and maintenance operations.\n\n", encoding="utf-8")

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)
