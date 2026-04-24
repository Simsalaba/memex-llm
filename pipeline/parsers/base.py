"""Base protocol for all source parsers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterator, Protocol


@dataclass
class Conversation:
    """Normalized representation of a single conversation from any source."""
    id: str
    title: str
    date: date
    source: str                    # "chatgpt" | "slack" | "web" | "claude"
    model: str                     # model used (if known), else ""
    messages: list[dict]           # [{"role": "user"|"assistant", "text": str}]
    message_count: int = 0
    total_chars: int = 0
    raw_text: str = ""             # full conversation as plain text for LLM prompts

    def __post_init__(self) -> None:
        if self.message_count == 0:
            self.message_count = len(self.messages)
        if self.total_chars == 0:
            self.total_chars = sum(len(m["text"]) for m in self.messages)
        if not self.raw_text:
            self.raw_text = self._build_raw_text()

    def _build_raw_text(self) -> str:
        parts = []
        for m in self.messages:
            role = m["role"].upper()
            text = m["text"].strip()
            if text:
                parts.append(f"{role}: {text}")
        return "\n\n".join(parts)

    def truncated_text(self, max_chars: int) -> str:
        if len(self.raw_text) <= max_chars:
            return self.raw_text
        return self.raw_text[:max_chars] + "\n\n[truncated]"

    def head_text(self, max_chars: int) -> str:
        """First N chars — used for triage."""
        return self.raw_text[:max_chars]


class Parser(Protocol):
    """Protocol all parsers must satisfy."""

    def parse(self, path: str) -> Iterator[Conversation]:
        ...
