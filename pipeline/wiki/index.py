"""
Index maintenance — regenerates vault/index.md from existing vault pages.

Scans all .md files in the vault, reads frontmatter, groups by type and
community, and writes a structured catalog.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


def _read_frontmatter(text: str) -> dict[str, str]:
    """Parse simple YAML-like frontmatter into a dict of strings."""
    fm: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return fm
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def _extract_summary(text: str) -> str:
    """Extract first non-empty line after '## Summary' heading."""
    in_summary = False
    for line in text.splitlines():
        if line.strip().startswith("## Summary"):
            in_summary = True
            continue
        if in_summary:
            if line.strip().startswith("#"):
                break
            if line.strip() and not line.strip().startswith("*"):
                return line.strip()
    return ""


def regenerate_index(vault_path: Path) -> None:
    """Scan vault, build grouped catalog, write index.md."""
    conversations: list[dict] = []
    entities: list[dict] = []

    # Collect conversations
    for md_file in sorted((vault_path / "conversations").rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        fm = _read_frontmatter(text)
        summary = _extract_summary(text)
        title = md_file.stem.replace("-", " ").title()
        # Try to get title from H1
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        rel = str(md_file.relative_to(vault_path).with_suffix(""))
        conversations.append({
            "title": title,
            "path": rel,
            "date": fm.get("date", ""),
            "community": fm.get("community", ""),
            "triage": fm.get("triage", ""),
            "summary": summary,
        })

    # Collect entities
    for md_file in sorted((vault_path / "entities").rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        fm = _read_frontmatter(text)
        title = md_file.stem.replace("-", " ").title()
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        rel = str(md_file.relative_to(vault_path).with_suffix(""))
        entities.append({
            "title": title,
            "path": rel,
            "category": fm.get("category", ""),
            "mentions": fm.get("mention_count", "1"),
            "community": fm.get("community", ""),
        })

    # Group conversations by community
    comm_groups: dict[str, list[dict]] = defaultdict(list)
    for conv in conversations:
        comm = conv["community"] or "Uncategorized"
        comm_groups[comm].append(conv)

    # Write index.md
    lines = [
        "# Wiki Index",
        "",
        f"*Auto-generated. {len(conversations)} conversations · {len(entities)} entities.*",
        "",
    ]

    # Conversations grouped by community
    lines.append("## Conversations by Community")
    lines.append("")
    for comm in sorted(comm_groups.keys()):
        convs = sorted(comm_groups[comm], key=lambda c: c["date"], reverse=True)
        lines.append(f"### {comm} ({len(convs)})")
        lines.append("")
        for c in convs[:50]:  # cap per community in index
            link = f"[[{c['path']}|{c['title']}]]"
            summary_snippet = f" — {c['summary'][:80]}" if c["summary"] else ""
            lines.append(f"- {link}{summary_snippet}")
        if len(convs) > 50:
            lines.append(f"- *...and {len(convs) - 50} more*")
        lines.append("")

    # Entity catalog
    lines.append("## Entities")
    lines.append("")
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for ent in entities:
        by_cat[ent["category"] or "other"].append(ent)
    for cat in sorted(by_cat.keys()):
        ents = sorted(by_cat[cat], key=lambda e: int(e["mentions"]), reverse=True)
        lines.append(f"### {cat.title()} ({len(ents)})")
        lines.append("")
        for e in ents:
            link = f"[[{e['path']}|{e['title']}]]"
            lines.append(f"- {link} ({e['mentions']} mentions)")
        lines.append("")

    index_path = vault_path / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
