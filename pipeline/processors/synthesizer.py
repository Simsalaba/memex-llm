"""
Synthesizer — community-level knowledge synthesis (Pass 3).

For each named Graphify community in index.md:
  1. Parse conversation paths from the community section
  2. Load Summary + Key Points from each conversation page
  3. If total text fits in one call → direct synthesis
  4. If too large → map-reduce (synthesize batches, merge)

Output: markdown prose topic page (not JSON).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from slugify import slugify

from pipeline.ollama_client import OllamaClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CHARS_PER_CALL = 35_000  # ~70% of 16k ctx window in chars (~3.5 chars/token)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYNTHESIZE_SYSTEM = """\
You are a personal knowledge curator. Synthesize conversation summaries into a
concise topic page for a personal wiki. Write for future-you who needs to quickly
recall everything you know about this topic. Be factual, concrete, skip filler."""

SYNTHESIZE_PROMPT = """\
Topic: {community_name}

Below are summaries from {count} of your past conversations about this topic.
Write a synthesis wiki page with these sections:

## Overview
What this topic covers in your life/work. 2-4 sentences.

## Key Patterns
Recurring themes, decisions, or approaches across conversations. Bullets.

## Tools & Concepts
Important tools, technologies, or ideas encountered. Bullets.

## Lessons & Gotchas
Things learned the hard way, non-obvious findings, things that bit you. Bullets.

## Open Questions
Unresolved things or areas to explore. Omit this section if nothing applies.

Rules:
- Write markdown, no extra headers beyond the 5 above
- Each section: 3-8 bullet points (except Overview)
- Do NOT summarize individual conversations — synthesize patterns across all
- Do NOT include a title line
- For any named tool, project, service, or person you mention, wrap it in [[double brackets]]: [[Kubernetes]], [[Jenkins]], [[Grafana]], [[Python]]. This creates Obsidian graph links.

--- Conversations ---
{summaries}
--- End ---

Write the synthesis page:"""

MERGE_PROMPT = """\
Topic: {community_name}

These are partial synthesis pages for different batches of conversations about this topic.
Merge into one cohesive page with the same 5-section structure.
Deduplicate. Keep the most important points. No new sections.
Preserve all [[wikilinks]] from the partial pages — do not strip the double brackets.

--- Partial syntheses ---
{partials}
--- End ---

Write the merged synthesis page:"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Community:
    name: str
    slug: str
    conv_paths: list[str]  # e.g. ["conversations/2024-03/my-title"]
    count: int


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------

def parse_communities(index_path: Path) -> list[Community]:
    """
    Parse index.md and return named communities that have conversation pages.
    Skips: Uncategorized, hyperedge:*, entity-type sections (no conversation links).
    """
    text = index_path.read_text(encoding="utf-8")
    sections = re.split(r"^### ", text, flags=re.MULTILINE)

    communities = []
    for section in sections:
        if not section.strip():
            continue

        header = section.split("\n", 1)[0].strip()

        # Must match "Name (N)" — count is always the last parenthetical
        m = re.match(r"^(.+)\s+\((\d+)\)$", header)
        if not m:
            continue

        name = m.group(1).strip()
        count = int(m.group(2))

        if name == "Uncategorized" or name.startswith("hyperedge:"):
            continue

        # Extract conversation wikilinks only (skip entity links)
        # Capture full path including "conversations/" prefix
        conv_paths = re.findall(
            r"\[\[(conversations/[^\]|]+?)(?:\|[^\]]+)?\]\]", section
        )
        if not conv_paths:
            continue  # entity-type section — no conversation links

        communities.append(Community(
            name=name,
            slug=slugify(name, max_length=80, separator="-"),
            conv_paths=conv_paths,
            count=count,
        ))

    return communities


# ---------------------------------------------------------------------------
# Summary extraction from conversation pages
# ---------------------------------------------------------------------------

def _extract_summary(md_text: str, title: str) -> str:
    """Pull title + Summary + Key Points sections from a conversation page."""
    parts = [f"### {title}"]

    m = re.search(r"## Summary\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if m:
        parts.append(m.group(1).strip())

    m = re.search(r"## Key Points\n(.*?)(?=\n## |\Z)", md_text, re.DOTALL)
    if m:
        parts.append(m.group(1).strip())

    return "\n".join(parts)


def load_community_summaries(community: Community, vault_path: Path) -> list[str]:
    """Load extracted summary text for each conversation in the community."""
    items = []
    for conv_path in community.conv_paths:
        page = vault_path / f"{conv_path}.md"
        if not page.exists():
            continue
        text = page.read_text(encoding="utf-8")
        title_m = re.search(r"^# (.+)$", text, re.MULTILINE)
        title = title_m.group(1) if title_m else conv_path.split("/")[-1]
        items.append(_extract_summary(text, title))
    return items


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def _batch_items(items: list[str], max_chars: int) -> list[list[str]]:
    """Pack items into batches that each stay under max_chars."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for item in items:
        item_chars = len(item)
        if current and current_chars + item_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars

    if current:
        batches.append(current)

    return batches or [[]]


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

def _synthesize_batch(
    community_name: str,
    total_count: int,
    items: list[str],
    client: OllamaClient,
    model: str,
    num_ctx: int,
) -> str:
    prompt = SYNTHESIZE_PROMPT.format(
        community_name=community_name,
        count=total_count,
        summaries="\n\n".join(items),
    )
    return client.generate(model, prompt, system=SYNTHESIZE_SYSTEM, num_ctx=num_ctx)


def _merge_syntheses(
    community_name: str,
    partials: list[str],
    client: OllamaClient,
    model: str,
    num_ctx: int,
) -> str:
    prompt = MERGE_PROMPT.format(
        community_name=community_name,
        partials="\n\n---\n\n".join(partials),
    )
    return client.generate(model, prompt, system=SYNTHESIZE_SYSTEM, num_ctx=num_ctx)


def _fix_wikilinks(text: str, vault_path: Path) -> str:
    """
    Post-process synthesis output: convert bare [[Name]] wikilinks to
    [[entities/slug|Name]] format so Obsidian resolves multi-word names correctly.
    Only converts links where a matching entity file exists.
    """
    entities_dir = vault_path / "entities"
    if not entities_dir.exists():
        return text

    known_slugs = {f.stem for f in entities_dir.glob("*.md")}

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        # Already has path or alias — leave alone
        if "/" in inner or "|" in inner:
            return m.group(0)
        candidate = slugify(inner, max_length=80, separator="-")
        if candidate in known_slugs:
            return f"[[entities/{candidate}|{inner}]]"
        return m.group(0)

    return re.sub(r"\[\[([^\]]+)\]\]", _replace, text)


def synthesize_community(
    community: Community,
    items: list[str],
    client: OllamaClient,
    model: str,
    num_ctx: int = 16384,
    vault_path: Path | None = None,
) -> str:
    """
    Synthesize a community into a topic page.
    Uses map-reduce if items exceed MAX_CHARS_PER_CALL.
    Returns raw markdown content (no frontmatter, no title).
    If vault_path is provided, fixes [[wikilinks]] to use entity path format.
    """
    batches = _batch_items(items, MAX_CHARS_PER_CALL)

    if len(batches) == 1:
        result = _synthesize_batch(community.name, len(items), batches[0], client, model, num_ctx)
    else:
        partials = [
            _synthesize_batch(community.name, len(items), batch, client, model, num_ctx)
            for batch in batches
        ]
        result = _merge_syntheses(community.name, partials, client, model, num_ctx)

    if vault_path:
        result = _fix_wikilinks(result, vault_path)

    return result
