# FAQ

## Pipeline & Commands

**Do I need to run `wiki reindex` after `wiki enrich`?**
No. `wiki enrich` calls `regenerate_index` automatically at the end. Only run `wiki reindex` manually if you edit vault pages directly or want to force a refresh.

**Do I need to run `wiki reindex` before `wiki synthesize`?**
Yes, if it's your first run or if you've added conversations since the last reindex. `wiki synthesize` reads `index.md` to find communities — stale index = stale synthesis targets.

**Will `wiki synthesize` process uncategorized conversations?**
No. The `Uncategorized` bucket and `hyperedge:*` sections are explicitly skipped. Only named Graphify community clusters get synthesized. See [Uncategorized](#uncategorized-conversations) below.

**What happens if I run `wiki synthesize` twice?**
Nothing — already-done communities are skipped via `data/synthesis_checkpoint.json`. It's safe to re-run.

**Will `wiki synthesize` update topic pages when new conversations are ingested?**
Not automatically. Once a community is marked `done` in the synthesis checkpoint, it's skipped on future runs. To re-synthesize after new ingests:
```bash
wiki synthesize --reset <community-slug>   # reset one
wiki synthesize --reset all                # reset all
wiki synthesize                            # re-run
```
Automatic delta detection (only re-synthesize communities with new conversations) is planned but not yet implemented.

**What is `_CLAUDE.md` and should I edit it?**
`_CLAUDE.md` at the vault root is an LLM navigation guide — it tells an AI assistant what's in each folder and how to search. It's auto-generated on first `wiki reindex` and never overwritten after that, so you can freely customize it. Delete it and rerun `wiki reindex` to regenerate from scratch.

---

## Vault Structure

**What are the four vault layers?**

| Folder | What | Written by |
|--------|------|------------|
| `conversations/` | One page per conversation — lossless archive | `wiki ingest` |
| `entities/` | One page per named entity, aggregated across conversations | `wiki ingest` |
| `wiki/` | One synthesized topic page per community cluster | `wiki synthesize` |
| `notes/` | Manual session artifacts — decisions, discoveries, handoffs | `/vault-save` command |

**Why keep `conversations/` if `wiki/` has the synthesized knowledge?**
Two reasons. First, synthesis is lossy — the LLM distills patterns but drops specifics. If you need to know what was actually said in a particular conversation, `conversations/` has it. Second, synthesis pages can be regenerated from the conversation archive at any time. The archive is the source of truth.

**Should `wiki/` pages be edited manually?**
You can, but they'll be overwritten if you reset and re-synthesize that community. Treat them as generated artifacts. If you want to add permanent notes about a topic, use `notes/` instead.

---

## Graphify & Communities

**What is Graphify and why does it matter?**
Graphify builds a knowledge graph from your vault content, runs community detection, and assigns each conversation to a named cluster (e.g. "DevOps / CI-CD Toolchain"). These clusters are what `wiki synthesize` uses to group conversations for synthesis. Without Graphify, all conversations land in `Uncategorized` and synthesis has no clusters to work with.

**Uncategorized conversations**
`Uncategorized (N)` in `index.md` means those conversations weren't assigned a Graphify community — usually because Graphify hasn't finished processing all chunks yet (it runs in batches and can hit agent limits).

Options:
- **Wait** — once Graphify finishes all chunks, run `wiki enrich --graph graph.json` to assign communities, then re-synthesize affected topics.
- **LLM-assign** — not yet implemented. Would ask the 14B model to assign each uncategorized conversation to one of the existing communities.

**Do I need to re-run Graphify after every ingest?**
Only if you want new conversations to get community assignments and wikilinks. The pipeline works fine without Graphify — entity pages just won't have cross-references, and new conversations land in `Uncategorized`.

---

## Models & Performance

**Which model does each pass use?**

| Pass | Model | Purpose |
|------|-------|---------|
| 1 — Triage | `qwen2.5:3b` (3B) | Fast classification: trivial / substantive / deep |
| 2 — Summarize | `qwen3:14b` (14B) | Structured extraction: summary, key points, entities, tags |
| 3 — Synthesize | `qwen3:14b` (14B) | Community-level knowledge synthesis |

**How long does synthesis take?**
~10-15s per community on a 14B model with 16GB VRAM. For 72 communities that's ~15-20 minutes total. Large communities (280 conversations) trigger map-reduce and take longer — roughly 3-5 minutes each.

**What happens with very large communities during synthesis?**
Map-reduce kicks in automatically when a community's summaries exceed ~35,000 characters. The summaries are split into batches, each batch is synthesized separately, then the partial results are merged into one final page. No action needed from you.

---

## `/vault-save` Command

**What is `/vault-save`?**
A Claude Code slash command that scans the current session for valuable artifacts (decisions, discoveries, handoff state) and writes them to `vault/notes/` after your confirmation. Run it at the end of any session where you've made architectural decisions or found non-obvious things.

**When should I use it vs just relying on the pipeline?**
The pipeline ingests ChatGPT export history — past conversations that already happened. `/vault-save` captures the current Claude Code session: things you're deciding right now, discoveries made during development, the state of in-progress work. These won't appear in any ChatGPT export.

**Where does `/vault-save` read the vault path from?**
From `config.yaml` in the project directory (`paths.vault`). No hardcoded paths.

---

## Architecture Philosophy

**Is this RAG?**
No. The vault uses direct file access — an LLM reads `index.md` as a navigation map, then drills into relevant pages. No embeddings, no vector database at current scale (~2000 conversations, ~72 wiki pages). When the vault grows large enough that `index.md` exceeds LLM context limits, adding a semantic search layer (embedding model + ChromaDB) is the natural next step.

**How does this compare to Karpathy's approach?**
Karpathy's system ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)) uses the same philosophy: immutable `raw/` sources + synthesized `wiki/` pages + `index.md` as LLM entry point. The difference is ingestion: his system takes one source at a time via Claude Code slash commands; this pipeline batch-ingests thousands of historical conversations. The two are complementary — this pipeline for retroactive bulk ingestion, Karpathy's approach (or `/vault-save`) for ongoing capture.

**Why not just use a "second brain" repo like obsidian-second-brain?**
Those repos are Claude Code slash commands — markdown prompt files copied to `~/.claude/commands/`. There's no automation, no background process, no code. Claude does all the work live during a session when you manually invoke a command. That's fine for one-off ingestion but not for processing thousands of conversations. This pipeline is code that actually runs.
