"""
Wiki writer — creates and updates Obsidian markdown files.

Handles:
  - Conversation pages (one per conversation)
  - Entity pages (one per named entity, aggregated over time)
  - Slugification and path management
  - [[wikilinks]] generation with graphify edge enrichment
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from slugify import slugify

from pipeline.graphify_bridge import GraphData, get_community, get_related, normalize_entity
from pipeline.parsers.base import Conversation
from pipeline.processors.summarizer import Summary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """URL/filesystem-safe slug."""
    return slugify(text, max_length=80, separator="-")


def _wikilink(label: str, path: str | None = None) -> str:
    """Format an Obsidian wikilink."""
    if path and path != label:
        return f"[[{path}|{label}]]"
    return f"[[{label}]]"


# ---------------------------------------------------------------------------
# Conversation pages
# ---------------------------------------------------------------------------

CONV_TEMPLATE = """\
---
source: {source}
conversation_id: "{conv_id}"
date: {date}
model: {model}
triage: {triage}
summarize_strategy: {strategy}
community: "{community}"
tags: [{tags}]
---
# {title}

## Summary
{summary}

## Key Points
{key_points}

## Entities
{entities}
{related_section}"""


def write_conversation_page(
    conv: Conversation,
    summary: Summary,
    triage: str,
    vault_path: Path,
    graph: GraphData,
) -> Path:
    """
    Write a conversation page. Returns the path of the created file.
    """
    # Determine output path: conversations/YYYY-MM/slug.md
    month_dir = vault_path / "conversations" / conv.date.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    filename = _slug(conv.title) or _slug(conv.id[:16])
    page_path = month_dir / f"{filename}.md"

    # Normalize entities against graphify
    normalized_entities = []
    for ent in summary.entities:
        canonical = normalize_entity(ent["name"], graph)
        normalized_entities.append({"name": canonical, "type": ent["type"]})

    # Community from first entity, or from graphify for the conversation title
    community = ""
    for ent in normalized_entities:
        c = get_community(ent["name"], graph)
        if c:
            community = c
            break

    # Build entity wikilinks
    entity_links = "\n".join(
        f"- {_wikilink(e['name'])} ({e['type']})" for e in normalized_entities
    ) or "- (none extracted)"

    # Graphify-enriched related links (neighbors not already in entity list)
    entity_names = {e["name"].lower() for e in normalized_entities}
    related: list[str] = []
    for ent in normalized_entities:
        for neighbor in get_related(ent["name"], graph, max_relations=5):
            if neighbor.lower() not in entity_names:
                related.append(neighbor)
                entity_names.add(neighbor.lower())

    related_section = ""
    if related:
        related_links = "\n".join(f"- {_wikilink(r)}" for r in related[:10])
        related_section = f"\n## Related (via graph)\n{related_links}"

    # Tags: combine summary tags + community tag
    all_tags = list(summary.tags)
    if community:
        comm_tag = "community/" + _slug(community)
        if comm_tag not in all_tags:
            all_tags.insert(0, comm_tag)

    key_points = "\n".join(f"- {p}" for p in summary.key_points) or "- (see summary)"

    content = CONV_TEMPLATE.format(
        source=conv.source,
        conv_id=conv.id,
        date=str(conv.date),
        model=conv.model or "unknown",
        triage=triage,
        strategy=summary.strategy,
        community=community,
        tags=", ".join(all_tags),
        title=conv.title,
        summary=summary.summary,
        key_points=key_points,
        entities=entity_links,
        related_section=related_section,
    )

    page_path.write_text(content, encoding="utf-8")
    return page_path


# ---------------------------------------------------------------------------
# Entity pages
# ---------------------------------------------------------------------------

ENTITY_HEADER = """\
---
type: entity
category: {category}
first_seen: {first_seen}
mention_count: {mention_count}
community: "{community}"
graphify_node: ""
---
# {name}

## Summary
*Aggregated from conversations*

## Conversations
"""


def update_entity_page(
    entity_name: str,
    entity_type: str,
    conv: Conversation,
    conv_wiki_path: str,
    vault_path: Path,
    graph: GraphData,
) -> Path:
    """
    Create or append to an entity page.
    Returns the path of the entity file.
    """
    entities_dir = vault_path / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    filename = _slug(entity_name) + ".md"
    entity_path = entities_dir / filename

    community = get_community(entity_name, graph)

    if entity_path.exists():
        # Append conversation reference
        existing = entity_path.read_text(encoding="utf-8")

        # Update mention_count in frontmatter
        existing = re.sub(
            r"(mention_count:\s*)(\d+)",
            lambda m: f"{m.group(1)}{int(m.group(2)) + 1}",
            existing,
        )

        # Append conversation link
        conv_link = f"- {_wikilink(conv.title, conv_wiki_path)} — {conv.date} ({conv.source})\n"
        existing += conv_link
        entity_path.write_text(existing, encoding="utf-8")
    else:
        # Create new entity page
        header = ENTITY_HEADER.format(
            category=entity_type,
            first_seen=str(conv.date),
            mention_count=1,
            community=community,
            name=entity_name,
        )
        conv_link = f"- {_wikilink(conv.title, conv_wiki_path)} — {conv.date} ({conv.source})\n"
        entity_path.write_text(header + conv_link, encoding="utf-8")

    return entity_path
