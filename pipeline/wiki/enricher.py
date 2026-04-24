"""
Vault enricher — re-enriches existing vault pages with a new/updated Graphify graph.

Run after completing an ingest to apply a higher-quality graph built from vault output:

  1. Entity pages: normalize names against new graph, merge fragmented pages
  2. Conversation pages: update entity wikilinks, rebuild Related section, fix community labels

Typical workflow:
  wiki ingest chatgpt          # build vault (no graph, or pre-ingest graph)
  /graphify on vault output    # build high-quality graph from clean summarized data
  wiki enrich --graph new.json # re-enrich vault with the better graph
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yaml

from pipeline.graphify_bridge import GraphData, get_community, get_related, normalize_entity
from pipeline.wiki.writer import ENTITY_HEADER, _slug, _wikilink


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class EnrichStats(NamedTuple):
    entity_pages_before: int
    entity_pages_after: int
    entity_pages_merged: int
    conversation_pages_updated: int
    conversation_pages_skipped: int


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split markdown into (frontmatter_dict, body). Body includes the # heading."""
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content
    fm = yaml.safe_load(content[4:end]) or {}
    body = content[end + 5:]
    return fm, body


def _patch_frontmatter_field(content: str, field: str, value: str) -> str:
    """Update a single quoted frontmatter field in-place, preserving all other content."""
    patched, n = re.subn(
        rf'({re.escape(field)}:\s*")[^"]*(")',
        rf'\g<1>{value}\g<2>',
        content,
    )
    if n:
        return patched
    # Field exists but unquoted — patch it
    patched, n = re.subn(
        rf'({re.escape(field)}:\s*).*',
        rf'\g<1>{value}',
        content,
    )
    return patched if n else content


# ---------------------------------------------------------------------------
# Entity page normalization + merging
# ---------------------------------------------------------------------------

_ENTITY_TITLE = re.compile(r'^# (.+)$', re.MULTILINE)


def _extract_conv_links(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.strip().startswith("- [[")]


def enrich_entity_pages(vault: Path, graph: GraphData) -> dict[str, str]:
    """
    Normalize all entity pages against the new graph, merging pages that resolve
    to the same canonical name.

    Returns old_slug → canonical_slug mapping so conversation pages can be fixed.
    """
    entities_dir = vault / "entities"
    if not entities_dir.exists():
        return {}

    # Pass 1: read every entity page, normalize name, accumulate into canonical buckets
    canonical: dict[str, dict] = {}   # canonical_slug → accumulated data
    slug_map: dict[str, str] = {}     # old_slug → canonical_slug

    for entity_file in sorted(entities_dir.glob("*.md")):
        content = entity_file.read_text(encoding="utf-8")
        title_match = _ENTITY_TITLE.search(content)
        if not title_match:
            continue
        name = title_match.group(1).strip()

        fm, body = _parse_frontmatter(content)
        canon_name = normalize_entity(name, graph)
        canon_slug = _slug(canon_name)
        old_slug = entity_file.stem
        slug_map[old_slug] = canon_slug

        if canon_slug not in canonical:
            canonical[canon_slug] = {
                "name": canon_name,
                "category": fm.get("category", ""),
                "first_seen": str(fm.get("first_seen", "")),
                "community": get_community(canon_name, graph) or str(fm.get("community", "")),
                "mention_count": 0,
                "conv_links": [],
            }

        canonical[canon_slug]["mention_count"] += int(fm.get("mention_count", 1))
        canonical[canon_slug]["conv_links"].extend(_extract_conv_links(body))

    # Pass 2: delete all existing entity pages, rewrite canonical ones cleanly
    for entity_file in entities_dir.glob("*.md"):
        entity_file.unlink()

    for canon_slug, data in canonical.items():
        header = ENTITY_HEADER.format(
            category=data["category"],
            first_seen=data["first_seen"],
            mention_count=data["mention_count"],
            community=data["community"],
            name=data["name"],
        )
        # Deduplicate conversation links (same conv may appear from multiple merged pages)
        seen: set[str] = set()
        unique_links: list[str] = []
        for link in data["conv_links"]:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        (entities_dir / f"{canon_slug}.md").write_text(
            header + "\n".join(unique_links) + "\n",
            encoding="utf-8",
        )

    return slug_map


# ---------------------------------------------------------------------------
# Conversation page update
# ---------------------------------------------------------------------------

_ENTITIES_SECTION = re.compile(r'(## Entities\n)(.*?)(?=\n+## |\Z)', re.DOTALL)
_RELATED_SECTION  = re.compile(r'\n+## Related \(via graph\)\n.*?(?=\n+## |\Z)', re.DOTALL)
_WIKILINK_NAME    = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')
_ENTITY_TYPE      = re.compile(r'\((\w+)\)\s*$')


def enrich_conversation_page(page_path: Path, graph: GraphData) -> bool:
    """
    Update a single conversation page with new graph data:
      - Normalize entity wikilinks to canonical names
      - Rebuild ## Related (via graph) section
      - Update community field in frontmatter

    Returns True if the file was changed.
    """
    content = page_path.read_text(encoding="utf-8")
    original = content

    entities_match = _ENTITIES_SECTION.search(content)
    if not entities_match:
        return False

    # Extract and normalize entities
    normalized: list[tuple[str, str]] = []  # (canonical_name, type)
    for line in entities_match.group(2).strip().splitlines():
        link_match = _WIKILINK_NAME.search(line)
        if not link_match:
            continue
        raw_name = link_match.group(1)
        type_match = _ENTITY_TYPE.search(line)
        entity_type = type_match.group(1) if type_match else ""
        normalized.append((normalize_entity(raw_name, graph), entity_type))

    if not normalized:
        return False

    # Rebuild ## Entities section
    new_entity_links = "\n".join(
        f"- {_wikilink(name)} ({etype})" for name, etype in normalized
    )
    content = _ENTITIES_SECTION.sub(
        lambda m: m.group(1) + new_entity_links,
        content,
    )

    # Rebuild ## Related (via graph) section
    seen_names = {name.lower() for name, _ in normalized}
    related: list[str] = []
    for name, _ in normalized:
        for neighbor in get_related(name, graph, max_relations=5):
            if neighbor.lower() not in seen_names:
                related.append(neighbor)
                seen_names.add(neighbor.lower())

    content = _RELATED_SECTION.sub("", content)
    if related:
        related_links = "\n".join(f"- {_wikilink(r)}" for r in related[:10])
        content = content.rstrip() + f"\n\n## Related (via graph)\n{related_links}"

    # Update community in frontmatter
    for name, _ in normalized:
        community = get_community(name, graph)
        if community:
            content = _patch_frontmatter_field(content, "community", community)
            break

    if content != original:
        page_path.write_text(content, encoding="utf-8")
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def enrich_vault(
    vault: Path,
    graph: GraphData,
    progress_callback: "callable[[int, int], None] | None" = None,
) -> EnrichStats:
    """
    Full enrichment pass. Optional progress_callback(done, total) for CLI progress bars.

    Steps:
      1. Normalize + merge entity pages
      2. Update all conversation pages
    """
    entities_dir = vault / "entities"
    entities_before = len(list(entities_dir.glob("*.md"))) if entities_dir.exists() else 0

    enrich_entity_pages(vault, graph)

    entities_after = len(list(entities_dir.glob("*.md"))) if entities_dir.exists() else 0

    # Update conversation pages
    conv_dir = vault / "conversations"
    all_pages = list(conv_dir.rglob("*.md")) if conv_dir.exists() else []
    total = len(all_pages)
    updated = 0
    skipped = 0

    for i, page in enumerate(all_pages, 1):
        changed = enrich_conversation_page(page, graph)
        if changed:
            updated += 1
        else:
            skipped += 1
        if progress_callback:
            progress_callback(i, total)

    return EnrichStats(
        entity_pages_before=entities_before,
        entity_pages_after=entities_after,
        entity_pages_merged=entities_before - entities_after,
        conversation_pages_updated=updated,
        conversation_pages_skipped=skipped,
    )
