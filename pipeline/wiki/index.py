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

    # Wiki (synthesis) pages
    wiki_dir = vault_path / "wiki"
    if wiki_dir.exists():
        wiki_pages = sorted(wiki_dir.glob("*.md"))
        if wiki_pages:
            lines.append("## Wiki (Synthesis)")
            lines.append("")
            lines.append("*One synthesized topic page per community cluster.*")
            lines.append("")
            for md_file in wiki_pages:
                text = md_file.read_text(encoding="utf-8")
                fm = _read_frontmatter(text)
                title = md_file.stem.replace("-", " ").title()
                for line in text.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                rel = str(md_file.relative_to(vault_path).with_suffix(""))
                count = fm.get("conversation_count", "?")
                lines.append(f"- [[{rel}|{title}]] ({count} conversations)")
            lines.append("")

    # Notes (manual session artifacts)
    notes_dir = vault_path / "notes"
    if notes_dir.exists():
        note_pages = sorted(notes_dir.glob("*.md"), reverse=True)
        if note_pages:
            lines.append("## Notes")
            lines.append("")
            lines.append("*Manual session artifacts: decisions, discoveries, handoffs.*")
            lines.append("")
            for md_file in note_pages:
                text = md_file.read_text(encoding="utf-8")
                fm = _read_frontmatter(text)
                title = md_file.stem
                for line in text.splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                rel = str(md_file.relative_to(vault_path).with_suffix(""))
                note_type = fm.get("type", "note")
                date = fm.get("date", "")
                lines.append(f"- [[{rel}|{title}]] ({note_type}{', ' + date if date else ''})")
            lines.append("")

    index_path = vault_path / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")

    # Keep _CLAUDE.md in sync (only write if missing — user may customize it)
    claude_md = vault_path / "_CLAUDE.md"
    if not claude_md.exists():
        _write_vault_claude_md(vault_path, len(conversations), len(entities))


def _write_vault_claude_md(vault_path: Path, conv_count: int, entity_count: int) -> None:
    """Write _CLAUDE.md at vault root — entry point for LLM navigation."""
    content = f"""\
# Vault Navigation

This is a personal knowledge vault built from AI conversation history.
Start here when searching for information or answering questions.

## Structure

| Folder | Contents | How to use |
|--------|----------|------------|
| `conversations/` | {conv_count} archived conversation summaries, organized by YYYY-MM | Search for specific past interactions |
| `entities/` | {entity_count} named entity pages (tools, people, projects, concepts) | Look up anything named |
| `wiki/` | Synthesized topic pages — one per community cluster | Best starting point for broad topics |
| `notes/` | Manual session artifacts: decisions, discoveries, handoffs | Recent context and architectural decisions |

## Navigation

1. **For a broad topic** (e.g. "what do I know about Kubernetes") → check `wiki/` first
2. **For a specific tool or project** → check `entities/`
3. **For recent decisions or session context** → check `notes/`
4. **For a specific past conversation** → search `conversations/` or use `index.md`
5. **Full catalog** → `index.md` lists everything grouped by community

## Query approach

Read `index.md` to get an overview of communities and recent notes.
Drill into relevant pages. Synthesize across sources to answer the question.
Do not re-summarize what pages already say — extend and connect.
"""
    claude_md.write_text(content, encoding="utf-8")
