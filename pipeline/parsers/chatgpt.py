"""
ChatGPT export parser.

ChatGPT exports conversations as multiple JSON files named conversations-NNN.json.
Each file contains a list of conversation objects. Each conversation has:
  - id / conversation_id: UUID
  - title: str
  - create_time: float (unix timestamp)
  - update_time: float
  - default_model_slug: str (e.g., "gpt-4", "o1-preview")
  - mapping: dict[str, MessageNode] — tree of messages (not a flat list)

MessageNode:
  - message: Message | None
  - parent: str | None
  - children: list[str]

Message:
  - id: str
  - author: {"role": "user"|"assistant"|"system"|"tool"}
  - content: {"content_type": "text"|..., "parts": list[str|dict]}
  - create_time: float | None
"""
from __future__ import annotations

import glob
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from pipeline.parsers.base import Conversation


def _extract_text_from_parts(parts: list) -> str:
    """Extract plain text from content parts, skipping media/tool objects."""
    texts = []
    for part in parts:
        if isinstance(part, str) and part.strip():
            texts.append(part.strip())
        elif isinstance(part, dict):
            # Tether messages, image_asset_pointer, etc. — skip
            if part.get("content_type") == "text":
                text = part.get("text", "").strip()
                if text:
                    texts.append(text)
    return "\n".join(texts)


def _linearize_mapping(mapping: dict) -> list[dict]:
    """
    Walk the message tree in chronological order (BFS from root),
    returning a flat list of {role, text} dicts with non-empty text.
    """
    # Find root node (no parent or parent is None)
    root_id = None
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            root_id = node_id
            break

    if root_id is None:
        return []

    messages = []
    queue = [root_id]
    visited: set[str] = set()

    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)

        node = mapping.get(node_id, {})
        msg = node.get("message")
        if msg:
            role = msg.get("author", {}).get("role", "")
            if role in ("user", "assistant"):
                content = msg.get("content", {})
                parts = content.get("parts", [])
                text = _extract_text_from_parts(parts)
                if text:
                    messages.append({"role": role, "text": text})

        for child_id in node.get("children", []):
            if child_id not in visited:
                queue.append(child_id)

    return messages


def _parse_conversation(raw: dict) -> Conversation | None:
    """Parse a single conversation dict into a Conversation dataclass."""
    conv_id = raw.get("id") or raw.get("conversation_id", "")
    if not conv_id:
        return None

    title = (raw.get("title") or "Untitled").strip()

    create_ts = raw.get("create_time")
    if create_ts:
        conv_date = datetime.fromtimestamp(create_ts, tz=timezone.utc).date()
    else:
        conv_date = date.today()

    model_slug = raw.get("default_model_slug", "")

    mapping = raw.get("mapping", {})
    messages = _linearize_mapping(mapping)

    if not messages:
        return None

    return Conversation(
        id=conv_id,
        title=title,
        date=conv_date,
        source="chatgpt",
        model=model_slug,
        messages=messages,
    )


def parse_file(path: str | Path) -> Iterator[Conversation]:
    """Yield Conversation objects from a single conversations-NNN.json file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        return

    for raw in data:
        conv = _parse_conversation(raw)
        if conv is not None:
            yield conv


def parse_export(export_dir: str | Path) -> Iterator[Conversation]:
    """
    Yield all Conversation objects from a ChatGPT export directory.
    Processes all conversations-*.json files in sorted order.
    """
    export_dir = Path(export_dir)
    files = sorted(export_dir.glob("conversations-*.json"))

    if not files:
        raise FileNotFoundError(f"No conversations-*.json files found in {export_dir}")

    for json_file in files:
        yield from parse_file(json_file)
