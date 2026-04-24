"""
Resumable checkpoint system.

Tracks per-conversation processing state so a run can be killed and restarted
without re-processing already-completed conversations.

State file schema (JSON):
{
  "conversation_id": {
    "status": "trivial" | "triage_done" | "summarized" | "written",
    "triage": "trivial" | "substantive" | "deep",
    "wiki_path": "conversations/2024-01/title.md",  # set after written
    "ts": 1714000000.0
  },
  ...
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

Status = Literal["flagged", "trivial", "triage_done", "summarized", "written"]
# flagged  = programmatic filter caught it (too short/empty) — recoverable
# trivial  = LLM classified as trivial — recoverable
# Both are skipped during ingest. Use reset_flagged() or reset_trivial() to recover.


class Checkpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        self._dirty = 0  # count of unsaved writes
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path) as f:
                self._data = json.load(f)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        tmp.replace(self.path)
        self._dirty = 0

    def _save_if_needed(self, threshold: int = 10) -> None:
        if self._dirty >= threshold:
            self.save()

    def is_done(self, conv_id: str) -> bool:
        return self._data.get(conv_id, {}).get("status") == "written"

    def is_skipped(self, conv_id: str) -> bool:
        """True if conversation is flagged or trivial (skipped but recoverable)."""
        return self._data.get(conv_id, {}).get("status") in ("flagged", "trivial")

    def is_trivial(self, conv_id: str) -> bool:
        return self._data.get(conv_id, {}).get("status") in ("flagged", "trivial")

    def needs_processing(self, conv_id: str) -> bool:
        return conv_id not in self._data or self._data[conv_id]["status"] not in (
            "flagged",
            "trivial",
            "written",
        )

    def mark_flagged(self, conv_id: str, reason: str = "") -> None:
        """Programmatic pre-filter caught this conversation. Recoverable via reset-flagged."""
        self._data[conv_id] = {
            "status": "flagged",
            "triage": "flagged",
            "reason": reason,
            "ts": time.time(),
        }
        self._dirty += 1
        self._save_if_needed()

    def mark_trivial(self, conv_id: str) -> None:
        """LLM classified as trivial. Recoverable via reset-trivial."""
        self._data[conv_id] = {"status": "trivial", "triage": "trivial", "ts": time.time()}
        self._dirty += 1
        self._save_if_needed()

    def mark_triage_done(self, conv_id: str, triage: str) -> None:
        self._data[conv_id] = {"status": "triage_done", "triage": triage, "ts": time.time()}
        self._dirty += 1
        self._save_if_needed()

    def mark_written(self, conv_id: str, wiki_path: str) -> None:
        entry = self._data.get(conv_id, {})
        entry.update({"status": "written", "wiki_path": wiki_path, "ts": time.time()})
        self._data[conv_id] = entry
        self._dirty += 1
        self._save_if_needed()

    # ------------------------------------------------------------------
    # Reset operations (make recoverable)
    # ------------------------------------------------------------------

    def reset_flagged(self) -> int:
        """Remove all flagged entries so they get re-processed. Returns count reset."""
        ids = [k for k, v in self._data.items() if v.get("status") == "flagged"]
        for k in ids:
            del self._data[k]
        if ids:
            self.save()
        return len(ids)

    def reset_trivial(self) -> int:
        """Remove all trivial+flagged entries so they get re-processed. Returns count reset."""
        ids = [k for k, v in self._data.items() if v.get("status") in ("trivial", "flagged")]
        for k in ids:
            del self._data[k]
        if ids:
            self.save()
        return len(ids)

    def reset_written(self, conv_ids: list[str] | None = None) -> int:
        """
        Reset written conversations back to triage_done so pass 2 re-summarizes them.
        If conv_ids is None, resets ALL written conversations.
        Returns count reset.
        """
        if conv_ids is None:
            targets = [k for k, v in self._data.items() if v.get("status") == "written"]
        else:
            targets = [k for k in conv_ids if self._data.get(k, {}).get("status") == "written"]

        for k in targets:
            entry = self._data[k]
            entry["status"] = "triage_done"
            # preserve triage classification, clear wiki_path
            entry.pop("wiki_path", None)
            entry["ts"] = time.time()

        if targets:
            self.save()
        return len(targets)

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self._data.values():
            s = v.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def total(self) -> int:
        return len(self._data)
