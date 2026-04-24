"""
Synthesis wiki writer — writes community topic pages to vault/wiki/.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pipeline.processors.synthesizer import Community

# ---------------------------------------------------------------------------
# Page template
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """\
---
type: synthesis
community: "{community_name}"
conversation_count: {count}
generated: {date}
tags: [synthesis]
---
# {community_name}

{content}

---
*Synthesized from {count} conversations · {date}*
"""


def write_synthesis_page(
    community: Community,
    content: str,
    vault_path: Path,
    date: str,
) -> Path:
    """Write vault/wiki/<slug>.md. Creates wiki/ dir if needed."""
    wiki_dir = vault_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    page_path = wiki_dir / f"{community.slug}.md"
    page_path.write_text(
        PAGE_TEMPLATE.format(
            community_name=community.name,
            count=community.count,
            date=date,
            content=content.strip(),
        ),
        encoding="utf-8",
    )
    return page_path


# ---------------------------------------------------------------------------
# Synthesis checkpoint (separate from conversation checkpoint)
# ---------------------------------------------------------------------------

class SynthesisCheckpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
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

    def is_done(self, slug: str) -> bool:
        return self._data.get(slug, {}).get("status") == "done"

    def mark_done(self, slug: str) -> None:
        self._data[slug] = {"status": "done", "ts": time.time()}
        self.save()

    def mark_error(self, slug: str, error: str) -> None:
        self._data[slug] = {"status": "error", "error": str(error), "ts": time.time()}
        self.save()

    def reset(self, slug: str | None = None) -> int:
        """Reset one slug or all. Returns count reset."""
        if slug:
            targets = [slug] if slug in self._data else []
        else:
            targets = list(self._data.keys())
        for k in targets:
            del self._data[k]
        if targets:
            self.save()
        return len(targets)

    def stats(self) -> dict[str, int]:
        done = sum(1 for v in self._data.values() if v.get("status") == "done")
        errors = sum(1 for v in self._data.values() if v.get("status") == "error")
        return {"done": done, "errors": errors, "total": len(self._data)}
